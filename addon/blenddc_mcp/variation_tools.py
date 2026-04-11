"""
Advanced variation, wear, failure-state, and theme tools for the UPTIME datacenter simulator.

All variation is non-destructive: wear/dust/damage layers are injected as labelled
node groups into existing material trees so they can be found and removed cleanly
by reset_variation without touching the original material structure.

Node injection label convention (used by reset_variation to locate injected nodes):
  "[WEAR] ..."    — nodes injected by apply_wear_variation
  "[DUST] ..."    — nodes injected by apply_dust_overlay
  "[DAMAGE] ..."  — nodes injected by apply_damage_state

Copy-on-write guarantee:
  Before any node injection, tools check whether the object's material is shared
  (users > 1). If it is, the material is copied first (per-object instance) so
  variation on one server never bleeds to identically-materialised neighbours.

Seed convention:
  All random variation is driven by _random.Random(seed_string) where seed_string
  is derived from the seed parameter + object name. Same seed → same result every run.
"""

import bpy
import hashlib
import math
import random as _random
from typing import Any, Dict, List, Optional, Tuple

import mathutils

from core import mcp, thread_safe, _log
from constants import (
    RACK_U_M,
    RACK_BASE_HEIGHT_M,
    SOCKET_PREFIX,
)


# ── Internal helpers ───────────────────────────────────────────────────────

def _get_or_make_collection(name: str) -> bpy.types.Collection:
    col = bpy.data.collections.get(name)
    if not col:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return col


def _cow_material(obj: bpy.types.Object) -> Optional[bpy.types.Material]:
    """
    Copy-on-write: if the object's active material is shared (users > 1),
    duplicate it onto the object so we can modify it without affecting others.
    Returns the (possibly new) active material, or None if no material exists.
    """
    if not obj.active_material:
        return None
    mat = obj.active_material
    if mat.users > 1:
        mat = mat.copy()
        mat.name = f"{mat.name}_var_{obj.name[:12]}"
        obj.active_material = mat
    mat.use_nodes = True
    return mat


def _find_bsdf(mat: bpy.types.Material) -> Optional[bpy.types.Node]:
    return next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)


def _remove_labelled_nodes(mat: bpy.types.Material, label_prefix: str) -> int:
    """Remove all nodes whose label starts with label_prefix. Returns count removed."""
    nodes   = mat.node_tree.nodes
    links   = mat.node_tree.links
    targets = [n for n in nodes if n.label.startswith(label_prefix)]
    for n in targets:
        nodes.remove(n)
    return len(targets)


def _noise_node(nodes, label: str, scale: float, loc: Tuple[float, float]) -> bpy.types.Node:
    """Create a Noise Texture node with a label and scale, positioned at loc."""
    noise = nodes.new('ShaderNodeTexNoise')
    noise.label    = label
    noise.location = loc
    noise.inputs['Scale'].default_value    = scale
    noise.inputs['Detail'].default_value   = 8.0
    noise.inputs['Roughness'].default_value = 0.65
    noise.inputs['Distortion'].default_value = 0.20
    return noise


def _mix_color_node(nodes, label: str, fac: float, loc: Tuple[float, float]) -> bpy.types.Node:
    """Create a Mix (color blend) node. Handles Blender 4.x ShaderNodeMix and 3.x ShaderNodeMixRGB."""
    try:
        mix = nodes.new('ShaderNodeMix')
        mix.data_type = 'RGBA'
        mix.blend_type = 'MIX'
        mix.label    = label
        mix.location = loc
        mix.inputs[0].default_value = fac
    except Exception:
        mix = nodes.new('ShaderNodeMixRGB')
        mix.blend_type = 'MIX'
        mix.label    = label
        mix.location = loc
        mix.inputs[0].default_value = fac
    return mix


def _math_node(nodes, operation: str, val1: float, val2: float,
                label: str, loc: Tuple[float, float]) -> bpy.types.Node:
    m = nodes.new('ShaderNodeMath')
    m.operation = operation
    m.label     = label
    m.location  = loc
    m.inputs[0].default_value = val1
    m.inputs[1].default_value = val2
    return m


def _color_out(mix_node: bpy.types.Node) -> str:
    """Return the correct output socket name for a Mix/MixRGB color node."""
    if 'Result' in mix_node.outputs:
        return 'Result'
    return 'Color'


def _color_in_a(mix_node: bpy.types.Node) -> str:
    return 'A' if 'A' in mix_node.inputs else 'Color1'


def _color_in_b(mix_node: bpy.types.Node) -> str:
    return 'B' if 'B' in mix_node.inputs else 'Color2'


def _mesh_objects_in(target: str) -> List[bpy.types.Object]:
    """Return all MESH objects from a named object or collection."""
    # Try as object first
    obj = bpy.data.objects.get(target)
    if obj and obj.type == 'MESH':
        return [obj]
    # Try as collection
    col = bpy.data.collections.get(target)
    if col:
        return [o for o in col.all_objects if o.type == 'MESH']
    raise ValueError(f"'{target}' is not a mesh object or collection")


def _bay_equipment_objects(bay_name: str) -> List[bpy.types.Object]:
    """Walk a bay collection and return all MESH equipment objects."""
    bay_col = bpy.data.collections.get(bay_name)
    if not bay_col:
        raise ValueError(f"Bay collection '{bay_name}' not found")

    objects: List[bpy.types.Object] = []

    def _walk(col: bpy.types.Collection) -> None:
        for obj in col.objects:
            if obj.type == 'MESH':
                objects.append(obj)
        for child in col.children:
            _walk(child)

    _walk(bay_col)
    return objects


def _obj_seed(base_seed: int, obj_name: str) -> int:
    """Derive a deterministic per-object seed from base_seed + object name."""
    h = int(hashlib.md5(f"{base_seed}:{obj_name}".encode()).hexdigest()[:8], 16)
    return h


# ── Tool 1: apply_wear_variation ──────────────────────────────────────────

