"""
Material and texturing pipeline for the UPTIME datacenter simulator (v2.3.0).

Provides PBR master material creation, Cycles baking, ORM channel packing,
and per-instance variation tools. All materials target UE5 with Nanite +
Lumen (Windows) and Metal fallback (macOS).

UE5 conventions observed throughout:
  - ORM packing: R = Occlusion, G = Roughness, B = Metallic
  - Normal maps: DirectX convention (G-channel inverted vs Blender's OpenGL)
  - Emission: separate Emission node named "LED_Emission" so UE5 blueprints
    can drive emissive state via Material Parameter Collections

All tools use @mcp.tool() + @thread_safe from core.py.
"""

import bpy
import os
import hashlib
import random as _random
from typing import Any, Dict, List, Optional, Tuple

from core import mcp, thread_safe, _log


# ── Node-tree helpers ──────────────────────────────────────────────────────

def _get_or_create_material(name: str) -> bpy.types.Material:
    mat = bpy.data.materials.get(name)
    if not mat:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    return mat


def _clear_nodes(mat: bpy.types.Material) -> None:
    mat.node_tree.nodes.clear()


def _output_node(mat: bpy.types.Material) -> bpy.types.Node:
    nodes = mat.node_tree.nodes
    out   = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
    if not out:
        out = nodes.new('ShaderNodeOutputMaterial')
        out.location = (600, 0)
    return out


def _add_principled(mat: bpy.types.Material, location=(0, 0)) -> bpy.types.Node:
    bsdf = mat.node_tree.nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = location
    return bsdf


def _set_bsdf_color(bsdf: bpy.types.Node, rgb: Tuple) -> None:
    """Set Base Color on Principled BSDF — handles 3- and 4-component tuples."""
    val = (*rgb[:3], 1.0) if len(rgb) == 3 else tuple(rgb[:4])
    bsdf.inputs["Base Color"].default_value = val


def _set_emission(bsdf: bpy.types.Node, color: Tuple, strength: float) -> None:
    """Set emission on Principled BSDF — handles Blender 3.x and 4.x API."""
    val = (*color[:3], 1.0) if len(color) == 3 else tuple(color[:4])
    if "Emission Color" in bsdf.inputs:          # Blender 4.x
        bsdf.inputs["Emission Color"].default_value   = val
        bsdf.inputs["Emission Strength"].default_value = strength
    elif "Emission" in bsdf.inputs:              # Blender 3.x
        bsdf.inputs["Emission"].default_value = val


def _separate_combine_nodes(nodes):
    """
    Return (sep_node, combine_node, r_out, g_out, b_out, r_in, g_in, b_in)
    using Blender 4.x ShaderNodeSeparateColor when available, falling back
    to ShaderNodeSeparateRGB for older builds.
    """
    try:
        sep     = nodes.new('ShaderNodeSeparateColor')
        combine = nodes.new('ShaderNodeCombineColor')
        return sep, combine, 'Red', 'Green', 'Blue', 'Red', 'Green', 'Blue'
    except Exception:
        sep     = nodes.new('ShaderNodeSeparateRGB')
        combine = nodes.new('ShaderNodeCombineRGB')
        return sep, combine, 'R', 'G', 'B', 'R', 'G', 'B'


def _apply_mat_to_collection(mat: bpy.types.Material, collection_name: str) -> List[str]:
    """Assign mat to all mesh objects in collection. Returns list of object names."""
    col = bpy.data.collections.get(collection_name)
    if not col:
        return []
    assigned = []
    for obj in col.all_objects:
        if obj.type != 'MESH':
            continue
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)
        assigned.append(obj.name)
    return assigned