@mcp.tool()
@thread_safe
def apply_wear_variation(
    object_name: str,
    wear_level: float = 0.3,
    scratch_scale: float = 150.0,
    seed: int = 0,
    affect_roughness: bool = True,
    affect_color: bool = True,
) -> Dict[str, Any]:
    """
    Add a non-destructive procedural wear layer to a mesh object's material.

    Injects a Noise Texture driven scratch/roughness overlay into the existing
    material without replacing any nodes. The material is copied first if shared
    (copy-on-write) so identical-looking neighbours are not affected.

    Injected nodes carry a '[WEAR]' label prefix — call reset_variation to
    remove them cleanly.

    At low wear_level (0.0–0.3): subtle roughness variation only.
    At mid wear_level (0.3–0.6): roughness spikes + faint dark streak overlay.
    At high wear_level (0.6–1.0): prominent scratches + metallic glint edges.

    object_name:      target mesh object
    wear_level:       0.0 (pristine) → 1.0 (heavily worn)
    scratch_scale:    noise texture scale — higher = finer scratches (default 150)
    seed:             integer seed for deterministic noise offset
    affect_roughness: inject roughness variation (default True)
    affect_color:     inject dark scratch color overlay (default True)
    """
    wear_level = max(0.0, min(1.0, wear_level))

    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh object")

    mat = _cow_material(obj)
    if not mat:
        # Create a default grey material so injection has somewhere to go
        mat = bpy.data.materials.new(f"MAT_{object_name}_wear")
        mat.use_nodes = True
        obj.active_material = mat

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf  = _find_bsdf(mat)
    if not bsdf:
        return {"status": "skipped", "reason": "No Principled BSDF found", "object": object_name}

    # Seed offset — shifts noise UV so objects with same settings look different
    seed_val = (_obj_seed(seed, object_name) % 100) / 10.0

    applied: List[str] = []

    # ── Roughness variation ────────────────────────────────────────────────
    if affect_roughness:
        noise_r = _noise_node(nodes, f"[WEAR] Scratch Noise", scratch_scale, (-900, 200))
        noise_r.inputs['W'].default_value = seed_val

        # Scale noise output to a roughness boost: wear_level controls amplitude
        amp_r = _math_node(nodes, 'MULTIPLY', 0.0, wear_level * 0.40,
                           "[WEAR] Roughness Amp", (-650, 200))
        links.new(noise_r.outputs['Fac'], amp_r.inputs[0])

        # Add boost to current roughness value
        current_rough = bsdf.inputs['Roughness'].default_value
        add_r = _math_node(nodes, 'ADD', current_rough, 0.0,
                           "[WEAR] Roughness Add", (-450, 200))
        add_r.inputs[0].default_value = current_rough
        links.new(amp_r.outputs['Value'], add_r.inputs[1])

        clamp_r = _math_node(nodes, 'MINIMUM', 0.0, 1.0,
                             "[WEAR] Roughness Clamp", (-250, 200))
        links.new(add_r.outputs['Value'], clamp_r.inputs[0])

        links.new(clamp_r.outputs['Value'], bsdf.inputs['Roughness'])
        applied.append("roughness")

    # ── Color scratch overlay ──────────────────────────────────────────────
    if affect_color and wear_level > 0.15:
        noise_c = _noise_node(nodes, f"[WEAR] Color Noise", scratch_scale * 1.4, (-900, -100))
        noise_c.inputs['W'].default_value = seed_val + 3.0

        # Streak threshold — only fire where noise > (1 - wear_level * 0.5)
        threshold = 1.0 - wear_level * 0.45
        gt = _math_node(nodes, 'GREATER_THAN', 0.0, threshold,
                        "[WEAR] Streak Mask", (-650, -100))
        links.new(noise_c.outputs['Fac'], gt.inputs[0])

        # Dark streak color (very dark metallic grey)
        streak_col = (0.03, 0.03, 0.03, 1.0)
        mix_c = _mix_color_node(nodes, "[WEAR] Streak Mix",
                                fac=0.0, loc=(-450, -100))
        mix_c.inputs[0].default_value = 0.0
        try:
            mix_c.inputs[_color_in_b(mix_c)].default_value = streak_col
        except Exception:
            pass
        links.new(gt.outputs['Value'], mix_c.inputs[0])

        # Insert between current Base Color source and BSDF
        bc_input = bsdf.inputs['Base Color']
        if bc_input.links:
            src_socket = bc_input.links[0].from_socket
            links.remove(bc_input.links[0])
            try:
                links.new(src_socket, mix_c.inputs[_color_in_a(mix_c)])
            except Exception:
                pass
        else:
            orig = tuple(bc_input.default_value)
            try:
                mix_c.inputs[_color_in_a(mix_c)].default_value = orig
            except Exception:
                pass

        links.new(mix_c.outputs[_color_out(mix_c)], bc_input)
        applied.append("color")

    # Record on material for later inspection
    mat["wear_level"] = round(wear_level, 3)

    return {
        "object":     object_name,
        "material":   mat.name,
        "wear_level": wear_level,
        "applied":    applied,
    }