# ── Tool 1: create_rack_material ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_rack_material(
    name: str = "MAT_Rack",
    variant: str = "painted",
    base_color: Tuple[float, float, float] = (0.08, 0.08, 0.08),
    roughness: float = 0.55,
    metallic: float = 0.75,
    wear_amount: float = 0.15,
    apply_to_collection: str = "",
) -> Dict[str, Any]:
    """
    Create a Principled BSDF rack cabinet material with a procedural wear overlay.

    Three variants are available:
      'painted'      — flat dark coat, medium roughness, subtle surface noise
      'brushed_metal' — high metallic, directional noise simulating brush marks
      'anodized'     — coloured anodising, high metallic, lower roughness

    The wear overlay uses a Noise Texture (scale 50, detail 8) multiplied by
    wear_amount and added to the base roughness. wear_amount=0 = pristine,
    wear_amount=1 = heavily worn.

    All parameters are stored as custom properties on the material so
    create_material_instance can copy and diff them without re-parsing nodes.

    name:                Blender material name
    variant:             'painted' | 'brushed_metal' | 'anodized'
    base_color:          RGB base colour (linear space)
    roughness:           base roughness value
    metallic:            base metallic value
    wear_amount:         0.0 (pristine) – 1.0 (heavily worn)
    apply_to_collection: if provided, assign material to all mesh objects
                         in this collection after creation
    """
    valid_variants = ("painted", "brushed_metal", "anodized")
    variant = variant.lower()
    if variant not in valid_variants:
        raise ValueError(f"variant must be one of {valid_variants}")

    mat   = _get_or_create_material(name)
    _clear_nodes(mat)
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    out  = _output_node(mat)
    bsdf = _add_principled(mat, location=(200, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    # ── Variant-specific base values ───────────────────────────────────────
    if variant == "brushed_metal":
        metallic  = max(metallic, 0.90)
        roughness = min(roughness, 0.25)
    elif variant == "anodized":
        metallic  = max(metallic, 0.85)
        roughness = min(roughness, 0.30)

    _set_bsdf_color(bsdf, base_color)
    bsdf.inputs["Metallic"].default_value   = metallic
    bsdf.inputs["Roughness"].default_value  = roughness

    # ── Noise texture overlay (wear + directional brushing) ────────────────
    tex_coord = nodes.new('ShaderNodeTexCoord')
    tex_coord.location = (-700, -150)

    mapping = nodes.new('ShaderNodeMapping')
    mapping.location = (-500, -150)

    if variant == "brushed_metal":
        # Elongated X scale = directional scratches along rack width
        mapping.inputs['Scale'].default_value = (200.0, 1.0, 1.0)
    elif variant == "anodized":
        mapping.inputs['Scale'].default_value = (50.0, 50.0, 50.0)
    else:  # painted
        mapping.inputs['Scale'].default_value = (30.0, 30.0, 30.0)

    noise = nodes.new('ShaderNodeTexNoise')
    noise.location = (-300, -150)
    noise.inputs['Scale'].default_value     = 5.0
    noise.inputs['Detail'].default_value    = 8.0
    noise.inputs['Roughness'].default_value = 0.7

    # Wear multiplier — scales noise contribution; 0 = no effect
    wear_mult = nodes.new('ShaderNodeMath')
    wear_mult.operation = 'MULTIPLY'
    wear_mult.location  = (-100, -250)
    wear_mult.inputs[1].default_value = wear_amount * 0.30  # cap max roughness add

    # Add noise to base roughness
    add_rough = nodes.new('ShaderNodeMath')
    add_rough.operation = 'ADD'
    add_rough.location  = (-100, -150)
    add_rough.use_clamp = True
    add_rough.inputs[0].default_value = roughness

    links.new(tex_coord.outputs['Object'],    mapping.inputs['Vector'])
    links.new(mapping.outputs['Vector'],      noise.inputs['Vector'])
    links.new(noise.outputs['Fac'],           wear_mult.inputs[0])
    links.new(wear_mult.outputs['Value'],     add_rough.inputs[1])
    links.new(add_rough.outputs['Value'],     bsdf.inputs['Roughness'])

    # ── Store params as custom properties ──────────────────────────────────
    mat["mat_type"]    = "rack"
    mat["variant"]     = variant
    mat["base_color"]  = list(base_color)
    mat["roughness"]   = roughness
    mat["metallic"]    = metallic
    mat["wear_amount"] = wear_amount

    assigned = []
    if apply_to_collection:
        assigned = _apply_mat_to_collection(mat, apply_to_collection)

    return {
        "material":           name,
        "variant":            variant,
        "base_color":         list(base_color),
        "roughness":          roughness,
        "metallic":           metallic,
        "wear_amount":        wear_amount,
        "applied_to_objects": assigned,
    }


# ── Tool 2: create_equipment_material ────────────────────────────────────

@mcp.tool()
@thread_safe
def create_equipment_material(
    name: str = "MAT_Equipment",
    chassis_color: Tuple[float, float, float] = (0.10, 0.10, 0.10),
    bezel_color: Tuple[float, float, float] = (0.05, 0.05, 0.05),
    roughness: float = 0.50,
    metallic: float = 0.70,
    bezel_y_threshold: float = 0.008,
    apply_to_collection: str = "",
) -> Dict[str, Any]:
    """
    Create a two-zone chassis material for servers, switches, and patch panels.

    The front bezel zone (object-space Y < bezel_y_threshold) receives
    bezel_color; the chassis body (Y >= threshold) receives chassis_color.
    This uses a geometry-position mask — no special UV map required — so it
    works correctly on joined meshes from create_server_chassis.

    The mask is a single Principled BSDF driven by a Mix node, so UE5 imports
    it as a standard single-material mesh (no multi-material complexity).

    name:               Blender material name
    chassis_color:      RGB colour for the main body (linear space)
    bezel_color:        RGB colour for the front face / bezel zone
    roughness:          shared roughness for both zones
    metallic:           shared metallic value
    bezel_y_threshold:  object-space Y cutoff between bezel and chassis (metres)
                        default 0.008 = 8 mm, covers bezel + bay detail geometry
    apply_to_collection: assign to all mesh objects in this collection
    """
    mat   = _get_or_create_material(name)
    _clear_nodes(mat)
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    out  = _output_node(mat)
    bsdf = _add_principled(mat, location=(200, 0))
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value  = metallic

    # ── Position-based bezel mask ──────────────────────────────────────────
    # Geometry Position (object space) → Y → Less Than threshold → mix factor
    geom = nodes.new('ShaderNodeNewGeometry')
    geom.location = (-700, -100)

    sep_xyz = nodes.new('ShaderNodeSeparateXYZ')
    sep_xyz.location = (-500, -100)

    # Less Than: outputs 1.0 when Y < threshold (= bezel zone)
    less_than = nodes.new('ShaderNodeMath')
    less_than.operation = 'LESS_THAN'
    less_than.location  = (-300, -100)
    less_than.inputs[1].default_value = bezel_y_threshold

    # MixRGB: Color1=chassis, Color2=bezel, Factor=mask
    # (Factor=1 → Color2=bezel, Factor=0 → Color1=chassis)
    try:
        color_mix = nodes.new('ShaderNodeMix')
        color_mix.data_type = 'RGBA'
        color_mix.location  = (-100, 0)
        c1_in = 'A'
        c2_in = 'B'
        mix_out = 'Result'
    except Exception:
        color_mix = nodes.new('ShaderNodeMixRGB')
        color_mix.location  = (-100, 0)
        c1_in = 'Color1'
        c2_in = 'Color2'
        mix_out = 'Color'

    color_mix.inputs[c1_in].default_value = (*chassis_color, 1.0)
    color_mix.inputs[c2_in].default_value = (*bezel_color,   1.0)

    links.new(geom.outputs['Position'],         sep_xyz.inputs['Vector'])
    links.new(sep_xyz.outputs['Y'],             less_than.inputs[0])
    links.new(less_than.outputs['Value'],       color_mix.inputs[0])  # Factor
    links.new(color_mix.outputs[mix_out],       bsdf.inputs['Base Color'])

    mat["mat_type"]           = "equipment"
    mat["chassis_color"]      = list(chassis_color)
    mat["bezel_color"]        = list(bezel_color)
    mat["roughness"]          = roughness
    mat["metallic"]           = metallic
    mat["bezel_y_threshold"]  = bezel_y_threshold

    assigned = []
    if apply_to_collection:
        assigned = _apply_mat_to_collection(mat, apply_to_collection)

    return {
        "material":           name,
        "chassis_color":      list(chassis_color),
        "bezel_color":        list(bezel_color),
        "roughness":          roughness,
        "metallic":           metallic,
        "bezel_y_threshold":  bezel_y_threshold,
        "applied_to_objects": assigned,
    }


# ── Tool 3: create_led_material ───────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_led_material(
    name: str = "MAT_LED",
    color: Tuple[float, float, float] = (0.0, 1.0, 0.2),
    intensity: float = 3.0,
    state: str = "on",
) -> Dict[str, Any]:
    """
    Create an emissive LED material for status indicator geometry.

    Uses a Mix Shader between a Principled BSDF (surface detail) and a named
    Emission node ("LED_Emission"). The Mix factor drives the on/off state:
      0.0 = fully off (BSDF only, no glow)
      0.6 = on (blend of surface + emission)
      1.0 = full emission only

    The Emission node is named "LED_Emission" so set_led_state can locate it
    reliably by name for runtime state changes. In UE5 this maps to a
    Material Parameter Collection parameter that blueprints drive at runtime —
    the baked texture stays constant; only the emissive strength changes.

    States:
      'on'      — color at intensity
      'off'     — intensity = 0, mix factor = 0 (BSDF only)
      'blink'   — stored as half-intensity (actual blink = UE5 material param)
      'error'   — forces color=(1, 0.05, 0) at intensity 8.0
      'warning' — forces color=(1, 0.6, 0) at intensity 4.0

    name:      Blender material name
    color:     RGB emission colour (linear space)
    intensity: emission strength (Lumen responds at > 1.0)
    state:     'on' | 'off' | 'blink' | 'error' | 'warning'
    """
    valid_states = ("on", "off", "blink", "error", "warning")
    state = state.lower()
    if state not in valid_states:
        raise ValueError(f"state must be one of {valid_states}")

    # Resolve state overrides
    actual_color     = list(color)
    actual_intensity = intensity
    mix_factor       = 0.6   # default on-blend

    if state == "off":
        actual_intensity = 0.0
        mix_factor       = 0.0
    elif state == "blink":
        actual_intensity = intensity * 0.5
        mix_factor       = 0.4
    elif state == "error":
        actual_color     = [1.0, 0.05, 0.0]
        actual_intensity = 8.0
        mix_factor       = 0.8
    elif state == "warning":
        actual_color     = [1.0, 0.60, 0.0]
        actual_intensity = 4.0
        mix_factor       = 0.7

    mat   = _get_or_create_material(name)
    _clear_nodes(mat)
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    out = _output_node(mat)

    # Principled BSDF — provides surface detail (dark base, slight sheen)
    bsdf = _add_principled(mat, location=(-200, 100))
    _set_bsdf_color(bsdf, (*actual_color,))
    bsdf.inputs["Roughness"].default_value = 0.10
    bsdf.inputs["Metallic"].default_value  = 0.0

    # Emission node — named "LED_Emission" for reliable lookup by set_led_state
    emission = nodes.new('ShaderNodeEmission')
    emission.name     = "LED_Emission"
    emission.label    = "LED_Emission"
    emission.location = (-200, -100)
    emission.inputs['Color'].default_value    = (*actual_color[:3], 1.0)
    emission.inputs['Strength'].default_value = actual_intensity

    # Mix Shader — factor=0 → pure BSDF (off), factor>0 → adds emission
    mix = nodes.new('ShaderNodeMixShader')
    mix.location = (100, 0)
    mix.inputs['Fac'].default_value = mix_factor

    links.new(bsdf.outputs['BSDF'],         mix.inputs[1])
    links.new(emission.outputs['Emission'], mix.inputs[2])
    links.new(mix.outputs['Shader'],        out.inputs['Surface'])

    mat["mat_type"]   = "led"
    mat["led_color"]  = actual_color
    mat["led_state"]  = state
    mat["intensity"]  = actual_intensity

    return {
        "material":     name,
        "state":        state,
        "color":        actual_color,
        "intensity":    actual_intensity,
        "mix_factor":   mix_factor,
        "emission_node": "LED_Emission",
        "ue5_note":     "Drive 'LED_Emission.Strength' via Material Parameter Collection for runtime state",
    }


# ── Tool 4: create_cable_material ────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_cable_material(
    name: str = "MAT_Cable",
    color: Tuple[float, float, float] = (0.15, 0.15, 0.15),
    roughness: float = 0.70,
    cable_type: str = "ethernet",
    color_variation_seed: str = "",
) -> Dict[str, Any]:
    """
    Create a flexible cable jacket material with optional per-instance colour shift.

    Type presets (override color and roughness defaults when specified):
      'ethernet' — grey, roughness 0.70
      'power'    — near-black, roughness 0.55 (slightly shiny jacket)
      'fiber'    — yellow, roughness 0.65

    color_variation_seed: if provided, applies a small deterministic colour
    shift seeded from this string (e.g. the cable object name). This gives
    visual variety to cables in the same scene without creating unique materials
    — different seeds produce slightly different hues from the same master.
    Pass "" to disable (all cables identical).

    name:                 Blender material name
    color:                RGB jacket colour (linear space)
    roughness:            surface roughness
    cable_type:           'ethernet' | 'power' | 'fiber'
    color_variation_seed: string to seed per-instance hue shift (optional)
    """
    cable_type = cable_type.lower()
    valid_types = ("ethernet", "power", "fiber")
    if cable_type not in valid_types:
        raise ValueError(f"cable_type must be one of {valid_types}")

    # Preset defaults
    presets = {
        "ethernet": ((0.15, 0.15, 0.15), 0.70),
        "power":    ((0.04, 0.04, 0.04), 0.55),
        "fiber":    ((0.80, 0.65, 0.00), 0.65),
    }
    preset_color, preset_rough = presets[cable_type]
    actual_color    = list(color) if color != (0.15, 0.15, 0.15) else list(preset_color)
    actual_roughness = roughness if roughness != 0.70 else preset_rough

    # Deterministic per-seed hue shift (±5% on each channel)
    if color_variation_seed:
        h = int(hashlib.md5(color_variation_seed.encode()).hexdigest(), 16)
        shift = [(((h >> (i * 8)) & 0xFF) / 255.0 - 0.5) * 0.10 for i in range(3)]
        actual_color = [max(0.0, min(1.0, actual_color[i] + shift[i])) for i in range(3)]

    mat   = _get_or_create_material(name)
    _clear_nodes(mat)
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    out  = _output_node(mat)
    bsdf = _add_principled(mat, location=(0, 0))
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

    _set_bsdf_color(bsdf, actual_color)
    bsdf.inputs['Roughness'].default_value = actual_roughness
    bsdf.inputs['Metallic'].default_value  = 0.0
    # Subsurface for fiber — soft translucency
    if cable_type == "fiber":
        if "Subsurface Weight" in bsdf.inputs:        # Blender 4.x
            bsdf.inputs['Subsurface Weight'].default_value = 0.05
        elif "Subsurface" in bsdf.inputs:             # Blender 3.x
            bsdf.inputs['Subsurface'].default_value = 0.05

    mat["mat_type"]   = "cable"
    mat["cable_type"] = cable_type
    mat["base_color"] = actual_color
    mat["roughness"]  = actual_roughness

    return {
        "material":   name,
        "cable_type": cable_type,
        "color":      actual_color,
        "roughness":  actual_roughness,
        "seeded":     bool(color_variation_seed),
    }