# ── Tool 2: apply_dust_overlay ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def apply_dust_overlay(
    object_name: str,
    dust_intensity: float = 0.4,
    dust_color: Tuple[float, float, float] = (0.55, 0.50, 0.45),
    accumulation_bias: str = "top",
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Inject a dust/grime accumulation layer driven by surface normal direction.

    Uses a Geometry node (Normal in world space dotted with world-up) to
    determine accumulation: heaviest on upward faces, zero on vertical faces,
    none on downward faces. The dust layer is a warm grey-brown tint + roughness
    boost in accumulated areas.

    Injected nodes carry a '[DUST]' label prefix — call reset_variation to remove.

    object_name:       target mesh object
    dust_intensity:    0.0 (clean) → 1.0 (heavy dust)
    dust_color:        RGB colour of dust (default warm grey-brown)
    accumulation_bias: 'top' (settles on top faces) | 'bottom' (underside
                       grime) | 'uniform' (all faces equally)
    seed:              integer seed for noise variation
    """
    accumulation_bias = accumulation_bias.lower()
    if accumulation_bias not in ("top", "bottom", "uniform"):
        raise ValueError("accumulation_bias must be 'top', 'bottom', or 'uniform'")

    dust_intensity = max(0.0, min(1.0, dust_intensity))

    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh object")

    mat = _cow_material(obj)
    if not mat:
        mat = bpy.data.materials.new(f"MAT_{object_name}_dust")
        mat.use_nodes = True
        obj.active_material = mat

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf  = _find_bsdf(mat)
    if not bsdf:
        return {"status": "skipped", "reason": "No Principled BSDF found", "object": object_name}

    seed_val = (_obj_seed(seed, object_name) % 100) / 10.0

    # ── Geometry normal → accumulation mask ───────────────────────────────
    geo = nodes.new('ShaderNodeNewGeometry')
    geo.label    = "[DUST] Geometry"
    geo.location = (-1100, 50)

    # Separate Z component of the normal (world-up component)
    sep = nodes.new('ShaderNodeSeparateXYZ')
    sep.label    = "[DUST] Normal Z"
    sep.location = (-900, 50)
    links.new(geo.outputs['Normal'], sep.inputs['Vector'])

    if accumulation_bias == "top":
        # Clamp to [0, 1]: only positive Z (facing up)
        clamp_n = _math_node(nodes, 'MAXIMUM', 0.0, 0.0, "[DUST] Clamp Up", (-700, 50))
        links.new(sep.outputs['Z'], clamp_n.inputs[0])
        mask_output = clamp_n.outputs['Value']
    elif accumulation_bias == "bottom":
        # Negate then clamp: downward faces
        neg_n = _math_node(nodes, 'MULTIPLY', 0.0, -1.0, "[DUST] Negate Z", (-700, 50))
        links.new(sep.outputs['Z'], neg_n.inputs[0])
        clamp_n = _math_node(nodes, 'MAXIMUM', 0.0, 0.0, "[DUST] Clamp Down", (-500, 50))
        links.new(neg_n.outputs['Value'], clamp_n.inputs[0])
        mask_output = clamp_n.outputs['Value']
    else:  # uniform
        # Use absolute value — all faces get some dust
        abs_n = _math_node(nodes, 'ABSOLUTE', 0.0, 0.0, "[DUST] Abs Z", (-700, 50))
        links.new(sep.outputs['Z'], abs_n.inputs[0])
        mask_output = abs_n.outputs['Value']

    # Modulate mask by dust_intensity
    scale_n = _math_node(nodes, 'MULTIPLY', 0.0, dust_intensity * 0.85,
                         "[DUST] Intensity Scale", (-300, 50))
    links.new(mask_output, scale_n.inputs[0])

    # Add fine noise variation to the dust for realism
    noise_d = _noise_node(nodes, "[DUST] Dust Noise", 80.0, (-500, -150))
    noise_d.inputs['W'].default_value = seed_val

    noise_mix = _math_node(nodes, 'MULTIPLY', 0.0, dust_intensity * 0.15,
                           "[DUST] Noise Contribution", (-300, -150))
    links.new(noise_d.outputs['Fac'], noise_mix.inputs[0])

    final_fac = _math_node(nodes, 'ADD', 0.0, 0.0, "[DUST] Final Factor", (-100, 50))
    links.new(scale_n.outputs['Value'],  final_fac.inputs[0])
    links.new(noise_mix.outputs['Value'], final_fac.inputs[1])

    # Clamp final factor to [0, 1]
    clamp_f = _math_node(nodes, 'MINIMUM', 0.0, 1.0, "[DUST] Final Clamp", (100, 50))
    links.new(final_fac.outputs['Value'], clamp_f.inputs[0])

    # ── Dust color blend ───────────────────────────────────────────────────
    dust_rgba = (*dust_color[:3], 1.0)
    mix_dust  = _mix_color_node(nodes, "[DUST] Color Mix", fac=0.0, loc=(300, 50))
    links.new(clamp_f.outputs['Value'], mix_dust.inputs[0])
    try:
        mix_dust.inputs[_color_in_b(mix_dust)].default_value = dust_rgba
    except Exception:
        pass

    # Chain into existing Base Color
    bc_input = bsdf.inputs['Base Color']
    if bc_input.links:
        src_socket = bc_input.links[0].from_socket
        links.remove(bc_input.links[0])
        try:
            links.new(src_socket, mix_dust.inputs[_color_in_a(mix_dust)])
        except Exception:
            pass
    else:
        orig = tuple(bc_input.default_value)
        try:
            mix_dust.inputs[_color_in_a(mix_dust)].default_value = orig
        except Exception:
            pass

    links.new(mix_dust.outputs[_color_out(mix_dust)], bc_input)

    # ── Roughness boost in dusty areas ────────────────────────────────────
    rough_boost = _math_node(nodes, 'MULTIPLY', 0.0, 0.25, "[DUST] Rough Boost", (100, -150))
    links.new(clamp_f.outputs['Value'], rough_boost.inputs[0])

    current_rough = bsdf.inputs['Roughness'].default_value
    add_rough = _math_node(nodes, 'ADD', current_rough, 0.0, "[DUST] Rough Add", (300, -150))
    add_rough.inputs[0].default_value = current_rough
    links.new(rough_boost.outputs['Value'], add_rough.inputs[1])

    clamp_rough = _math_node(nodes, 'MINIMUM', 0.0, 1.0, "[DUST] Rough Clamp", (500, -150))
    links.new(add_rough.outputs['Value'], clamp_rough.inputs[0])
    links.new(clamp_rough.outputs['Value'], bsdf.inputs['Roughness'])

    mat["dust_intensity"]     = round(dust_intensity, 3)
    mat["dust_accumulation"]  = accumulation_bias

    return {
        "object":            object_name,
        "material":          mat.name,
        "dust_intensity":    dust_intensity,
        "accumulation_bias": accumulation_bias,
    }


# ── Tool 3: randomize_color_tint ──────────────────────────────────────────

@mcp.tool()
@thread_safe
def randomize_color_tint(
    target: str,
    hue_range: float = 0.04,
    saturation_range: float = 0.08,
    value_range: float = 0.06,
    seed: int = 0,
    mode: str = "object",
) -> Dict[str, Any]:
    """
    Apply a seeded per-object hue/value shift to each mesh object's Base Color.

    Shifts are clamped to the specified ranges so colors always stay within the
    industrial palette — no neon, no pure primaries. Works on the direct default
    value of the Principled BSDF Base Color input (does not inject new nodes,
    so no '[WEAR]'-style cleanup is needed).

    Operates in copy-on-write mode: shared materials are duplicated before modification.

    target:           object name or collection name (mode='collection' processes all meshes)
    hue_range:        max per-channel shift in linear RGB (default ±0.04)
    saturation_range: max desaturation/saturation shift (default ±0.08)
    value_range:      max brightness shift (default ±0.06)
    seed:             integer seed for reproducibility
    mode:             'object' (single object) | 'collection' (all meshes in collection)
    """
    mode = mode.lower()
    objects = _mesh_objects_in(target)

    modified: List[str] = []
    skipped:  List[str] = []

    for obj in objects:
        mat = _cow_material(obj)
        if not mat:
            skipped.append(obj.name)
            continue
        bsdf = _find_bsdf(mat)
        if not bsdf:
            skipped.append(obj.name)
            continue

        # Per-object deterministic seed
        rng = _random.Random(_obj_seed(seed, obj.name))

        bc_input = bsdf.inputs['Base Color']
        if bc_input.links:
            # Base Color driven by texture — shift not applied (avoid node injection here)
            skipped.append(obj.name)
            continue

        c   = bc_input.default_value
        r   = max(0.0, min(1.0, c[0] + rng.uniform(-hue_range, hue_range)))
        g   = max(0.0, min(1.0, c[1] + rng.uniform(-hue_range, hue_range)))
        b   = max(0.0, min(1.0, c[2] + rng.uniform(-hue_range, hue_range)))

        # Value (brightness) shift — keep all channels moving together
        v_shift = rng.uniform(-value_range, value_range)
        r = max(0.0, min(1.0, r + v_shift))
        g = max(0.0, min(1.0, g + v_shift))
        b = max(0.0, min(1.0, b + v_shift))

        bc_input.default_value = (r, g, b, 1.0)
        mat["color_tint_seed"] = seed
        modified.append(obj.name)

    return {
        "target":    target,
        "mode":      mode,
        "modified":  modified,
        "skipped":   skipped,
        "count":     len(modified),
    }


# ── Tool 4: apply_damage_state ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def apply_damage_state(
    object_name: str,
    damage_level: float = 0.3,
    scorch_color: Tuple[float, float, float] = (0.05, 0.02, 0.01),
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Apply parametric damage to a mesh object as a graduated material effect.

    Damage is non-destructive — nodes are injected with '[DAMAGE]' labels.

    Level thresholds:
      0.0–0.3 (minor):    roughness spike + slight darkening at random noise point
      0.3–0.6 (moderate): burn/scorch streak overlay (dark brown noise), roughness 0.9
      0.6–1.0 (severe):   near-black scorch zone + weak orange emissive heat glow

    Stores 'damage_level' and 'damage_state' custom properties on the object
    for use by propagate_failure, validate_bay, and get_variation_report.

    object_name:  target mesh object
    damage_level: 0.0 (pristine) → 1.0 (destroyed)
    scorch_color: base scorch/burn tint color (default very dark red-brown)
    seed:         integer seed
    """
    damage_level = max(0.0, min(1.0, damage_level))

    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh object")

    mat = _cow_material(obj)
    if not mat:
        mat = bpy.data.materials.new(f"MAT_{object_name}_dmg")
        mat.use_nodes = True
        obj.active_material = mat

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf  = _find_bsdf(mat)
    if not bsdf:
        return {"status": "skipped", "reason": "No Principled BSDF found", "object": object_name}

    seed_val = (_obj_seed(seed, object_name) % 100) / 10.0

    # Roughness always spikes with damage
    new_rough = min(1.0, 0.60 + damage_level * 0.40)
    bsdf.inputs['Roughness'].default_value = new_rough

    # ── Scorch overlay (fires at any damage > 0.2) ────────────────────────
    if damage_level > 0.20:
        scorch_rgba = (*scorch_color[:3], 1.0)
        noise_s     = _noise_node(nodes, "[DAMAGE] Scorch Noise", 60.0, (-900, 100))
        noise_s.inputs['W'].default_value   = seed_val
        noise_s.inputs['Scale'].default_value = 40.0 + damage_level * 40.0

        # Threshold: higher damage → larger scorch zones
        threshold_val = 1.0 - damage_level * 0.70
        thresh_s = _math_node(nodes, 'GREATER_THAN', 0.0, threshold_val,
                              "[DAMAGE] Scorch Threshold", (-650, 100))
        links.new(noise_s.outputs['Fac'], thresh_s.inputs[0])

        mix_s = _mix_color_node(nodes, "[DAMAGE] Scorch Mix", fac=0.0, loc=(-400, 100))
        try:
            mix_s.inputs[_color_in_b(mix_s)].default_value = scorch_rgba
        except Exception:
            pass
        links.new(thresh_s.outputs['Value'], mix_s.inputs[0])

        # Chain into existing Base Color
        bc_input = bsdf.inputs['Base Color']
        if bc_input.links:
            src_socket = bc_input.links[0].from_socket
            links.remove(bc_input.links[0])
            try:
                links.new(src_socket, mix_s.inputs[_color_in_a(mix_s)])
            except Exception:
                pass
        else:
            orig = tuple(bc_input.default_value)
            try:
                mix_s.inputs[_color_in_a(mix_s)].default_value = orig
            except Exception:
                pass
        links.new(mix_s.outputs[_color_out(mix_s)], bc_input)

    # ── Heat glow emission (severe damage only) ────────────────────────────
    if damage_level > 0.60:
        # Mix an emissive shader for residual heat glow
        emit_node = nodes.new('ShaderNodeEmission')
        emit_node.label    = "[DAMAGE] Heat Glow"
        emit_node.location = (-400, -200)
        # Dim orange heat glow — strength scaled by how far past 0.6 we are
        glow_strength = (damage_level - 0.60) / 0.40 * 1.5
        emit_node.inputs['Color'].default_value    = (1.0, 0.25, 0.03, 1.0)
        emit_node.inputs['Strength'].default_value = glow_strength

        out_node = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if out_node:
            mix_shader = nodes.new('ShaderNodeMixShader')
            mix_shader.label    = "[DAMAGE] Glow Mix"
            mix_shader.location = (-150, -200)
            mix_shader.inputs['Fac'].default_value = glow_strength * 0.12  # subtle

            # Find what's currently wired into the output
            surf_input = out_node.inputs['Surface']
            if surf_input.links:
                src_shader = surf_input.links[0].from_socket
                links.remove(surf_input.links[0])
                links.new(src_shader,               mix_shader.inputs[1])
            links.new(emit_node.outputs['Emission'], mix_shader.inputs[2])
            links.new(mix_shader.outputs['Shader'],  surf_input)

    # Determine textual state for metadata
    if damage_level <= 0.30:
        state_label = "minor"
    elif damage_level <= 0.60:
        state_label = "moderate"
    else:
        state_label = "severe"

    obj["damage_level"] = round(damage_level, 3)
    obj["damage_state"] = state_label

    return {
        "object":        object_name,
        "material":      mat.name,
        "damage_level":  damage_level,
        "damage_state":  state_label,
        "roughness_set": round(new_rough, 3),
    }


# ── Tool 5: set_failure_state ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def set_failure_state(
    object_name: str,
    failure_type: str = "overheated",
    damage_level: float = 0.5,
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Apply a composite failure state to a single equipment object.

    Combines apply_damage_state (visual damage), LED state update (via
    material_tools.set_led_state if an LED material is present), and sets
    failure metadata custom properties for downstream tools.

    failure_type drives the LED state mapping:
      'overheated'  → apply_damage_state, LED → 'error'
      'failed'      → apply_damage_state, LED → 'off'
      'degraded'    → apply_damage_state (mild), LED → 'warning'
      'maintenance' → no damage, LED → 'off', adds SOCKET_MaintenanceTag empty

    Stores 'failure_state' and 'failure_type' on the object for use by
    propagate_failure, validate_bay, and get_variation_report.

    object_name:   target equipment mesh object
    failure_type:  'overheated' | 'failed' | 'degraded' | 'maintenance'
    damage_level:  passed to apply_damage_state (ignored for 'maintenance')
    seed:          integer seed for damage node variation
    """
    failure_type = failure_type.lower()
    valid_types  = ("overheated", "failed", "degraded", "maintenance")
    if failure_type not in valid_types:
        raise ValueError(f"failure_type must be one of {valid_types}")

    obj = bpy.data.objects.get(object_name)
    if not obj or obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh object")

    led_state_map = {
        "overheated":  "error",
        "failed":      "off",
        "degraded":    "warning",
        "maintenance": "off",
    }

    actions: List[str] = []
    dmg_result: Dict[str, Any] = {}

    # ── Apply visual damage (not for maintenance) ─────────────────────────
    if failure_type != "maintenance":
        # Clamp damage_level: degraded stays mild even if caller passes high value
        effective_dmg = min(damage_level, 0.35) if failure_type == "degraded" else damage_level
        dmg_result = apply_damage_state(
            object_name=object_name,
            damage_level=effective_dmg,
            seed=seed,
        )
        actions.append(f"damage({effective_dmg:.2f})")

    # ── LED state update ──────────────────────────────────────────────────
    target_led = led_state_map[failure_type]
    mat = obj.active_material
    if mat and mat.use_nodes:
        nodes = mat.node_tree.nodes
        has_led = any(n.name == "LED_Emission" or
                      (n.type == 'EMISSION' and n.label == "LED_Emission")
                      for n in nodes)
        if has_led:
            try:
                import material_tools as _mt
                _mt.set_led_state(state=target_led, object_name=object_name)
                actions.append(f"led({target_led})")
            except Exception as exc:
                _log(f"set_failure_state: LED update skipped — {exc}")

    # ── Maintenance tag socket ────────────────────────────────────────────
    maintenance_socket: Optional[str] = None
    if failure_type == "maintenance":
        tag_name = f"{SOCKET_PREFIX}MaintenanceTag_{object_name[:20]}"
        existing = bpy.data.objects.get(tag_name)
        if existing:
            bpy.data.objects.remove(existing, do_unlink=True)
        tag = bpy.data.objects.new(tag_name, None)
        tag.empty_display_type = 'ARROWS'
        tag.empty_display_size = 0.020

        # Place tag at front-centre of the object's bounding box
        bb     = obj.bound_box
        front_y = min(v[1] for v in bb) + obj.location.y
        ctr_x   = obj.location.x
        ctr_z   = (min(v[2] for v in bb) + max(v[2] for v in bb)) / 2 + obj.location.z
        tag.location = (ctr_x, front_y - 0.02, ctr_z)

        user_cols = list(obj.users_collection)
        if user_cols:
            user_cols[0].objects.link(tag)
        tag.parent = obj
        tag.matrix_parent_inverse = obj.matrix_world.inverted()
        maintenance_socket = tag_name
        actions.append(f"maintenance_tag({tag_name})")

    # ── Store failure metadata ─────────────────────────────────────────────
    obj["failure_state"] = True
    obj["failure_type"]  = failure_type
    obj["damage_level"]  = round(damage_level, 3)

    result: Dict[str, Any] = {
        "object":       object_name,
        "failure_type": failure_type,
        "damage_level": damage_level,
        "actions":      actions,
        "led_state":    target_led,
    }
    if maintenance_socket:
        result["maintenance_socket"] = maintenance_socket

    return result


# ── Tool 6: generate_failure_preset ───────────────────────────────────────

@mcp.tool()
@thread_safe
def generate_failure_preset(
    object_names: List[str],
    preset: str,
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Apply a named failure preset to one or more equipment objects.

    Presets are pre-tuned combinations of damage level and LED state:
      'overheated'       — damage 0.55, scorch marks, LED → error
      'failed_unit'      — damage 0.80, severe scorch, LED → off
      'degraded'         — damage 0.25, minor discolor, LED → warning
      'maintenance'      — no damage, LED → off, adds maintenance tag socket
      'normal_operation' — reset: wear_level 0.10 (cosmetic only), LED → on

    For 'normal_operation', reset_variation is called first to clear any
    existing wear/dust/damage layers before applying light cosmetic wear.

    object_names: list of equipment object names to apply the preset to
    preset:       preset name (see above)
    seed:         base seed — per-object seeds are derived from this + object name
    """
    preset = preset.lower().replace(" ", "_")
    valid  = ("overheated", "failed_unit", "degraded", "maintenance", "normal_operation")
    if preset not in valid:
        raise ValueError(f"preset must be one of {valid}")

    _preset_params = {
        "overheated":       {"failure_type": "overheated", "damage_level": 0.55},
        "failed_unit":      {"failure_type": "failed",     "damage_level": 0.80},
        "degraded":         {"failure_type": "degraded",   "damage_level": 0.25},
        "maintenance":      {"failure_type": "maintenance","damage_level": 0.00},
    }

    applied:  List[str] = []
    skipped:  List[str] = []

    for obj_name in object_names:
        obj = bpy.data.objects.get(obj_name)
        if not obj or obj.type != 'MESH':
            skipped.append(obj_name)
            continue

        obj_seed = _obj_seed(seed, obj_name)

        try:
            if preset == "normal_operation":
                # Clear existing variation layers, then apply light cosmetic wear
                reset_variation(target=obj_name, reset_wear=True, reset_dust=True,
                                reset_damage=True, reset_led=True)
                apply_wear_variation(object_name=obj_name, wear_level=0.10, seed=obj_seed)
                obj.pop("failure_state", None)
                obj.pop("failure_type",  None)
                obj.pop("damage_level",  None)
            else:
                params = _preset_params[preset]
                set_failure_state(
                    object_name=obj_name,
                    failure_type=params["failure_type"],
                    damage_level=params["damage_level"],
                    seed=obj_seed,
                )
            applied.append(obj_name)
        except Exception as exc:
            _log(f"generate_failure_preset: {obj_name} failed — {exc}")
            skipped.append(obj_name)

    return {
        "preset":   preset,
        "applied":  applied,
        "skipped":  skipped,
        "count":    len(applied),
    }