# ── Tool 5: setup_bake_scene ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def setup_bake_scene(
    object_name: str,
    resolution: int = 2048,
    samples: int = 64,
    margin_px: int = 4,
    uv_channel_name: str = "UVMap_Bake",
) -> Dict[str, Any]:
    """
    Prepare a Blender scene for PBR texture baking.

    Steps performed:
    1. Set render engine to Cycles (required for baking).
    2. Set bake samples.
    3. Ensure the object has a UV map named uv_channel_name — creates one via
       Smart UV Project (66° angle limit, 2% island margin) if absent.
    4. Create a blank bake target Image (resolution × resolution, 32-bit float)
       named '<object_name>_BakeTarget' and add a selected Image Texture node
       to the active material pointing at it. Blender bakes to whichever
       Image Texture node is active/selected in the material.

    Call this once before a bake_full_pbr_set run. You can call it again
    with a different resolution before a higher-quality bake pass without
    rebuilding the material.

    object_name:      target mesh object
    resolution:       bake image resolution in pixels (1024 / 2048 / 4096)
    samples:          Cycles bake samples — 64 is fast enough for hard-surface
    margin_px:        UV island bleed margin in pixels (default 4)
    uv_channel_name:  name for the bake UV channel
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    # ── Render engine ──────────────────────────────────────────────────────
    bpy.context.scene.render.engine    = 'CYCLES'
    bpy.context.scene.cycles.samples   = samples
    bpy.context.scene.render.bake.margin = margin_px
    bpy.context.scene.render.bake.use_selected_to_active = False

    # ── UV map ─────────────────────────────────────────────────────────────
    uv_created = False
    if uv_channel_name not in obj.data.uv_layers:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.smart_project(
                angle_limit=1.1519,   # 66 degrees in radians
                island_margin=0.02,
            )
            # Rename the new UV layer
            for layer in obj.data.uv_layers:
                if layer.name not in (uv_channel_name,):
                    layer.name = uv_channel_name
                    break
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')
        uv_created = True

    # Set bake UV as active
    if uv_channel_name in obj.data.uv_layers:
        obj.data.uv_layers[uv_channel_name].active = True
        obj.data.uv_layers[uv_channel_name].active_render = True

    # ── Bake target image ──────────────────────────────────────────────────
    img_name = f"{object_name}_BakeTarget"
    bake_img = bpy.data.images.get(img_name)
    if bake_img:
        bpy.data.images.remove(bake_img)
    bake_img = bpy.data.images.new(
        img_name,
        width=resolution,
        height=resolution,
        alpha=False,
        float_buffer=True,
    )

    # ── Image Texture node in active material ──────────────────────────────
    mat = obj.active_material
    img_node_name = "BAKE_TARGET"
    if mat and mat.use_nodes:
        existing = mat.node_tree.nodes.get(img_node_name)
        if existing:
            mat.node_tree.nodes.remove(existing)
        img_node = mat.node_tree.nodes.new('ShaderNodeTexImage')
        img_node.name     = img_node_name
        img_node.label    = "BAKE TARGET (active)"
        img_node.image    = bake_img
        img_node.location = (-800, 300)
        # Make it the active node — Blender bakes to the active Image Texture
        img_node.select             = True
        mat.node_tree.nodes.active  = img_node
    else:
        _log(f"setup_bake_scene: '{object_name}' has no material — add one before baking")

    return {
        "object":          object_name,
        "resolution":      resolution,
        "samples":         samples,
        "uv_channel":      uv_channel_name,
        "uv_created":      uv_created,
        "bake_image":      img_name,
        "render_engine":   "CYCLES",
    }


# ── Tool 6: bake_full_pbr_set ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def bake_full_pbr_set(
    object_name: str,
    output_dir: str,
    resolution: int = 2048,
    samples: int = 64,
    bake_ao: bool = True,
) -> Dict[str, Any]:
    """
    Bake a full PBR texture set (BaseColor, Normal, Roughness, Metallic, AO)
    for a mesh object in a single call.

    Output files (PNG, 16-bit where applicable):
      <name>_BaseColor.png   — diffuse colour, no lighting
      <name>_Normal_GL.png   — tangent-space normal, OpenGL convention
                               (use apply_baked_textures to auto-flip G for UE5)
      <name>_Roughness.png   — roughness greyscale
      <name>_Metallic.png    — metallic greyscale (via Emit trick, see below)
      <name>_AO.png          — ambient occlusion (if bake_ao=True)

    Metallic bake method:
      Blender has no direct 'Metallic' bake type. This tool creates a temporary
      copy of the active material with the Metallic value plugged into an
      Emission node, bakes as EMIT (which captures any emission-driven value),
      then restores the original material. Works correctly for constant metallic
      values; textur-driven metallic requires manual baking.

    Calls setup_bake_scene internally — no need to call it separately unless
    you need custom UV or resolution settings.

    object_name: source mesh to bake
    output_dir:  directory to write texture files (created if absent)
    resolution:  texture resolution in pixels (default 2048)
    samples:     Cycles bake samples (default 64)
    bake_ao:     whether to bake AO map (adds significant time, default True)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")
    if not obj.active_material:
        raise ValueError(f"'{object_name}' has no active material — assign one first")

    os.makedirs(output_dir, exist_ok=True)

    # Ensure bake scene is ready
    setup_bake_scene(object_name, resolution=resolution, samples=samples)

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in object_name)
    mat  = obj.active_material

    def _save_image(img: bpy.types.Image, path: str) -> None:
        img.filepath_raw = path
        img.file_format  = 'PNG'
        img.save()

    def _fresh_image(suffix: str) -> bpy.types.Image:
        """Create a clean image for each bake pass."""
        iname = f"{object_name}_{suffix}"
        existing = bpy.data.images.get(iname)
        if existing:
            bpy.data.images.remove(existing)
        img = bpy.data.images.new(iname, resolution, resolution,
                                  alpha=False, float_buffer=True)
        # Point the active BAKE_TARGET node to this image
        node = mat.node_tree.nodes.get("BAKE_TARGET")
        if node:
            node.image = img
        return img

    outputs: Dict[str, str] = {}
    errors:  List[str] = []

    # ── BaseColor (Diffuse, colour only — no Direct/Indirect lighting) ─────
    try:
        img = _fresh_image("BaseColor")
        bpy.ops.object.bake(
            type='DIFFUSE',
            pass_filter={'COLOR'},
            use_clear=True,
        )
        path = os.path.join(output_dir, f"{safe}_BaseColor.png")
        _save_image(img, path)
        outputs["basecolor"] = path
    except Exception as exc:
        errors.append(f"BaseColor: {exc}")

    # ── Normal (tangent-space, OpenGL convention — G-channel NOT flipped) ──
    # apply_baked_textures handles the DirectX G-flip for UE5 in the node tree.
    try:
        img = _fresh_image("Normal_GL")
        bpy.context.scene.render.bake.normal_space = 'TANGENT'
        bpy.ops.object.bake(type='NORMAL', use_clear=True)
        path = os.path.join(output_dir, f"{safe}_Normal_GL.png")
        _save_image(img, path)
        outputs["normal"] = path
    except Exception as exc:
        errors.append(f"Normal: {exc}")

    # ── Roughness ──────────────────────────────────────────────────────────
    try:
        img = _fresh_image("Roughness")
        bpy.ops.object.bake(type='ROUGHNESS', use_clear=True)
        path = os.path.join(output_dir, f"{safe}_Roughness.png")
        _save_image(img, path)
        outputs["roughness"] = path
    except Exception as exc:
        errors.append(f"Roughness: {exc}")

    # ── Metallic (via Emit trick — temporary material copy) ────────────────
    # Blender has no native Metallic bake type. We duplicate the material,
    # wire the Metallic socket value into an Emission node, bake EMIT to
    # capture the metallic value as a greyscale image, then restore.
    try:
        metallic_val = mat.node_tree.nodes.get("Principled BSDF")
        metallic_val = (metallic_val.inputs['Metallic'].default_value
                        if metallic_val else 0.0)

        temp_mat = mat.copy()
        temp_mat.name = f"{mat.name}__METALLIC_BAKE"
        _clear_nodes(temp_mat)
        t_nodes = temp_mat.node_tree.nodes
        t_links = temp_mat.node_tree.links

        # Carry over the bake target image node
        t_img_node = t_nodes.new('ShaderNodeTexImage')
        t_img_node.name  = "BAKE_TARGET"
        t_img_node.image = _fresh_image("Metallic")

        t_emit = t_nodes.new('ShaderNodeEmission')
        t_emit.inputs['Strength'].default_value = metallic_val
        t_emit.inputs['Color'].default_value    = (1.0, 1.0, 1.0, 1.0)

        t_out = t_nodes.new('ShaderNodeOutputMaterial')
        t_links.new(t_emit.outputs['Emission'], t_out.inputs['Surface'])
        t_img_node.select     = True
        t_nodes.active        = t_img_node

        mat_slot_idx = obj.active_material_index
        obj.data.materials[mat_slot_idx] = temp_mat
        try:
            bpy.ops.object.bake(type='EMIT', use_clear=True)
        finally:
            obj.data.materials[mat_slot_idx] = mat
            bpy.data.materials.remove(temp_mat, do_unlink=True)

        path = os.path.join(output_dir, f"{safe}_Metallic.png")
        _save_image(bpy.data.images.get(f"{object_name}_Metallic"), path)
        outputs["metallic"] = path
    except Exception as exc:
        errors.append(f"Metallic: {exc}")
        # Ensure material is restored even on unexpected errors
        try:
            if obj.data.materials[obj.active_material_index] != mat:
                obj.data.materials[obj.active_material_index] = mat
        except Exception:
            pass

    # ── AO ────────────────────────────────────────────────────────────────
    if bake_ao:
        try:
            img = _fresh_image("AO")
            bpy.ops.object.bake(type='AO', use_clear=True)
            path = os.path.join(output_dir, f"{safe}_AO.png")
            _save_image(img, path)
            outputs["ao"] = path
        except Exception as exc:
            errors.append(f"AO: {exc}")

    return {
        "object":     object_name,
        "output_dir": output_dir,
        "resolution": resolution,
        "outputs":    outputs,
        "errors":     errors,
        "ready_for_pack_orm": (
            "roughness" in outputs and
            "metallic"  in outputs
        ),
    }