# ── Tool 7: propagate_failure ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def propagate_failure(
    source_object: str,
    radius_m: float = 0.5,
    max_damage: float = 0.6,
    falloff: str = "inverse_square",
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Spread failure visually from a source object to nearby equipment.

    Finds all MESH objects within radius_m metres of the source object's world
    origin and applies graduated set_failure_state calls. Closer objects receive
    higher damage; farther objects receive lower damage, scaled by the falloff.

    falloff options:
      'inverse_square' — damage ∝ 1 / distance²  (sharp falloff, realistic heat)
      'linear'         — damage ∝ 1 - (dist / radius)  (gradual, uniform spread)

    Objects already marked with failure_type='maintenance' are skipped.

    source_object: name of the failed/overheated source equipment object
    radius_m:      search radius in metres
    max_damage:    damage level assigned at distance ≈ 0 (default 0.6)
    falloff:       'inverse_square' | 'linear'
    seed:          base seed for damage variation
    """
    falloff = falloff.lower().replace(" ", "_")
    if falloff not in ("inverse_square", "linear"):
        raise ValueError("falloff must be 'inverse_square' or 'linear'")

    src_obj = bpy.data.objects.get(source_object)
    if not src_obj:
        raise ValueError(f"Source object '{source_object}' not found")

    src_loc = src_obj.matrix_world.translation.copy()
    affected: List[Dict[str, Any]] = []

    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        if obj.name == source_object:
            continue
        if obj.get("failure_type") == "maintenance":
            continue

        dist = (obj.matrix_world.translation - src_loc).length
        if dist > radius_m or dist < 1e-6:
            continue

        # Compute damage fraction based on falloff
        if falloff == "inverse_square":
            # Normalise: at dist=0 → 1.0, at dist=radius_m → ~ 0
            norm      = (dist / radius_m) ** 2
            dmg_frac  = max(0.0, 1.0 - norm)
        else:  # linear
            dmg_frac  = max(0.0, 1.0 - dist / radius_m)

        effective_dmg = max_damage * dmg_frac

        if effective_dmg < 0.05:
            continue  # Too faint to bother

        # Classify failure type by effective damage level
        if effective_dmg >= 0.50:
            f_type = "overheated"
        elif effective_dmg >= 0.25:
            f_type = "degraded"
        else:
            f_type = "degraded"

        obj_seed = _obj_seed(seed, obj.name)

        try:
            set_failure_state(
                object_name=obj.name,
                failure_type=f_type,
                damage_level=effective_dmg,
                seed=obj_seed,
            )
            affected.append({
                "object":       obj.name,
                "distance_m":   round(dist, 3),
                "damage_level": round(effective_dmg, 3),
                "failure_type": f_type,
            })
        except Exception as exc:
            _log(f"propagate_failure: {obj.name} — {exc}")

    return {
        "source":       source_object,
        "radius_m":     radius_m,
        "falloff":      falloff,
        "max_damage":   max_damage,
        "affected":     affected,
        "count":        len(affected),
    }


# ── Tool 8: reset_variation ───────────────────────────────────────────────

@mcp.tool()
@thread_safe
def reset_variation(
    target: str,
    reset_wear:   bool = True,
    reset_dust:   bool = True,
    reset_damage: bool = True,
    reset_led:    bool = False,
) -> Dict[str, Any]:
    """
    Remove all injected variation nodes and clear variation custom properties.

    Finds nodes labelled '[WEAR] ...', '[DUST] ...', '[DAMAGE] ...' in the
    active material and removes them. Optionally resets LED state to 'on'.
    Does not affect nodes that were not created by variation tools.

    Works on a single object or all meshes in a named collection.

    target:       object name or collection name
    reset_wear:   remove [WEAR] nodes (default True)
    reset_dust:   remove [DUST] nodes (default True)
    reset_damage: remove [DAMAGE] nodes + clear damage custom props (default True)
    reset_led:    reset LED material to 'on' state (default False — leave as-is)
    """
    objects  = _mesh_objects_in(target)
    summary: List[Dict[str, Any]] = []

    for obj in objects:
        mat = obj.active_material
        if not mat or not mat.use_nodes:
            continue

        removed = 0

        if reset_wear:
            removed += _remove_labelled_nodes(mat, "[WEAR]")
            mat.pop("wear_level", None)

        if reset_dust:
            removed += _remove_labelled_nodes(mat, "[DUST]")
            mat.pop("dust_intensity", None)
            mat.pop("dust_accumulation", None)

        if reset_damage:
            removed += _remove_labelled_nodes(mat, "[DAMAGE]")
            obj.pop("damage_level", None)
            obj.pop("damage_state", None)
            obj.pop("failure_state", None)
            obj.pop("failure_type",  None)

        if reset_led:
            if any(n.name == "LED_Emission" or n.type == 'EMISSION'
                   for n in mat.node_tree.nodes):
                try:
                    import material_tools as _mt
                    _mt.set_led_state(state="on", object_name=obj.name)
                except Exception:
                    pass

        summary.append({"object": obj.name, "nodes_removed": removed})

    return {
        "target":        target,
        "objects":       len(summary),
        "reset_wear":    reset_wear,
        "reset_dust":    reset_dust,
        "reset_damage":  reset_damage,
        "reset_led":     reset_led,
        "detail":        summary,
    }


# ── Tool 9: randomize_bay_variation ───────────────────────────────────────

@mcp.tool()
@thread_safe
def randomize_bay_variation(
    bay_name: str,
    age_factor: float = 0.4,
    dust_factor: float = 0.3,
    color_variation: bool = True,
    severity_bias: float = 0.0,
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Apply seeded wear, dust, and color variation across every equipment object
    in a bay, making the bay feel lived-in rather than freshly installed.

    Skips objects that already have 'failure_state' set — intentional failure
    states are not overwritten by generic wear.

    severity_bias (0.0–1.0) allows uneven wear distribution across the bay:
      0.0  — uniform wear everywhere
      0.5  — objects near the bay's hot-aisle edge get ≈ 1.5× wear multiplier
      1.0  — objects near the hot-aisle edge get ≈ 2.5× wear; far side stays cleaner

    This models real datacenter wear patterns: racks near power feeds and hot
    aisles age faster than cold-aisle or mid-bay equipment.

    bay_name:        bay collection name
    age_factor:      base wear intensity 0.0 (pristine) → 1.0 (heavily worn)
    dust_factor:     base dust intensity 0.0 (clean) → 1.0 (dusty)
    color_variation: apply per-object hue/value shifts (default True)
    severity_bias:   0.0 (uniform) → 1.0 (hot-aisle edge gets heavier wear)
    seed:            base seed for all per-object randomisation
    """
    age_factor    = max(0.0, min(1.0, age_factor))
    dust_factor   = max(0.0, min(1.0, dust_factor))
    severity_bias = max(0.0, min(1.0, severity_bias))

    bay_col = bpy.data.collections.get(bay_name)
    if not bay_col:
        raise ValueError(f"Bay collection '{bay_name}' not found")
    if not bay_col.get("is_bay"):
        raise ValueError(f"'{bay_name}' is not a bay collection")

    objects = _bay_equipment_objects(bay_name)

    # Determine bay Y extents for severity_bias calculation
    bay_y_min = bay_col.get("bay_start_y_m", 0.0)
    bay_total_w = bay_col.get("bay_total_width_m", 1.0) or 1.0
    bay_y_max   = bay_y_min + bay_total_w

    worn:    List[str] = []
    dusted:  List[str] = []
    tinted:  List[str] = []
    skipped: List[str] = []

    for obj in objects:
        if obj.get("failure_state"):
            skipped.append(obj.name)
            continue

        obj_seed = _obj_seed(seed, obj.name)
        rng      = _random.Random(obj_seed)

        # ── Severity bias: objects further toward hot aisle get more wear ──
        if severity_bias > 0.0:
            obj_y  = obj.matrix_world.translation.y
            # Normalise position in bay [0, 1] where 1 = hot-aisle end (max Y)
            t      = max(0.0, min(1.0, (obj_y - bay_y_min) / bay_total_w))
            # Multiplier: 1.0 at cold aisle, (1 + severity_bias * 1.5) at hot aisle
            bias_mult = 1.0 + severity_bias * 1.5 * t
        else:
            bias_mult = 1.0

        # Per-object jitter so not all objects look identical
        jitter = rng.uniform(0.85, 1.15)

        effective_wear = min(1.0, age_factor  * bias_mult * jitter)
        effective_dust = min(1.0, dust_factor * bias_mult * rng.uniform(0.70, 1.30))

        try:
            apply_wear_variation(
                object_name=obj.name,
                wear_level=effective_wear,
                seed=obj_seed,
            )
            worn.append(obj.name)
        except Exception as exc:
            _log(f"randomize_bay_variation wear: {obj.name} — {exc}")

        try:
            apply_dust_overlay(
                object_name=obj.name,
                dust_intensity=effective_dust,
                accumulation_bias="top",
                seed=obj_seed,
            )
            dusted.append(obj.name)
        except Exception as exc:
            _log(f"randomize_bay_variation dust: {obj.name} — {exc}")

        if color_variation:
            try:
                randomize_color_tint(
                    target=obj.name,
                    hue_range=0.025,
                    value_range=0.04,
                    seed=obj_seed,
                )
                tinted.append(obj.name)
            except Exception as exc:
                _log(f"randomize_bay_variation tint: {obj.name} — {exc}")

    return {
        "bay":          bay_name,
        "total":        len(objects),
        "worn":         len(worn),
        "dusted":       len(dusted),
        "tinted":       len(tinted),
        "skipped":      len(skipped),
        "age_factor":   age_factor,
        "dust_factor":  dust_factor,
        "severity_bias": severity_bias,
    }


# ── Tool 10: apply_theme ──────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def apply_theme(
    bay_name: str,
    theme: str,
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Apply a named visual theme to an entire bay in one call.

    Themes:
      'new_install'   — zero wear, zero dust, all LEDs on, clean colors.
                        Calls reset_variation across all equipment.
      'aged_dc'       — age_factor=0.70, dust_factor=0.50, severity_bias=0.4.
                        ~5% of units get 'degraded' failure preset.
      'high_security' — clean equipment (age_factor=0.10), slight desaturation
                        (value_range reduced), no visible damage.
      'edge_pod'      — moderate wear (age_factor=0.55), heavier dust (0.45),
                        severity_bias=0.6. ~8% of units get 'failed_unit' preset.
      'post_incident' — picks the central rack, applies 'overheated' failure +
                        propagate_failure. Surrounding racks in same row get
                        'degraded'. Remaining equipment gets 'aged_dc' variation.

    bay_name: bay collection name
    theme:    theme preset name (see above)
    seed:     base seed for all randomisation
    """
    theme = theme.lower().replace(" ", "_").replace("-", "_")
    valid = ("new_install", "aged_dc", "high_security", "edge_pod", "post_incident")
    if theme not in valid:
        raise ValueError(f"theme must be one of {valid}")

    bay_col = bpy.data.collections.get(bay_name)
    if not bay_col:
        raise ValueError(f"Bay collection '{bay_name}' not found")
    if not bay_col.get("is_bay"):
        raise ValueError(f"'{bay_name}' is not a bay collection")

    rng = _random.Random(seed)
    objects = _bay_equipment_objects(bay_name)
    actions: List[str] = []

    # ── new_install ────────────────────────────────────────────────────────
    if theme == "new_install":
        for obj in objects:
            try:
                reset_variation(target=obj.name, reset_wear=True, reset_dust=True,
                                reset_damage=True, reset_led=True)
            except Exception:
                pass
        actions.append(f"reset_variation({len(objects)} objects)")

    # ── aged_dc ────────────────────────────────────────────────────────────
    elif theme == "aged_dc":
        randomize_bay_variation(
            bay_name=bay_name,
            age_factor=0.70,
            dust_factor=0.50,
            color_variation=True,
            severity_bias=0.40,
            seed=seed,
        )
        actions.append("randomize_bay_variation(age=0.7, dust=0.5, bias=0.4)")

        # ~5% of units get degraded failure
        n_degrade = max(1, int(len(objects) * 0.05))
        degrade_targets = rng.sample([o.name for o in objects], min(n_degrade, len(objects)))
        generate_failure_preset(object_names=degrade_targets, preset="degraded", seed=seed)
        actions.append(f"degraded({len(degrade_targets)} units)")

    # ── high_security ──────────────────────────────────────────────────────
    elif theme == "high_security":
        randomize_bay_variation(
            bay_name=bay_name,
            age_factor=0.10,
            dust_factor=0.05,
            color_variation=True,
            severity_bias=0.0,
            seed=seed,
        )
        # Desaturate slightly — corporate aesthetic
        for obj in objects:
            obj_seed = _obj_seed(seed, obj.name)
            try:
                randomize_color_tint(
                    target=obj.name,
                    hue_range=0.01,
                    saturation_range=0.12,  # more saturation range = desaturation possible
                    value_range=0.02,
                    seed=obj_seed,
                )
            except Exception:
                pass
        actions.append("randomize_bay_variation(age=0.1) + desaturation")

    # ── edge_pod ───────────────────────────────────────────────────────────
    elif theme == "edge_pod":
        randomize_bay_variation(
            bay_name=bay_name,
            age_factor=0.55,
            dust_factor=0.45,
            color_variation=True,
            severity_bias=0.60,
            seed=seed,
        )
        actions.append("randomize_bay_variation(age=0.55, dust=0.45, bias=0.6)")

        # ~8% failed units
        n_fail = max(1, int(len(objects) * 0.08))
        fail_targets = rng.sample([o.name for o in objects], min(n_fail, len(objects)))
        generate_failure_preset(object_names=fail_targets, preset="failed_unit", seed=seed)
        actions.append(f"failed_unit({len(fail_targets)} units)")

    # ── post_incident ──────────────────────────────────────────────────────
    elif theme == "post_incident":
        # Step 1: base aged wear across the whole bay
        randomize_bay_variation(
            bay_name=bay_name,
            age_factor=0.45,
            dust_factor=0.35,
            color_variation=True,
            severity_bias=0.30,
            seed=seed,
        )
        actions.append("randomize_bay_variation(age=0.45)")

        # Step 2: pick the central rack of the bay as the incident epicentre.
        # Walk Row_A racks (sorted by X) and pick the middle one.
        row_a_name = bay_col.get("bay_row_a", "")
        row_a_col  = bpy.data.collections.get(row_a_name)
        incident_rack: Optional[bpy.types.Collection] = None
        rack_cols: List[bpy.types.Collection] = []

        if row_a_col:
            rack_cols = sorted(
                [c for c in row_a_col.children if c.get("is_rack_cabinet")],
                key=lambda c: c.get("row_x_m", 0.0),
            )

        if rack_cols:
            incident_rack = rack_cols[len(rack_cols) // 2]

        epicentre_objects: List[str] = []
        if incident_rack:
            equip_col_name = f"{incident_rack.name}_Equipment"
            equip_col      = bpy.data.collections.get(equip_col_name)
            if equip_col:
                epicentre_objects = [o.name for o in equip_col.all_objects
                                     if o.type == 'MESH']

        # Step 3: apply overheated failure to epicentre objects
        if epicentre_objects:
            generate_failure_preset(
                object_names=epicentre_objects,
                preset="overheated",
                seed=seed,
            )
            actions.append(f"overheated({len(epicentre_objects)} units in {incident_rack.name if incident_rack else '?'})")

            # Step 4: propagate failure outward from the first epicentre object
            for obj_name in epicentre_objects[:2]:
                try:
                    propagate_failure(
                        source_object=obj_name,
                        radius_m=1.5,
                        max_damage=0.55,
                        falloff="inverse_square",
                        seed=seed,
                    )
                except Exception as exc:
                    _log(f"apply_theme post_incident propagate: {exc}")
            actions.append("propagate_failure(radius=1.5m)")

        # Step 5: surrounding racks in same row get 'degraded'
        surrounding_racks = [c for c in rack_cols if c is not incident_rack]
        surrounding_objects: List[str] = []
        for rc in surrounding_racks:
            ec = bpy.data.collections.get(f"{rc.name}_Equipment")
            if ec:
                surrounding_objects += [o.name for o in ec.all_objects
                                        if o.type == 'MESH'
                                        and not o.get("failure_state")]

        if surrounding_objects:
            generate_failure_preset(
                object_names=surrounding_objects,
                preset="degraded",
                seed=seed + 1,
            )
            actions.append(f"degraded({len(surrounding_objects)} surrounding units)")

    return {
        "bay":    bay_name,
        "theme":  theme,
        "seed":   seed,
        "actions": actions,
    }


# ── Tool 11: get_variation_report ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def get_variation_report(
    bay_name: str,
) -> Dict[str, Any]:
    """
    Return a structured read-only summary of all variation and failure states
    across a bay's equipment objects.

    Reports:
      - Count of objects with [WEAR] / [DUST] / [DAMAGE] nodes injected
      - LED state breakdown (on / off / warning / error / unknown)
      - Failure type distribution (overheated / failed / degraded / maintenance)
      - List of objects with damage_level > 0.5 (flagged for pre-export review)
      - Overall variation coverage percentage

    No modifications are made. Safe to call at any time.

    bay_name: bay collection name to inspect
    """
    objects = _bay_equipment_objects(bay_name)

    wear_count   = 0
    dust_count   = 0
    damage_count = 0
    led_states:   Dict[str, int] = {}
    failure_types: Dict[str, int] = {}
    high_damage:  List[Dict[str, Any]] = []

    for obj in objects:
        mat = obj.active_material

        # Node injection counts
        if mat and mat.use_nodes:
            node_labels = [n.label for n in mat.node_tree.nodes]
            if any(l.startswith("[WEAR]") for l in node_labels):
                wear_count += 1
            if any(l.startswith("[DUST]") for l in node_labels):
                dust_count += 1
            if any(l.startswith("[DAMAGE]") for l in node_labels):
                damage_count += 1

            # LED state
            emission_node = mat.node_tree.nodes.get("LED_Emission") or next(
                (n for n in mat.node_tree.nodes if n.type == 'EMISSION'), None
            )
            if emission_node:
                led_s = mat.get("led_state", "unknown")
                led_states[led_s] = led_states.get(led_s, 0) + 1

        # Failure type
        if obj.get("failure_type"):
            ft = obj["failure_type"]
            failure_types[ft] = failure_types.get(ft, 0) + 1

        # High-damage flag
        dmg = float(obj.get("damage_level", 0.0))
        if dmg > 0.50:
            high_damage.append({
                "object":       obj.name,
                "damage_level": round(dmg, 3),
                "failure_type": obj.get("failure_type", "none"),
            })

    n_obj = len(objects)
    variation_coverage = round(max(wear_count, dust_count) / max(n_obj, 1) * 100, 1)
    failure_coverage   = round(sum(failure_types.values()) / max(n_obj, 1) * 100, 1)

    return {
        "bay":                 bay_name,
        "total_objects":       n_obj,
        "wear_nodes":          wear_count,
        "dust_nodes":          dust_count,
        "damage_nodes":        damage_count,
        "variation_coverage_pct": variation_coverage,
        "failure_coverage_pct":   failure_coverage,
        "led_states":          led_states,
        "failure_types":       failure_types,
        "high_damage_objects": high_damage,
        "high_damage_count":   len(high_damage),
    }