# ── Tool 7: pack_orm_texture ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def pack_orm_texture(
    roughness_path: str,
    metallic_path: str,
    output_path: str,
    ao_path: str = "",
) -> Dict[str, Any]:
    """
    Pack three greyscale maps into a single ORM texture using UE5's convention:
      R = Occlusion   (ao_path; white = no occlusion if ao_path is empty)
      G = Roughness   (roughness_path)
      B = Metallic    (metallic_path)

    UE5's default M_Master material expects this exact channel layout. The
    packed texture plugs directly into a TextureSampleParameter2D node wired
    to the ORM input — no channel remapping needed in UE5.

    Output is saved as a 32-bit float PNG to preserve gradient precision in
    the Roughness and Metallic channels. The file is written to output_path
    (directory is created if absent).

    roughness_path: path to Roughness greyscale PNG (from bake_full_pbr_set)
    metallic_path:  path to Metallic greyscale PNG
    output_path:    destination ORM PNG path
    ao_path:        path to AO greyscale PNG (optional; R=1.0 if omitted)
    """
    for path in (roughness_path, metallic_path):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Source texture not found: {path}")
    if ao_path and not os.path.isfile(ao_path):
        raise FileNotFoundError(f"AO texture not found: {ao_path}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Load source images into Blender's image system
    def _load(path: str, name_hint: str) -> bpy.types.Image:
        # Remove any previously loaded copy to avoid name conflicts
        existing = bpy.data.images.get(name_hint)
        if existing:
            bpy.data.images.remove(existing)
        img = bpy.data.images.load(path)
        img.name = name_hint
        return img

    r_img = _load(roughness_path, "__ORM_R_src")
    m_img = _load(metallic_path,  "__ORM_M_src")
    a_img = _load(ao_path, "__ORM_A_src") if ao_path else None

    w, h = r_img.size[0], r_img.size[1]

    # Validate dimensions match
    if m_img.size[0] != w or m_img.size[1] != h:
        raise ValueError(
            f"Roughness ({w}×{h}) and Metallic "
            f"({m_img.size[0]}×{m_img.size[1]}) dimensions must match"
        )
    if a_img and (a_img.size[0] != w or a_img.size[1] != h):
        raise ValueError(
            f"AO ({a_img.size[0]}×{a_img.size[1]}) must match "
            f"Roughness ({w}×{h}) dimensions"
        )

    # Read pixel arrays — Blender stores as flat [R,G,B,A, R,G,B,A, ...] (0.0–1.0)
    r_px = list(r_img.pixels)
    m_px = list(m_img.pixels)
    a_px = list(a_img.pixels) if a_img else None

    # Build ORM pixel array: R=AO(or 1.0), G=Roughness, B=Metallic, A=1.0
    pixel_count = w * h
    out_px = [0.0] * (pixel_count * 4)
    for i in range(pixel_count):
        base = i * 4
        out_px[base + 0] = a_px[base] if a_px else 1.0    # R = AO
        out_px[base + 1] = r_px[base]                      # G = Roughness
        out_px[base + 2] = m_px[base]                      # B = Metallic
        out_px[base + 3] = 1.0                             # A = unused

    # Write output image
    out_name = "__ORM_packed"
    existing = bpy.data.images.get(out_name)
    if existing:
        bpy.data.images.remove(existing)
    out_img = bpy.data.images.new(out_name, w, h, alpha=False, float_buffer=True)
    out_img.pixels = out_px
    out_img.filepath_raw = output_path
    out_img.file_format  = 'PNG'
    out_img.save()

    # Clean up temp images
    for img in (r_img, m_img, a_img, out_img):
        if img:
            bpy.data.images.remove(img)

    file_kb = os.path.getsize(output_path) // 1024

    return {
        "output_path":   output_path,
        "resolution":    f"{w}×{h}",
        "channels":      {"R": "Occlusion", "G": "Roughness", "B": "Metallic"},
        "ao_included":   bool(ao_path),
        "file_size_kb":  file_kb,
        "ue5_note":      "Plug into TextureSampleParameter2D → ORM input of M_Master",
    }


# ── Tool 8: apply_baked_textures ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def apply_baked_textures(
    object_name: str,
    basecolor_path: str = "",
    normal_path: str = "",
    orm_path: str = "",
    flip_normal_g: bool = True,
) -> Dict[str, Any]:
    """
    Wire baked texture files into a mesh object's active material node tree.

    Creates Image Texture nodes for each provided map and connects them to
    the Principled BSDF inputs. Existing procedural nodes are left in place —
    the texture nodes are added alongside them (non-destructive).

    NORMAL MAP G-CHANNEL (DirectX vs OpenGL):
    ──────────────────────────────────────────
    Blender bakes normal maps in OpenGL convention:
      G channel HIGH (≈1.0) = surface facing UP
    UE5 expects DirectX convention:
      G channel LOW  (≈0.0) = surface facing UP

    When flip_normal_g=True (default), this tool inserts:
      Image Texture → Separate Color → Invert G (Math: 1.0 - G) → Combine Color
      → Normal Map node → Principled BSDF Normal

    This corrects the convention mismatch so both Blender's viewport and UE5
    display correct normals without a separate texture conversion step.

    Set flip_normal_g=False when:
      - You baked with a DX-convention tool (not Blender's native baker)
      - You already converted the texture outside Blender
      - You are debugging and want to see raw bake output

    object_name:    target mesh object
    basecolor_path: path to BaseColor PNG (optional — skip if not baking colour)
    normal_path:    path to Normal PNG in OpenGL convention
    orm_path:       path to ORM packed texture (R=AO, G=Roughness, B=Metallic)
    flip_normal_g:  invert G channel for UE5 DirectX convention (default True)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if not obj.active_material:
        raise ValueError(f"'{object_name}' has no active material")

    mat   = obj.active_material
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    # Find Principled BSDF
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if not bsdf:
        raise ValueError(f"No Principled BSDF found in material '{mat.name}'")

    applied: List[str] = []

    def _img_node(path: str, label: str, colorspace: str) -> bpy.types.Node:
        """Load image and create an Image Texture node."""
        img = bpy.data.images.load(path, check_existing=True)
        node = nodes.new('ShaderNodeTexImage')
        node.image = img
        node.label = label
        if colorspace == 'Non-Color':
            img.colorspace_settings.name = 'Non-Color'
        return node

    # ── BaseColor ──────────────────────────────────────────────────────────
    if basecolor_path and os.path.isfile(basecolor_path):
        existing = next((n for n in nodes if n.label == "BakedBaseColor"), None)
        if existing:
            nodes.remove(existing)
        bc_node = _img_node(basecolor_path, "BakedBaseColor", 'sRGB')
        bc_node.location = (-600, 300)
        links.new(bc_node.outputs['Color'], bsdf.inputs['Base Color'])
        applied.append("basecolor")

    # ── Normal Map ────────────────────────────────────────────────────────
    if normal_path and os.path.isfile(normal_path):
        existing = next((n for n in nodes if n.label == "BakedNormal"), None)
        if existing:
            nodes.remove(existing)

        nm_node = _img_node(normal_path, "BakedNormal", 'Non-Color')
        nm_node.location = (-800, 0)

        if flip_normal_g:
            # ── DirectX G-channel flip ─────────────────────────────────────
            # Blender bakes OpenGL normals (G = up = high value ≈ 1.0).
            # UE5 expects DirectX normals (G = up = low value ≈ 0.0).
            # Fix: invert G channel before the Normal Map node so both
            # Blender viewport and UE5 display correct surface detail.
            #
            # Node graph:
            #   Image Texture
            #       ↓ Color
            #   Separate Color (R / G / B)
            #       G ↓
            #   Math: 1.0 - G  ← inversion
            #       ↓ Value
            #   Combine Color (R, inverted_G, B)
            #       ↓ Color
            #   Normal Map → Principled BSDF Normal

            sep, comb, r_out, g_out, b_out, r_in, g_in, b_in = \
                _separate_combine_nodes(nodes)
            sep.location  = (-580, 0)
            comb.location = (-360, 0)

            invert_g = nodes.new('ShaderNodeMath')
            invert_g.operation = 'SUBTRACT'
            invert_g.location  = (-470, -80)
            invert_g.inputs[0].default_value = 1.0   # 1.0 - G
            invert_g.label = "Invert G (GL→DX)"

            normal_map = nodes.new('ShaderNodeNormalMap')
            normal_map.location = (-200, 0)

            links.new(nm_node.outputs['Color'],      sep.inputs['Color'])
            links.new(sep.outputs[g_out],            invert_g.inputs[1])
            links.new(sep.outputs[r_out],            comb.inputs[r_in])
            links.new(invert_g.outputs['Value'],     comb.inputs[g_in])
            links.new(sep.outputs[b_out],            comb.inputs[b_in])
            links.new(comb.outputs['Color'],         normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'],  bsdf.inputs['Normal'])
        else:
            # No flip — connect directly (use when texture is already DirectX)
            normal_map = nodes.new('ShaderNodeNormalMap')
            normal_map.location = (-200, 0)
            normal_map.label    = "NormalMap (no G-flip)"
            links.new(nm_node.outputs['Color'],     normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])

        applied.append(f"normal (flip_g={flip_normal_g})")

    # ── ORM (R=AO, G=Roughness, B=Metallic) ─────────────────────────────
    if orm_path and os.path.isfile(orm_path):
        existing = next((n for n in nodes if n.label == "BakedORM"), None)
        if existing:
            nodes.remove(existing)

        orm_node = _img_node(orm_path, "BakedORM", 'Non-Color')
        orm_node.location = (-600, -200)

        sep, _, r_out, g_out, b_out, _, _, _ = _separate_combine_nodes(nodes)
        sep.location = (-350, -200)
        links.new(orm_node.outputs['Color'], sep.inputs['Color'])

        # R → AO (if Principled has AO socket; Blender 4.x uses it)
        if "Ambient Occlusion" in bsdf.inputs:
            links.new(sep.outputs[r_out], bsdf.inputs['Ambient Occlusion'])

        # G → Roughness
        links.new(sep.outputs[g_out], bsdf.inputs['Roughness'])

        # B → Metallic
        links.new(sep.outputs[b_out], bsdf.inputs['Metallic'])

        applied.append("orm (R=AO, G=Roughness, B=Metallic)")

    return {
        "object":       object_name,
        "material":     mat.name,
        "applied":      applied,
        "flip_normal_g": flip_normal_g,
        "ue5_note":     "Normal map G-channel flipped for DirectX convention"
                        if flip_normal_g else "Normal map passed through unchanged",
    }


# ── Tool 9: create_material_instance ─────────────────────────────────────

@mcp.tool()
@thread_safe
def create_material_instance(
    source_material: str,
    name: str,
    wear: float = 0.0,
    dust: float = 0.0,
    color_tint: Tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> Dict[str, Any]:
    """
    Create a named variant copy of a master material with parameter overrides.

    Copies the source material's full node tree, then applies the specified
    overrides by modifying node values directly. Override values are stored
    as custom properties on the new material for inspection and further
    instancing.

    wear:        0.0–1.0 — increases roughness noise amplitude on worn surfaces
    dust:        0.0–1.0 — adds a grey-brown additive tint overlay
    color_tint:  RGB offset added to the base colour (e.g. (0.02, 0, 0) = slightly redder)

    Note: this creates a full Blender material copy, not a UE5 Material Instance.
    Each variant bakes to its own texture set if needed. For high-volume variation
    (20+ unique instances), use randomize_material_variation which batches this.

    source_material: name of the master material to copy from
    name:            name for the new variant material
    wear:            additional wear amount (added to source wear_amount)
    dust:            dust overlay intensity
    color_tint:      RGB additive colour offset
    """
    src = bpy.data.materials.get(source_material)
    if not src:
        raise ValueError(f"Source material '{source_material}' not found")

    # Copy the full material
    inst = src.copy()
    inst.name = name

    # ── Apply wear override ────────────────────────────────────────────────
    if wear > 0 and inst.use_nodes:
        nodes = inst.node_tree.nodes
        # Locate the wear multiplier node (inputs[1] = wear contribution cap)
        wear_mult = next(
            (n for n in nodes
             if n.type == 'MATH' and n.operation == 'MULTIPLY'
             and n.inputs[1].default_value <= 0.31),  # was set to wear_amount*0.3
            None,
        )
        if wear_mult:
            current = wear_mult.inputs[1].default_value
            wear_mult.inputs[1].default_value = min(current + wear * 0.30, 0.50)

    # ── Apply dust tint ────────────────────────────────────────────────────
    if dust > 0 and inst.use_nodes:
        nodes = inst.node_tree.nodes
        links = inst.node_tree.links
        bsdf  = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf:
            # Add a Mix node between current Base Color source and BSDF
            dust_color = (0.55 + dust * 0.10, 0.50 + dust * 0.08, 0.42, 1.0)
            try:
                mix = nodes.new('ShaderNodeMix')
                mix.data_type = 'RGBA'
                mix.location  = bsdf.location + __import__('mathutils').Vector((-150, -80))
                mix.inputs[0].default_value   = dust * 0.30  # factor
                mix.inputs['B'].default_value = dust_color
            except Exception:
                mix = nodes.new('ShaderNodeMixRGB')
                mix.location  = (-150, -80)
                mix.inputs[0].default_value        = dust * 0.30
                mix.inputs['Color2'].default_value = dust_color

            # Redirect existing Base Color link through the mix
            bc_input = bsdf.inputs['Base Color']
            if bc_input.links:
                orig_link = bc_input.links[0]
                from_socket = orig_link.from_socket
                links.remove(orig_link)
                try:
                    links.new(from_socket, mix.inputs['A'])
                    links.new(mix.outputs['Result'], bc_input)
                except Exception:
                    links.new(from_socket, mix.inputs['Color1'])
                    links.new(mix.outputs['Color'], bc_input)
            else:
                orig_color = tuple(bc_input.default_value)
                try:
                    mix.inputs['A'].default_value = orig_color
                    links.new(mix.outputs['Result'], bc_input)
                except Exception:
                    mix.inputs['Color1'].default_value = orig_color
                    links.new(mix.outputs['Color'], bc_input)

    # ── Apply colour tint ──────────────────────────────────────────────────
    if any(abs(v) > 1e-6 for v in color_tint) and inst.use_nodes:
        bsdf = next((n for n in inst.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf and not bsdf.inputs['Base Color'].links:
            c = bsdf.inputs['Base Color'].default_value
            bsdf.inputs['Base Color'].default_value = (
                max(0.0, min(1.0, c[0] + color_tint[0])),
                max(0.0, min(1.0, c[1] + color_tint[1])),
                max(0.0, min(1.0, c[2] + color_tint[2])),
                1.0,
            )

    # Inherit source params, then layer overrides
    for k, v in src.items():
        inst[k] = v
    inst["source_material"] = source_material
    inst["wear_override"]   = wear
    inst["dust_override"]   = dust
    inst["color_tint"]      = list(color_tint)

    return {
        "material":        name,
        "source":          source_material,
        "wear":            wear,
        "dust":            dust,
        "color_tint":      list(color_tint),
    }


# ── Tool 10: randomize_material_variation ────────────────────────────────

@mcp.tool()
@thread_safe
def randomize_material_variation(
    collection_name: str,
    source_material: str,
    wear_range: Tuple[float, float] = (0.0, 0.30),
    dust_range: Tuple[float, float] = (0.0, 0.15),
    tint_range: float = 0.03,
    seed: int = 42,
) -> Dict[str, Any]:
    """
    Apply subtle randomised material variants to every mesh object in a collection.

    For each mesh object, creates a unique material instance (via
    create_material_instance) with wear, dust, and colour tint values sampled
    uniformly within the specified ranges. The seed makes runs reproducible —
    the same seed on the same collection always produces the same variation.

    This is the primary tool for making a row of 20 identical 2U servers read
    as individual machines rather than perfect clones. The variation is subtle
    by default (wear 0–30%, dust 0–15%, tint ±3%) so it reads as normal
    manufacturing/environmental variation at game viewing distances.

    collection_name:  collection of mesh objects to vary
    source_material:  master material to create variants from
    wear_range:       (min, max) wear values
    dust_range:       (min, max) dust values
    tint_range:       ±tint_range random shift on each RGB channel
    seed:             random seed for reproducibility
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not bpy.data.materials.get(source_material):
        raise ValueError(f"Source material '{source_material}' not found")

    rng      = _random.Random(seed)
    applied  = []
    errors   = []
    mesh_objs = [o for o in col.all_objects if o.type == 'MESH']

    for obj in mesh_objs:
        wear  = rng.uniform(*wear_range)
        dust  = rng.uniform(*dust_range)
        tint  = tuple(rng.uniform(-tint_range, tint_range) for _ in range(3))
        inst_name = f"{source_material}_var_{obj.name}"

        try:
            result = create_material_instance(
                source_material=source_material,
                name=inst_name,
                wear=wear,
                dust=dust,
                color_tint=tint,
            )
            if obj.data.materials:
                obj.data.materials[0] = bpy.data.materials[inst_name]
            else:
                obj.data.materials.append(bpy.data.materials[inst_name])
            applied.append({
                "object":   obj.name,
                "material": inst_name,
                "wear":     round(wear, 3),
                "dust":     round(dust, 3),
                "tint":     [round(v, 4) for v in tint],
            })
        except Exception as exc:
            errors.append({"object": obj.name, "error": str(exc)})

    return {
        "collection":    collection_name,
        "source":        source_material,
        "applied_count": len(applied),
        "errors":        errors,
        "seed":          seed,
        "applied":       applied,
    }


# ── Tool 11: set_led_state ────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def set_led_state(
    state: str,
    object_name: str = "",
    material_name: str = "",
    color: Optional[Tuple[float, float, float]] = None,
    intensity: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Drive the emissive state on LED materials without creating new materials.

    Locates the 'LED_Emission' node (created by create_led_material) and
    updates its Color and Strength inputs directly. Also updates the 'MixShader'
    factor and the 'led_state' custom property on the material.

    State presets (override color/intensity unless explicitly passed):
      'on'      — full intensity, current stored colour
      'off'     — intensity 0, mix factor 0 (only BSDF surface visible)
      'blink'   — half intensity (actual blink animation = UE5 material param)
      'error'   — red (1, 0.05, 0) at intensity 8.0
      'warning' — amber (1, 0.60, 0) at intensity 4.0

    In UE5, the baked surface texture stays constant. The emissive state is
    driven at runtime by a Material Parameter Collection — the Emission
    Strength value maps to a float parameter named 'LED_Intensity', and
    the Color maps to 'LED_Color'. This tool sets the Blender-side values
    that define the initial/default state baked into the asset.

    state:         'on' | 'off' | 'blink' | 'error' | 'warning'
    object_name:   find LED material via this object's active material
    material_name: drive this specific material by name (alternative to object)
    color:         optional RGB override (uses state preset if None)
    intensity:     optional intensity override (uses state preset if None)
    """
    if not object_name and not material_name:
        raise ValueError("Provide object_name or material_name")

    valid_states = ("on", "off", "blink", "error", "warning")
    state = state.lower()
    if state not in valid_states:
        raise ValueError(f"state must be one of {valid_states}")

    # Resolve material
    if material_name:
        mat = bpy.data.materials.get(material_name)
        if not mat:
            raise ValueError(f"Material '{material_name}' not found")
    else:
        obj = bpy.data.objects.get(object_name)
        if not obj:
            raise ValueError(f"Object '{object_name}' not found")
        mat = obj.active_material
        if not mat:
            raise ValueError(f"'{object_name}' has no active material")

    if not mat.use_nodes:
        raise ValueError(f"Material '{mat.name}' does not use nodes")

    # ── State presets ──────────────────────────────────────────────────────
    stored_color = list(mat.get("led_color", [0.0, 1.0, 0.2]))
    stored_intensity = mat.get("intensity", 3.0)

    presets = {
        "on":      (stored_color,          stored_intensity, 0.60),
        "off":     (stored_color,          0.0,              0.00),
        "blink":   (stored_color,          stored_intensity * 0.5, 0.40),
        "error":   ([1.0, 0.05, 0.0],     8.0,              0.80),
        "warning": ([1.0, 0.60, 0.0],     4.0,              0.70),
    }
    preset_color, preset_intensity, mix_factor = presets[state]

    final_color     = list(color)     if color     is not None else preset_color
    final_intensity = intensity       if intensity is not None else preset_intensity

    nodes = mat.node_tree.nodes

    # ── Update LED_Emission node ───────────────────────────────────────────
    emission_node = nodes.get("LED_Emission")
    if not emission_node:
        emission_node = next(
            (n for n in nodes if n.type == 'EMISSION'),
            None,
        )
    if not emission_node:
        raise ValueError(
            f"No 'LED_Emission' node found in '{mat.name}' — "
            "create the material with create_led_material first"
        )
    emission_node.inputs['Color'].default_value    = (*final_color[:3], 1.0)
    emission_node.inputs['Strength'].default_value = final_intensity

    # ── Update Mix Shader factor ───────────────────────────────────────────
    mix_node = next((n for n in nodes if n.type == 'MIX_SHADER'), None)
    if mix_node:
        mix_node.inputs['Fac'].default_value = mix_factor

    # ── Update stored state ────────────────────────────────────────────────
    mat["led_state"] = state
    mat["led_color"] = final_color
    mat["intensity"] = final_intensity

    return {
        "material":   mat.name,
        "state":      state,
        "color":      final_color,
        "intensity":  final_intensity,
        "mix_factor": mix_factor,
        "ue5_note":   "Maps to Material Parameter Collection 'LED_Intensity' and 'LED_Color'",
    }
