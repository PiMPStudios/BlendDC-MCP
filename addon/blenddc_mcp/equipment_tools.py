"""
Equipment kitbash and rack population tools for the BlendDC asset pipeline.

Provides parametric server, switch, patch panel, and PDU geometry creation,
plus tools to populate rack collections from JSON layouts or named presets.

All equipment origins are placed at front-face-bottom-centre (x=0, y=0, z=0)
to align with snap_to_rack_u positioning from rack_tools.py.

Coordinate convention (matching rack_tools.py):
  X = equipment centreline
  Y = depth  (0 = front face, positive = toward rear)
  Z = height (0 = bottom of equipment, positive = up)
"""

import bpy
import bmesh
import json
import math
import os
import random as _random
from typing import Any, Dict, List, Optional, Tuple

import mathutils

from core import mcp, thread_safe, _log
from constants import (
    RACK_U_M, RACK_U_MM,
    EIA_RAIL_SPAN_M, EIA_RAIL_SPAN_MM,
    EIA_EQUIPMENT_BODY_M,
    RACK_SHEET_THICK_M,
    RACK_BASE_HEIGHT_M,
    RACK_INTERIOR_HEIGHT_M,
    SOCKET_PREFIX,
    QUALITY_TIERS,
)


# ── Local geometry helpers ─────────────────────────────────────────────────
# Copied from rack_tools.py to keep equipment_tools.py self-contained and
# avoid circular imports. Both files are thin wrappers around the same three
# primitives; sharing via a third module would add unnecessary indirection.

def _create_box_object(
    name: str,
    cx: float, cy: float, cz: float,
    w: float, d: float, h: float,
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    """Create a solid box mesh centred at (cx, cy, cz) with dimensions w×d×h."""
    mesh = bpy.data.meshes.new(name)
    obj  = bpy.data.objects.new(name, mesh)
    bm   = bmesh.new()
    scale = mathutils.Matrix.Diagonal((w * 0.5, d * 0.5, h * 0.5, 1.0))
    bmesh.ops.create_cube(bm, size=2.0, matrix=scale)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    obj.location = (cx, cy, cz)
    collection.objects.link(obj)
    return obj


def _set_origin_to(
    obj: bpy.types.Object,
    world_pos: Tuple[float, float, float],
) -> None:
    """Move obj's Blender origin to world_pos using the 3D-cursor trick."""
    import contextlib
    saved = tuple(bpy.context.scene.cursor.location)
    try:
        bpy.context.scene.cursor.location = world_pos
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.origin_set(type='ORIGIN_CURSOR')
    finally:
        with contextlib.suppress(Exception):
            bpy.context.scene.cursor.location = saved


def _add_socket_empty(
    name: str,
    location: Tuple[float, float, float],
    parent: bpy.types.Object,
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    """Create a SOCKET_ empty parented to parent at the given local location."""
    full_name = name if name.startswith(SOCKET_PREFIX) else f"{SOCKET_PREFIX}{name}"
    existing = bpy.data.objects.get(full_name)
    if existing:
        bpy.data.objects.remove(existing, do_unlink=True)
    e = bpy.data.objects.new(full_name, None)
    e.empty_display_type = 'ARROWS'
    e.empty_display_size = 0.015
    e.location = location
    collection.objects.link(e)
    e.parent = parent
    # Parent inverse = identity (parent at world origin when equipment is created)
    # so socket location IS the local offset from equipment origin.
    e.matrix_parent_inverse = parent.matrix_world.inverted()
    return e


def _get_or_create_collection(name: str) -> bpy.types.Collection:
    """Get existing collection or create and link it to the scene root."""
    col = bpy.data.collections.get(name)
    if not col:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return col


def _jitter(value: float, amount: float, enabled: bool) -> float:
    """Return value ± random(amount) when enabled; identity when disabled."""
    if not enabled:
        return value
    return value + _random.uniform(-amount, amount)


def _join_parts(
    parts: List[bpy.types.Object],
    final_name: str,
) -> bpy.types.Object:
    """Join a list of mesh objects, name the result, set origin to (0,0,0)."""
    bpy.ops.object.select_all(action='DESELECT')
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = final_name
    _set_origin_to(joined, (0.0, 0.0, 0.0))
    return joined


# ── Bmesh helpers for photorealistic switch/server geometry ───────────────

def _sw_F(bm: bmesh.types.BMesh, verts: list) -> None:
    """Add a face to bmesh, silently skip on duplicate."""
    try: bm.faces.new(verts)
    except: pass


def _sw_box(bm: bmesh.types.BMesh,
            x0: float, x1: float,
            y0: float, y1: float,
            z0: float, z1: float) -> None:
    """Add a solid box (6 quads) to an existing bmesh."""
    vs = [
        bm.verts.new((x0, y0, z0)), bm.verts.new((x1, y0, z0)),
        bm.verts.new((x1, y1, z0)), bm.verts.new((x0, y1, z0)),
        bm.verts.new((x0, y0, z1)), bm.verts.new((x1, y0, z1)),
        bm.verts.new((x1, y1, z1)), bm.verts.new((x0, y1, z1)),
    ]
    for f in [(0,1,2,3),(4,7,6,5),(0,4,5,1),(3,2,6,7),(0,3,7,4),(1,5,6,2)]:
        try: bm.faces.new([vs[i] for i in f])
        except: pass


def _sw_mesh_obj(
    name: str,
    bm:   "bmesh.types.BMesh",
    col:  bpy.types.Collection,
    mat_name: Optional[str] = None,
) -> bpy.types.Object:
    """Create a mesh object from bmesh, add to collection, assign material."""
    final_name = name
    suffix = 1
    while bpy.data.objects.get(final_name):
        final_name = f"{name}_{suffix:03d}"
        suffix += 1
    me = bpy.data.meshes.new(final_name)
    bm.to_mesh(me); bm.free(); me.update()
    obj = bpy.data.objects.new(final_name, me)
    try: col.objects.link(obj)
    except: pass
    try: bpy.context.scene.collection.objects.unlink(obj)
    except: pass
    if mat_name:
        mat = bpy.data.materials.get(mat_name)
        if mat: obj.data.materials.append(mat)
    return obj


def _sw_holey_plate(
    name: str,
    py: float,
    rect_holes: List[Tuple[float, float, float, float]],
    circ_holes: List[Tuple[float, float, float]],
    col: bpy.types.Collection,
    mat_name: str,
    x_min: float, x_max: float,
    z_min: float, z_max: float,
    outward_plus_y: bool = False,
) -> bpy.types.Object:
    """Flat plate at Y=py with rectangular/circular holes via grid topology."""
    EPS = 1e-6
    xs_s: set = {x_min, x_max}
    zs_s: set = {z_min, z_max}
    for x0, x1, z0, z1 in rect_holes:
        xs_s.update([x0, x1]); zs_s.update([z0, z1])
    NC = 32
    for cx, cz, r in circ_holes:
        for i in range(NC):
            a = 2 * math.pi * i / NC
            xs_s.add(cx + r * math.cos(a)); zs_s.add(cz + r * math.sin(a))
        xs_s.update([cx - r, cx + r]); zs_s.update([cz - r, cz + r])
    xs = sorted(xs_s); zs = sorted(zs_s)
    bm_p = bmesh.new()
    vd: Dict[Tuple[int,int], Any] = {}
    for i, x in enumerate(xs):
        for j, z in enumerate(zs):
            vd[(i, j)] = bm_p.verts.new((x, py, z))
    bm_p.verts.ensure_lookup_table()
    def in_hole(i: int, j: int) -> bool:
        mx = (xs[i] + xs[i+1]) * .5
        mz = (zs[j] + zs[j+1]) * .5
        for x0, x1, z0, z1 in rect_holes:
            if x0 - EPS < mx < x1 + EPS and z0 - EPS < mz < z1 + EPS:
                return True
        for cx, cz, r in circ_holes:
            if (mx - cx)**2 + (mz - cz)**2 < r * r:
                return True
        return False
    for i in range(len(xs) - 1):
        for j in range(len(zs) - 1):
            if not in_hole(i, j):
                v0, v1 = vd[(i, j)], vd[(i+1, j)]
                v2, v3 = vd[(i+1, j+1)], vd[(i, j+1)]
                try:
                    if outward_plus_y: bm_p.faces.new([v0, v3, v2, v1])
                    else:              bm_p.faces.new([v0, v1, v2, v3])
                except: pass
    return _sw_mesh_obj(name, bm_p, col, mat_name)


def _sw_ensure_materials() -> None:
    """Create PBR materials for switch/server photorealistic components."""
    def _pbr(mat_name: str, color: tuple, metallic: float, roughness: float,
             emission: tuple = None, strength: float = 1.0) -> None:
        if bpy.data.materials.get(mat_name): return
        mat = bpy.data.materials.new(mat_name)
        mat.use_nodes = True
        nt = mat.node_tree; nt.nodes.clear()
        out  = nt.nodes.new('ShaderNodeOutputMaterial')
        bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.inputs['Base Color'].default_value = (*color, 1.0)
        bsdf.inputs['Metallic'].default_value   = metallic
        bsdf.inputs['Roughness'].default_value  = roughness
        if emission:
            emit = nt.nodes.new('ShaderNodeEmission')
            emit.inputs['Color'].default_value    = (*emission, 1.0)
            emit.inputs['Strength'].default_value = strength
            mix  = nt.nodes.new('ShaderNodeMixShader')
            mix.inputs['Fac'].default_value = 0.5
            nt.links.new(bsdf.outputs['BSDF'],    mix.inputs[1])
            nt.links.new(emit.outputs['Emission'], mix.inputs[2])
            nt.links.new(mix.outputs['Shader'],    out.inputs['Surface'])
        else:
            nt.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

    _pbr('M_Aluminum',    (0.82, 0.82, 0.80), metallic=1.0, roughness=0.12)
    _pbr('M_BlackMatte',  (0.04, 0.04, 0.04), metallic=0.0, roughness=0.80)
    _pbr('M_DarkGrayMet', (0.12, 0.12, 0.13), metallic=0.8, roughness=0.30)
    _pbr('M_PlasticDark', (0.08, 0.10, 0.12), metallic=0.0, roughness=0.60)
    _pbr('M_Gold',        (1.00, 0.78, 0.28), metallic=1.0, roughness=0.15)
    _pbr('M_SFPCage',     (0.18, 0.18, 0.19), metallic=0.9, roughness=0.20)
    _pbr('M_PortVoid',    (0.01, 0.01, 0.01), metallic=0.0, roughness=0.95)
    _pbr('M_Display',     (0.05, 0.30, 0.70), metallic=0.0, roughness=0.05)
    _pbr('M_LED_Green',   (0.10, 1.00, 0.10), metallic=0.0, roughness=0.30,
          emission=(0.10, 1.00, 0.10), strength=8.0)
    _pbr('M_LED_Amber',   (1.00, 0.55, 0.02), metallic=0.0, roughness=0.30,
          emission=(1.00, 0.55, 0.02), strength=8.0)
    _pbr('M_LED_Off',     (0.06, 0.06, 0.06), metallic=0.0, roughness=0.50)


# ── Tool 1: create_server_chassis ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_server_chassis(
    name: str = "Server",
    u_size: int = 2,
    depth_mm: float = 700.0,
    drive_bays: int = 4,
    data_ports: int = 2,
    collection_name: str = "Equipment",
    random_variation: bool = False,
    quality: str = "high",
) -> Dict[str, Any]:
    """
    Create a parametric server chassis with detailed front bezel geometry.

    Builds a chassis body sized to u_size × EIA span × depth_mm, a proud bezel
    frame (top/bottom strips + right control panel), drive bay surrounds with
    eject handles, EIA mounting ears with screw slots, side ventilation louvres,
    and a rear exhaust tile grille. SOCKET_ empties at rear for UE5 cable routing.

    Depth illusion is achieved through proud geometry (negative-Y boxes sticking
    out from the chassis face) that casts shadows on the chassis surface between
    them — no Booleans required.

    Origin: front-face-bottom-centre (0, 0, 0). Equipment depth: Y=0 (front) to
    Y=depth_mm/1000 (rear).

    name:             base name for all objects
    u_size:           rack unit height (1, 2, or 4)
    depth_mm:         chassis depth in mm (default 700)
    drive_bays:       number of drive bay assemblies on bezel (0–8)
    data_ports:       number of rear SOCKET_Data empties
    collection_name:  Blender collection to add objects to
    random_variation: slightly randomize bay/LED positions and counts
    """
    # ── Quality flags ──────────────────────────────────────────────────────
    qf = QUALITY_TIERS.get(quality, QUALITY_TIERS["high"])

    h  = u_size * RACK_U_M
    w  = EIA_EQUIPMENT_BODY_M   # 446 mm — equipment body slides inside rack posts
    d  = depth_mm / 1000.0
    st = RACK_SHEET_THICK_M

    col   = _get_or_create_collection(collection_name)
    parts: List[bpy.types.Object] = []

    # ── Chassis body — open-front shell ───────────────────────────────────
    # Remove front face so drive bay recesses, bezel frames, and control
    # panel elements are all visible rather than hidden behind solid front.
    chassis = _create_box_object(
        f"{name}_chassis",
        cx=0.0, cy=d / 2, cz=h / 2,
        w=w, d=d, h=h, collection=col,
    )
    _bm_srv = bmesh.new()
    _bm_srv.from_mesh(chassis.data)
    _srv_ff = [f for f in _bm_srv.faces if f.calc_center_median().y < -(d / 2 * 0.97)]
    bmesh.ops.delete(_bm_srv, geom=_srv_ff, context='FACES_ONLY')
    _bm_srv.to_mesh(chassis.data)
    _bm_srv.free()
    chassis.data.update()
    parts.append(chassis)

    actual_bays = 0

    if u_size == 1:
        # ── 1U front face: 2-row bay grid + real control cluster ──────────
        bz_y = -st / 2
        bz_d = st

        if qf["bezel"]:
            # Thin top and bottom bezel strips
            parts.append(_create_box_object(f"{name}_bz_top",
                cx=0.0, cy=bz_y, cz=h - h * 0.045,
                w=w - 0.004, d=bz_d, h=h * 0.07, collection=col))
            parts.append(_create_box_object(f"{name}_bz_bot",
                cx=0.0, cy=bz_y, cz=h * 0.035,
                w=w - 0.004, d=bz_d, h=h * 0.06, collection=col))

        if qf["server_bays"] and drive_bays > 0:
            actual_bays = drive_bays
            if random_variation and drive_bays > 1:
                actual_bays = max(2, drive_bays + _random.randint(-1, 1))
                if actual_bays % 2:
                    actual_bays += 1  # keep even so rows are balanced

            bay_cols = max(1, (actual_bays + 1) // 2)
            bay_rows = 2 if actual_bays > 1 else 1

            bay_left    = -(w * 0.5) + 0.015   # 15 mm from left body edge
            bay_area_w  = w * 0.70              # 70% of body width
            bay_area_h  = h * 0.78              # 78% of chassis height
            bay_area_z0 = (h - bay_area_h) / 2
            bay_cx      = bay_left + bay_area_w / 2
            bay_cz      = bay_area_z0 + bay_area_h / 2

            gap_x = 0.0015
            gap_z = 0.0020

            bay_w = (bay_area_w - gap_x * (bay_cols - 1)) / bay_cols
            bay_h = (bay_area_h - gap_z * (bay_rows - 1)) / bay_rows

            # ── Optimised bay geometry: 1 background plate + separators +
            #    per-carrier face plates.  Replaces per-bay housing/tray/lip
            #    stack (5 objects × N bays) with a shared grid — ~55% fewer tris.
            bg_d = 0.014 if qf.get("deep_bays") else (0.008 if qf["bay_3d"] else 0.004)

            # Single recessed background spanning the whole bay zone
            parts.append(_create_box_object(f"{name}_bay_bg",
                cx=bay_cx, cy=bg_d / 2, cz=bay_cz,
                w=bay_area_w, d=bg_d, h=bay_area_h, collection=col))

            # Vertical separators between columns (fills the gap_x slots)
            for ci in range(1, bay_cols):
                sx = bay_left + ci * (bay_w + gap_x) - gap_x / 2
                parts.append(_create_box_object(f"{name}_bay_vsep_{ci}",
                    cx=sx, cy=bg_d / 2, cz=bay_cz,
                    w=gap_x, d=bg_d + 0.001, h=bay_area_h, collection=col))

            # Horizontal separator between rows
            if bay_rows == 2:
                hs_z = bay_area_z0 + bay_h + gap_z / 2
                parts.append(_create_box_object(f"{name}_bay_hsep",
                    cx=bay_cx, cy=bg_d / 2, cz=hs_z,
                    w=bay_area_w, d=bg_d + 0.001, h=gap_z, collection=col))

            # Individual carrier face plates + per-row handle strip
            for row in range(bay_rows):
                rz = bay_area_z0 + (row + 0.5) * bay_h + row * gap_z
                for col_i in range(bay_cols):
                    idx = row * bay_cols + col_i
                    if idx >= actual_bays:
                        break
                    bx = bay_left + (col_i + 0.5) * bay_w + col_i * gap_x
                    bx = _jitter(bx, 0.0005, random_variation)

                    # Carrier face (thin plate at face level)
                    parts.append(_create_box_object(f"{name}_carr_{idx:02d}",
                        cx=bx, cy=0.0010, cz=rz,
                        w=bay_w - 0.002, d=0.0020, h=bay_h - 0.002, collection=col))

                    if qf["bay_3d"]:
                        # ultra: individual eject handles per bay
                        hdl_cx = bx - (bay_w / 2) + 0.005
                        parts.append(_create_box_object(f"{name}_bay_hdl_{idx:02d}",
                            cx=hdl_cx, cy=-bg_d - 0.003, cz=rz,
                            w=0.004, d=0.003, h=bay_h - 0.004, collection=col))
                        if qf.get("detailed_handles"):
                            # hero: pivot pin stubs at top and bottom of handle
                            for pin_z_off in (-bay_h * 0.44, bay_h * 0.44):
                                parts.append(_create_box_object(
                                    f"{name}_bay_pin_{idx:02d}_{('T' if pin_z_off > 0 else 'B')}",
                                    cx=hdl_cx, cy=-bg_d - 0.0055, cz=rz + pin_z_off,
                                    w=0.006, d=0.002, h=0.003, collection=col))
                            # latch tab (small proud nub on carrier face, right edge)
                            parts.append(_create_box_object(f"{name}_bay_latch_{idx:02d}",
                                cx=bx + (bay_w / 2) - 0.004, cy=-0.0035, cz=rz,
                                w=0.004, d=0.002, h=bay_h * 0.28, collection=col))

                if not qf["bay_3d"] and qf["server_bays"]:
                    # high/medium: one handle rail per row (cheap single box)
                    parts.append(_create_box_object(f"{name}_bay_hdl_row_{row}",
                        cx=bay_left + 0.004, cy=-bg_d - 0.003, cz=rz,
                        w=0.004, d=0.003, h=bay_h - 0.004, collection=col))

            if qf["bezel"]:
                # Activity LED strip — one slim box per row (top-left corner)
                for row in range(bay_rows):
                    lz = bay_area_z0 + (row + 0.5) * bay_h + row * gap_z + bay_h * 0.37
                    parts.append(_create_box_object(f"{name}_bay_led_{row}",
                        cx=bay_left + 0.009, cy=-bg_d - 0.001, cz=lz,
                        w=0.003, d=0.001, h=0.002, collection=col))
                    if qf.get("led_emissive"):
                        # hero: proud lens dome on each LED
                        parts.append(_create_box_object(f"{name}_bay_led_lens_{row}",
                            cx=bay_left + 0.009, cy=-bg_d - 0.0025, cz=lz,
                            w=0.0025, d=0.0015, h=0.0025, collection=col))

        # Right control panel zone
        ctrl_cx = w * 0.385
        ctrl_hw = w * 0.095

        if qf["bezel"]:
            parts.append(_create_box_object(f"{name}_ctrl_panel",
                cx=ctrl_cx, cy=bz_y, cz=h / 2,
                w=ctrl_hw * 2 - 0.002, d=bz_d, h=h * 0.84, collection=col))

            pwr_z = _jitter(h * 0.74, 0.002, random_variation)
            parts.append(_create_box_object(f"{name}_pwr",
                cx=ctrl_cx, cy=-0.003, cz=pwr_z,
                w=0.008, d=0.003, h=0.008, collection=col))
            if qf.get("led_emissive"):
                parts.append(_create_box_object(f"{name}_pwr_lens",
                    cx=ctrl_cx, cy=-0.0048, cz=pwr_z,
                    w=0.006, d=0.0015, h=0.006, collection=col))

            parts.append(_create_box_object(f"{name}_uid",
                cx=ctrl_cx, cy=-0.002, cz=h * 0.57,
                w=0.005, d=0.002, h=0.005, collection=col))
            if qf.get("led_emissive"):
                parts.append(_create_box_object(f"{name}_uid_lens",
                    cx=ctrl_cx, cy=-0.0038, cz=h * 0.57,
                    w=0.004, d=0.0015, h=0.004, collection=col))

            for li, lz_frac in enumerate((0.44, 0.37, 0.30)):
                lz = _jitter(h * lz_frac, 0.001, random_variation)
                parts.append(_create_box_object(f"{name}_sled_{li}",
                    cx=ctrl_cx - ctrl_hw * 0.55, cy=-0.003, cz=lz,
                    w=0.003, d=0.002, h=0.003, collection=col))

            for ui, uz_frac in enumerate((0.20, 0.11)):
                # Outer frame (at face level)
                parts.append(_create_box_object(f"{name}_usb_{ui}_frm",
                    cx=ctrl_cx, cy=-0.0010, cz=h * uz_frac,
                    w=0.013, d=0.0020, h=0.009, collection=col))
                # Recessed inner face
                parts.append(_create_box_object(f"{name}_usb_{ui}_inn",
                    cx=ctrl_cx, cy=0.0050, cz=h * uz_frac,
                    w=0.009, d=0.0025, h=0.005, collection=col))

            # Service tag pull-tab (far left)
            parts.append(_create_box_object(f"{name}_svc_tag",
                cx=-(w * 0.5) + 0.008, cy=-0.001, cz=h / 2,
                w=0.012, d=0.001, h=h * 0.38, collection=col))

        # ── 1U rear face: dual PSUs, PCIe brackets, I/O cluster ──────────
        if qf["bezel"]:
            psu_w = w * 0.175

            for pi, psu_cx in enumerate((
                -(w / 2) + psu_w * 0.5 + 0.005,
                -(w / 2) + psu_w * 1.5 + 0.010,
            )):
                parts.append(_create_box_object(f"{name}_psu_{pi}_face",
                    cx=psu_cx, cy=d + 0.001, cz=h / 2,
                    w=psu_w - 0.003, d=0.002, h=h * 0.90, collection=col))
                parts.append(_create_box_object(f"{name}_psu_{pi}_c14",
                    cx=psu_cx, cy=d + 0.004, cz=h * 0.32,
                    w=0.022, d=0.003, h=0.014, collection=col))
                parts.append(_create_box_object(f"{name}_psu_{pi}_hdl",
                    cx=psu_cx, cy=d + 0.005, cz=h * 0.88,
                    w=psu_w - 0.008, d=0.004, h=h * 0.07, collection=col))
                if qf["bay_3d"]:
                    for vi in range(3):
                        vz = h * 0.52 + vi * h * 0.09
                        parts.append(_create_box_object(f"{name}_psu_{pi}_vent_{vi}",
                            cx=psu_cx, cy=d + 0.003, cz=vz,
                            w=psu_w * 0.78, d=0.0015, h=0.003, collection=col))
                parts.append(_create_box_object(f"{name}_psu_{pi}_led",
                    cx=psu_cx + psu_w * 0.32, cy=d + 0.004, cz=h * 0.82,
                    w=0.004, d=0.003, h=0.004, collection=col))

            pcie_x0      = -(w / 2) + w * 0.38
            pcie_slot_w  = w * 0.14
            for si in range(2):
                sx = pcie_x0 + (si + 0.5) * pcie_slot_w + si * 0.003
                parts.append(_create_box_object(f"{name}_pcie_{si}_brk",
                    cx=sx, cy=d + 0.001, cz=h / 2,
                    w=pcie_slot_w - 0.003, d=0.002, h=h * 0.88, collection=col))
                if qf["bay_3d"]:
                    for vi in range(4):
                        vz = h * 0.12 + vi * h * 0.19
                        parts.append(_create_box_object(f"{name}_pcie_{si}_vent_{vi}",
                            cx=sx, cy=d + 0.002, cz=vz,
                            w=pcie_slot_w * 0.72, d=0.0015, h=0.003, collection=col))

            io_cx = w * 0.30
            for li in range(2):
                lx = _jitter(io_cx + (li - 0.5) * 0.018, 0.001, random_variation)
                parts.append(_create_box_object(f"{name}_rear_lan_{li}",
                    cx=lx, cy=d + 0.004, cz=h * 0.65,
                    w=0.014, d=0.004, h=0.010, collection=col))
            for ui in range(2):
                parts.append(_create_box_object(f"{name}_rear_usb_{ui}",
                    cx=io_cx, cy=d + 0.003, cz=h * (0.44 - ui * 0.16),
                    w=0.009, d=0.003, h=0.005, collection=col))
            parts.append(_create_box_object(f"{name}_rear_vga",
                cx=io_cx + 0.020, cy=d + 0.003, cz=h * 0.54,
                w=0.018, d=0.003, h=0.010, collection=col))
            parts.append(_create_box_object(f"{name}_rear_mgmt",
                cx=io_cx - 0.020, cy=d + 0.003, cz=h * 0.36,
                w=0.010, d=0.003, h=0.007, collection=col))

    elif u_size <= 3:
        # ── 2U / 3U front face ────────────────────────────────────────────
        # Three zones left→right: expansion bays | main drive bay row | control
        # Proportions sum to 1.0 leaving 2.7% margins each side and 0.7% gaps:
        #   0.027 + 0.148 + 0.007 + 0.631 + 0.007 + 0.153 + 0.027 = 1.000
        bz_y = -st / 2
        bz_d = st

        EXPAND_W   = w * 0.148   # ~66 mm  – NVMe / SFF expansion slots
        BAY_ZONE_W = w * 0.631   # ~282 mm – main drive bays (single row)
        CTRL_W     = w * 0.153   # ~68 mm  – power btn, UID, LEDs, USB, tag
        L_MARG     = w * 0.027   # ~12 mm
        ZG         = w * 0.007   # ~3 mm   gap between zones

        EXPAND_X0  = -(w / 2) + L_MARG
        EXPAND_CX  = EXPAND_X0 + EXPAND_W / 2
        BAY_X0     = EXPAND_X0 + EXPAND_W + ZG
        BAY_CX     = BAY_X0 + BAY_ZONE_W / 2
        CTRL_X0    = BAY_X0 + BAY_ZONE_W + ZG
        CTRL_CX    = CTRL_X0 + CTRL_W / 2

        BZ_STRIP_H = h * 0.100   # prominent top/bottom chrome strip
        BAY_H_DIM  = h * 0.740   # carrier face height (between bezel strips)
        BAY_CZ     = h / 2

        if qf["bezel"]:
            # Wide top + bottom bezel strips (the 2U "chrome frame" look)
            parts.append(_create_box_object(f"{name}_bz_bot",
                cx=0.0, cy=bz_y, cz=BZ_STRIP_H / 2,
                w=w - 0.004, d=bz_d, h=BZ_STRIP_H, collection=col))
            parts.append(_create_box_object(f"{name}_bz_top",
                cx=0.0, cy=bz_y, cz=h - BZ_STRIP_H / 2,
                w=w - 0.004, d=bz_d, h=BZ_STRIP_H, collection=col))
            # Lid edge ridge (thin raised bar along very top of chassis lid)
            parts.append(_create_box_object(f"{name}_lid_edge",
                cx=0.0, cy=0.004, cz=h - 0.0015,
                w=w * 0.88, d=0.008, h=0.002, collection=col))
            # Top cable management bar + return lip
            parts.append(_create_box_object(f"{name}_cable_bar",
                cx=0.0, cy=-0.004, cz=h - BZ_STRIP_H * 0.55,
                w=w - 0.010, d=0.006, h=0.004, collection=col))

        # ── Left expansion zone: 2 stacked SFF / NVMe bays ───────────────
        EXP_SLOTS   = 2
        exp_gap     = 0.0025
        exp_slot_h  = (BAY_H_DIM - exp_gap * (EXP_SLOTS - 1)) / EXP_SLOTS
        exp_bg_d    = 0.010 if qf["bay_3d"] else 0.005

        if qf["server_bays"]:
            parts.append(_create_box_object(f"{name}_exp_bg",
                cx=EXPAND_CX, cy=exp_bg_d / 2, cz=BAY_CZ,
                w=EXPAND_W - 0.002, d=exp_bg_d, h=BAY_H_DIM, collection=col))
            for i in range(EXP_SLOTS):
                ez = BAY_CZ - BAY_H_DIM / 2 + (i + 0.5) * exp_slot_h + i * exp_gap
                # Slot carrier face (at face level)
                parts.append(_create_box_object(f"{name}_exp_face_{i}",
                    cx=EXPAND_CX, cy=0.0010, cz=ez,
                    w=EXPAND_W - 0.006, d=0.0020, h=exp_slot_h - 0.002, collection=col))
                if qf["bay_3d"]:
                    # Orange pull-tab at top-right of slot
                    parts.append(_create_box_object(f"{name}_exp_tab_{i}",
                        cx=EXPAND_CX + EXPAND_W * 0.33, cy=-exp_bg_d - 0.003,
                        cz=ez + exp_slot_h * 0.28,
                        w=EXPAND_W * 0.20, d=0.003, h=exp_slot_h * 0.24, collection=col))
                if qf["bezel"]:
                    # Activity LED (top-left of slot face)
                    parts.append(_create_box_object(f"{name}_exp_led_{i}",
                        cx=EXPAND_CX - EXPAND_W * 0.36, cy=-0.002,
                        cz=ez + exp_slot_h * 0.36,
                        w=0.003, d=0.001, h=0.002, collection=col))

        # ── Main drive bay zone: single row, optimised geometry ──────────
        if qf["server_bays"] and drive_bays > 0:
            actual_bays = drive_bays
            if random_variation and drive_bays > 1:
                actual_bays = max(1, drive_bays + _random.randint(-1, 1))

            bg_d    = 0.012 if qf["bay_3d"] else 0.006
            bay_gap = 0.0012
            bay_w   = (BAY_ZONE_W - bay_gap * (actual_bays - 1)) / actual_bays
            hdl_h   = h * 0.068   # handle bar ≈ 6 mm on 2U, scales with height
            hdl_d   = 0.005 if qf["bay_3d"] else 0.003

            # Single recessed background spanning the full bay zone
            parts.append(_create_box_object(f"{name}_bay_bg",
                cx=BAY_CX, cy=bg_d / 2, cz=BAY_CZ,
                w=BAY_ZONE_W, d=bg_d, h=BAY_H_DIM, collection=col))

            # Vertical separators between bay slots
            for ci in range(1, actual_bays):
                sx = BAY_X0 + ci * (bay_w + bay_gap) - bay_gap / 2
                parts.append(_create_box_object(f"{name}_bay_vsep_{ci}",
                    cx=sx, cy=bg_d / 2, cz=BAY_CZ,
                    w=bay_gap, d=bg_d + 0.001, h=BAY_H_DIM, collection=col))

            for i in range(actual_bays):
                bx  = BAY_X0 + (i + 0.5) * bay_w + i * bay_gap
                bx  = _jitter(bx, 0.0008, random_variation)
                cw  = bay_w - 0.0015

                # Carrier face (at face level)
                parts.append(_create_box_object(f"{name}_carr_{i:02d}",
                    cx=bx, cy=0.0010, cz=BAY_CZ,
                    w=cw, d=0.0020, h=BAY_H_DIM - 0.002, collection=col))

                # Eject handle at top of carrier
                hdl_cz = BAY_CZ + BAY_H_DIM / 2 - hdl_h / 2
                parts.append(_create_box_object(f"{name}_hdl_{i:02d}",
                    cx=bx, cy=-bg_d - hdl_d / 2, cz=hdl_cz,
                    w=cw * 0.80, d=hdl_d, h=hdl_h, collection=col))

                if qf["bezel"]:
                    # Activity LED — just below handle
                    led_cz = hdl_cz - hdl_h / 2 - 0.005
                    parts.append(_create_box_object(f"{name}_bay_led_{i:02d}",
                        cx=bx - cw * 0.32, cy=-bg_d - 0.001,
                        cz=led_cz, w=0.004, d=0.002, h=0.003, collection=col))

        # ── Right control zone ────────────────────────────────────────────
        if qf["bezel"]:
            parts.append(_create_box_object(f"{name}_ctrl_bg",
                cx=CTRL_CX, cy=bz_y, cz=BAY_CZ,
                w=CTRL_W - 0.002, d=bz_d, h=h * 0.80, collection=col))

            # Power button (square, upper-centre of zone)
            pwr_cz = _jitter(h * 0.80, 0.003, random_variation)
            parts.append(_create_box_object(f"{name}_pwr",
                cx=CTRL_CX, cy=-0.004, cz=pwr_cz,
                w=0.011, d=0.004, h=0.011, collection=col))

            # UID button (offset right of power)
            parts.append(_create_box_object(f"{name}_uid",
                cx=CTRL_CX + CTRL_W * 0.22, cy=-0.003,
                cz=_jitter(h * 0.68, 0.002, random_variation),
                w=0.007, d=0.003, h=0.007, collection=col))

            # Three status LEDs stacked vertically
            for li, lz_frac in enumerate((0.60, 0.52, 0.44)):
                parts.append(_create_box_object(f"{name}_sled_{li}",
                    cx=CTRL_CX + CTRL_W * 0.28, cy=-0.003,
                    cz=_jitter(h * lz_frac, 0.001, random_variation),
                    w=0.004, d=0.002, h=0.004, collection=col))

            # iDRAC micro-USB port
            parts.append(_create_box_object(f"{name}_idrac_usb",
                cx=CTRL_CX, cy=-0.003, cz=h * 0.33,
                w=0.008, d=0.003, h=0.005, collection=col))

            # SD card slot
            parts.append(_create_box_object(f"{name}_sdcard",
                cx=CTRL_CX - CTRL_W * 0.22, cy=-0.002, cz=h * 0.21,
                w=0.010, d=0.002, h=0.005, collection=col))

            # Service tag pull-tab (left edge of control zone)
            parts.append(_create_box_object(f"{name}_svc_tag",
                cx=CTRL_X0 + 0.006, cy=-0.001, cz=BAY_CZ,
                w=0.010, d=0.001, h=h * 0.38, collection=col))

    else:
        # ── 4U GPU / compute server front face ───────────────────────────
        # Two horizontal sections:
        #   TOP (upper ~47% height): 3-section fan/grille mesh zone
        #   BOT (lower ~49% height): ctrl strip | drive bay row | NVMe zone
        bz_y = -st / 2
        bz_d = st

        # ── Section split heights ────────────────────────────────────────
        BOT_H   = h * 0.490   # drive bay section
        BOT_CZ  = BOT_H / 2
        GRL_Z0  = BOT_H + h * 0.010   # grille starts just above midpoint
        GRL_H   = h * 0.475
        GRL_CZ  = GRL_Z0 + GRL_H / 2

        if qf["bezel"]:
            # Thin bottom bezel strip (industrial frame)
            parts.append(_create_box_object(f"{name}_bz_bot",
                cx=0.0, cy=bz_y, cz=h * 0.022,
                w=w - 0.004, d=bz_d, h=h * 0.042, collection=col))
            # Horizontal separator bar between grille and bay zones
            parts.append(_create_box_object(f"{name}_mid_bar",
                cx=0.0, cy=0.003, cz=BOT_H + h * 0.005,
                w=w - 0.006, d=0.006, h=h * 0.012, collection=col))
            # Top lip / lid edge
            parts.append(_create_box_object(f"{name}_lid_edge",
                cx=0.0, cy=0.005, cz=h - 0.0020,
                w=w * 0.88, d=0.010, h=0.003, collection=col))

        # ── Top grille / fan zone — 3 mesh sections ──────────────────────
        GRL_W     = w * 0.900
        GRL_X0    = -GRL_W / 2
        DIV_W     = 0.005
        N_SECS    = 3
        SEC_W     = (GRL_W - DIV_W * (N_SECS - 1)) / N_SECS
        n_bars    = 7 if qf["bay_3d"] else (5 if qf["vents"] else 3)

        if qf["bezel"]:
            for sec in range(N_SECS):
                sx = GRL_X0 + sec * (SEC_W + DIV_W) + SEC_W / 2
                # Dark background per section
                parts.append(_create_box_object(f"{name}_grl_bg_{sec}",
                    cx=sx, cy=0.002, cz=GRL_CZ,
                    w=SEC_W - 0.002, d=0.006, h=GRL_H - 0.004, collection=col))
                # Horizontal vent bars
                for bi in range(n_bars):
                    bz = GRL_Z0 + h * 0.020 + bi * ((GRL_H - h * 0.040) / n_bars)
                    parts.append(_create_box_object(f"{name}_grl_bar_{sec}_{bi}",
                        cx=sx, cy=-0.001, cz=bz,
                        w=SEC_W * 0.90, d=0.004, h=h * 0.010, collection=col))
            # Vertical dividers between sections
            for dv in range(1, N_SECS):
                dx = GRL_X0 + dv * (SEC_W + DIV_W) - DIV_W / 2
                parts.append(_create_box_object(f"{name}_grl_div_{dv}",
                    cx=dx, cy=0.001, cz=GRL_CZ,
                    w=DIV_W, d=0.008, h=GRL_H, collection=col))

        # ── Bottom bay zone ───────────────────────────────────────────────
        # Layout: [ctrl strip | main bays | NVMe zone]
        CTRL_W_4  = 0.022          # narrow power/LED strip on far left
        NVME_W_4  = 0.058          # 2 specialty NVMe slots on far right
        L_M       = 0.012          # left body margin
        R_M       = 0.012          # right body margin
        ZG4       = 0.003          # zone gap
        BAY_W_4   = w - L_M - CTRL_W_4 - ZG4 - NVME_W_4 - ZG4 - R_M  # ~317 mm

        CTRL_CX_4 = -(w / 2) + L_M + CTRL_W_4 / 2
        BAY_X0_4  = -(w / 2) + L_M + CTRL_W_4 + ZG4
        BAY_CX_4  = BAY_X0_4 + BAY_W_4 / 2
        NVME_X0_4 = BAY_X0_4 + BAY_W_4 + ZG4
        NVME_CX_4 = NVME_X0_4 + NVME_W_4 / 2

        BAY_H_DIM_4 = BOT_H * 0.72
        HDL_H_4     = h * 0.045   # handle bar height

        # Left control strip: power button + status LED
        if qf["bezel"]:
            pwr_cz = _jitter(BOT_H * 0.74, 0.003, random_variation)
            parts.append(_create_box_object(f"{name}_pwr",
                cx=CTRL_CX_4, cy=-0.003, cz=pwr_cz,
                w=0.010, d=0.003, h=0.010, collection=col))
            led_cz = _jitter(BOT_H * 0.52, 0.002, random_variation)
            parts.append(_create_box_object(f"{name}_led",
                cx=CTRL_CX_4, cy=-0.003, cz=led_cz,
                w=0.005, d=0.002, h=0.005, collection=col))
            # UID button below LED
            parts.append(_create_box_object(f"{name}_uid",
                cx=CTRL_CX_4, cy=-0.002, cz=BOT_H * 0.33,
                w=0.006, d=0.002, h=0.006, collection=col))

        # Main drive bay zone — optimised shared geometry
        if qf["server_bays"] and drive_bays > 0:
            actual_bays = drive_bays
            if random_variation and drive_bays > 1:
                actual_bays = max(1, drive_bays + _random.randint(-1, 1))

            bg_d_4  = 0.012 if qf["bay_3d"] else 0.006
            bay_gap = 0.0010
            bay_w   = (BAY_W_4 - bay_gap * (actual_bays - 1)) / actual_bays
            hdl_d   = 0.005 if qf["bay_3d"] else 0.003

            # Single recessed background plate
            parts.append(_create_box_object(f"{name}_bay_bg",
                cx=BAY_CX_4, cy=bg_d_4 / 2, cz=BOT_CZ,
                w=BAY_W_4, d=bg_d_4, h=BAY_H_DIM_4, collection=col))

            # Vertical separators
            for ci in range(1, actual_bays):
                sx4 = BAY_X0_4 + ci * (bay_w + bay_gap) - bay_gap / 2
                parts.append(_create_box_object(f"{name}_bay_vsep_{ci}",
                    cx=sx4, cy=bg_d_4 / 2, cz=BOT_CZ,
                    w=bay_gap, d=bg_d_4 + 0.001, h=BAY_H_DIM_4, collection=col))

            for i in range(actual_bays):
                bx4 = BAY_X0_4 + (i + 0.5) * bay_w + i * bay_gap
                bx4 = _jitter(bx4, 0.0008, random_variation)
                cw4 = bay_w - 0.0015

                # Carrier face (at face level)
                parts.append(_create_box_object(f"{name}_carr_{i:02d}",
                    cx=bx4, cy=0.0010, cz=BOT_CZ,
                    w=cw4, d=0.0020, h=BAY_H_DIM_4 - 0.002, collection=col))

                # Eject handle at BOTTOM of carrier (4U style)
                hdl_cz4 = BOT_CZ - BAY_H_DIM_4 / 2 + HDL_H_4 / 2
                parts.append(_create_box_object(f"{name}_hdl_{i:02d}",
                    cx=bx4, cy=-bg_d_4 - hdl_d / 2, cz=hdl_cz4,
                    w=cw4 * 0.80, d=hdl_d, h=HDL_H_4, collection=col))

                if qf["bezel"]:
                    # Activity LED at TOP of carrier
                    led_cz4 = BOT_CZ + BAY_H_DIM_4 / 2 - 0.005
                    parts.append(_create_box_object(f"{name}_bay_led_{i:02d}",
                        cx=bx4, cy=-bg_d_4 - 0.001, cz=led_cz4,
                        w=0.004, d=0.002, h=0.003, collection=col))

        # Right NVMe specialty zone: 2 stacked slots
        if qf["server_bays"]:
            nvme_slot_h = (BAY_H_DIM_4 - 0.003) / 2
            nvme_bg_d   = 0.010 if qf["bay_3d"] else 0.005
            parts.append(_create_box_object(f"{name}_nvme_bg",
                cx=NVME_CX_4, cy=nvme_bg_d / 2, cz=BOT_CZ,
                w=NVME_W_4 - 0.002, d=nvme_bg_d, h=BAY_H_DIM_4, collection=col))
            for ni in range(2):
                nz = BOT_CZ - BAY_H_DIM_4 / 2 + (ni + 0.5) * nvme_slot_h + ni * 0.003
                parts.append(_create_box_object(f"{name}_nvme_face_{ni}",
                    cx=NVME_CX_4, cy=0.0010, cz=nz,
                    w=NVME_W_4 - 0.006, d=0.0020, h=nvme_slot_h - 0.002, collection=col))
                if qf["bay_3d"]:
                    parts.append(_create_box_object(f"{name}_nvme_tab_{ni}",
                        cx=NVME_CX_4 + NVME_W_4 * 0.30, cy=-nvme_bg_d - 0.003,
                        cz=nz + nvme_slot_h * 0.25,
                        w=NVME_W_4 * 0.22, d=0.003, h=nvme_slot_h * 0.22, collection=col))
                if qf["bezel"]:
                    parts.append(_create_box_object(f"{name}_nvme_led_{ni}",
                        cx=NVME_CX_4 - NVME_W_4 * 0.35, cy=-0.002,
                        cz=nz + nvme_slot_h * 0.35,
                        w=0.003, d=0.001, h=0.002, collection=col))

    # ── Mounting ears — always present at all quality levels ───────────────
    # Total panel = 482.6 mm; body = 446 mm; each ear = (482.6 - 446) / 2 = 18.3 mm
    ear_w = (EIA_RAIL_SPAN_M - EIA_EQUIPMENT_BODY_M) / 2   # 18.3 mm
    ear_d = 0.002   # 2 mm deep
    ear_h = h * 0.68

    for side_sign in (-1, 1):
        side_label = 'L' if side_sign < 0 else 'R'
        ear_cx = side_sign * (w / 2 + ear_w / 2)

        ear_plate = _create_box_object(
            f"{name}_ear_{side_label}",
            cx=ear_cx, cy=-ear_d / 2, cz=h / 2,
            w=ear_w, d=ear_d, h=ear_h,
            collection=col,
        )
        parts.append(ear_plate)

        # Screw slot indicator — raised strip on ear face (high+ quality)
        if qf["bezel"]:
            ear_slot = _create_box_object(
                f"{name}_ear_slot_{side_label}",
                cx=ear_cx, cy=-ear_d + 0.001, cz=h * 0.50,
                w=ear_w * 0.30, d=0.001, h=h * 0.22,
                collection=col,
            )
            parts.append(ear_slot)

        if qf["ear_screws"]:
            # ultra: visible screw-head bumps on ear face (M6 cap screws)
            # hero: four screws with Phillips cross grooves
            screw_fracs = (0.20, 0.42, 0.60, 0.80) if qf.get("detailed_screws") else (0.30, 0.70)
            for screw_frac in screw_fracs:
                screw_y = -ear_d + 0.0018
                parts.append(_create_box_object(
                    f"{name}_ear_screw_{side_label}_{int(screw_frac*100)}",
                    cx=ear_cx, cy=screw_y, cz=h * screw_frac,
                    w=0.006, d=0.002, h=0.006, collection=col))
                if qf.get("detailed_screws"):
                    # Phillips cross: horizontal + vertical thin bars on face
                    for dim in ("H", "V"):
                        parts.append(_create_box_object(
                            f"{name}_ear_screw_{side_label}_{int(screw_frac*100)}_{dim}",
                            cx=ear_cx,
                            cy=screw_y - 0.0008,
                            cz=h * screw_frac,
                            w=0.0035 if dim == "H" else 0.0010,
                            d=0.0008,
                            h=0.0010 if dim == "H" else 0.0035,
                            collection=col))

    if qf["vents"]:
        # ── Side ventilation slots (horizontal louvre strips) ─────────────
        _base_vents = 4 if u_size == 1 else 6
        vent_count  = _base_vents * 2 if qf.get("high_poly_grilles") else _base_vents
        vent_h_dim  = max(0.002, h * (0.022 if qf.get("high_poly_grilles") else 0.038))
        vent_d_len  = d * 0.55
        vent_cy     = d * 0.25 + vent_d_len / 2
        vent_z_start = h * 0.18
        vent_z_span  = h * 0.64
        vent_thick  = 0.0025

        for side_sign in (-1, 1):
            side_label = 'L' if side_sign < 0 else 'R'
            vent_cx = side_sign * (w / 2 + vent_thick / 2)
            denom = vent_count - 1 if vent_count > 1 else 1
            for i in range(vent_count):
                vz = _jitter(vent_z_start + i * (vent_z_span / denom), 0.001, random_variation)
                vent = _create_box_object(
                    f"{name}_vent_{side_label}_{i}",
                    cx=vent_cx, cy=vent_cy, cz=vz,
                    w=vent_thick, d=vent_d_len, h=vent_h_dim,
                    collection=col,
                )
                parts.append(vent)

    if 1 < u_size <= 3 and qf["bezel"]:
        # ── 2U / 3U rear panel: I/O cluster | PCIe zone | fan bay | dual PSU
        # Layout left→right (proportional, sums to w):
        #   0.146 (I/O) + 0.009 + 0.280 (PCIe) + 0.009 + 0.202 (fans) + 0.009 + 0.345 (PSUs)
        _io_w   = w * 0.146   # ~65 mm  I/O cluster
        _pcie_w = w * 0.280   # ~125 mm PCIe bracket zone
        _fan_w  = w * 0.202   # ~90 mm  fan modules
        _psu_w  = w * 0.345   # ~154 mm dual PSU (2 × 74mm + gap)
        _rg     = w * 0.009   # ~4 mm   gap between rear zones
        _io_x0   = -(w / 2)
        _pcie_x0 = _io_x0   + _io_w   + _rg
        _fan_x0  = _pcie_x0 + _pcie_w + _rg
        _psu_x0  = _fan_x0  + _fan_w  + _rg

        # Rear panel base
        parts.append(_create_box_object(f"{name}_rear_panel",
            cx=0.0, cy=d + 0.001, cz=h / 2,
            w=w, d=0.002, h=h, collection=col))

        # ── I/O cluster ─────────────────────────────────────────────────
        _io_cx = _io_x0 + _io_w / 2
        # iDRAC management RJ45 + LED
        parts.append(_create_box_object(f"{name}_io_mgmt",
            cx=_io_cx, cy=d + 0.005, cz=h * 0.82,
            w=0.016, d=0.005, h=0.011, collection=col))
        parts.append(_create_box_object(f"{name}_io_mgmt_led",
            cx=_io_cx + 0.007, cy=d + 0.007, cz=h * 0.82 + 0.005,
            w=0.003, d=0.001, h=0.002, collection=col))
        # 2× 1 GbE LAN (stacked)
        for li in range(2):
            parts.append(_create_box_object(f"{name}_io_lan_{li}",
                cx=_io_cx + (li - 0.5) * 0.020, cy=d + 0.005, cz=h * 0.64,
                w=0.016, d=0.005, h=0.012, collection=col))
        # VGA + DB9 serial (side by side, lower)
        parts.append(_create_box_object(f"{name}_io_vga",
            cx=_io_cx - 0.010, cy=d + 0.004, cz=h * 0.44,
            w=0.020, d=0.004, h=0.013, collection=col))
        parts.append(_create_box_object(f"{name}_io_serial",
            cx=_io_cx + 0.015, cy=d + 0.003, cz=h * 0.44,
            w=0.015, d=0.003, h=0.011, collection=col))
        # 2× USB 3.0 (bottom, side by side)
        for ui in range(2):
            parts.append(_create_box_object(f"{name}_io_usb_{ui}",
                cx=_io_cx + (ui - 0.5) * 0.015, cy=d + 0.004, cz=h * 0.26,
                w=0.012, d=0.004, h=0.006, collection=col))

        # ── PCIe bracket zone ────────────────────────────────────────────
        PCIE_SLOTS  = 3
        _psw        = (_pcie_w - 0.004 * (PCIE_SLOTS - 1)) / PCIE_SLOTS
        for si in range(PCIE_SLOTS):
            sx = _pcie_x0 + (si + 0.5) * _psw + si * 0.004
            parts.append(_create_box_object(f"{name}_pcie_brk_{si}",
                cx=sx, cy=d + 0.001, cz=h / 2,
                w=_psw - 0.003, d=0.002, h=h * 0.90, collection=col))
            if qf["bay_3d"]:
                # Dense horizontal vent bars — more bars at grille quality
                n_bars = 8 if qf["grille"] else 5
                for bi in range(n_bars):
                    bz = h * 0.07 + bi * (h * 0.80 / n_bars)
                    parts.append(_create_box_object(f"{name}_pcie_vent_{si}_{bi}",
                        cx=sx, cy=d + 0.003, cz=bz,
                        w=(_psw - 0.003) * 0.82, d=0.0015, h=0.003, collection=col))

        # ── Fan bay (4 visible fan modules) ──────────────────────────────
        N_FANS    = 4
        _fw       = (_fan_w - 0.003 * (N_FANS - 1)) / N_FANS
        for fi in range(N_FANS):
            fx = _fan_x0 + (fi + 0.5) * _fw + fi * 0.003
            parts.append(_create_box_object(f"{name}_fan_hsg_{fi}",
                cx=fx, cy=d + 0.001, cz=h / 2,
                w=_fw - 0.002, d=0.002, h=h * 0.86, collection=col))
            if qf["bay_3d"]:
                # 4-bar exhaust slats per fan
                _fh = h * 0.72
                for bi in range(4):
                    bz = h / 2 - _fh / 2 + (bi + 0.5) * (_fh / 4)
                    parts.append(_create_box_object(f"{name}_fan_bar_{fi}_{bi}",
                        cx=fx, cy=d + 0.004, cz=bz,
                        w=(_fw - 0.004) * 0.84, d=0.002, h=0.003, collection=col))

        # ── Dual PSU (right side) ────────────────────────────────────────
        PSU_W_EACH  = (_psu_w - 0.005) / 2   # ~74 mm each
        PSU_DEPTH   = 0.065
        for pi in range(2):
            px = _psu_x0 + pi * (PSU_W_EACH + 0.005) + PSU_W_EACH / 2
            # PSU body block
            parts.append(_create_box_object(f"{name}_psu_{pi}_body",
                cx=px, cy=d + PSU_DEPTH / 2, cz=h / 2,
                w=PSU_W_EACH, d=PSU_DEPTH, h=h * 0.93, collection=col))
            # PSU rear face
            parts.append(_create_box_object(f"{name}_psu_{pi}_face",
                cx=px, cy=d + PSU_DEPTH + 0.001, cz=h / 2,
                w=PSU_W_EACH, d=0.002, h=h * 0.93, collection=col))
            # Orange handle bar (top)
            parts.append(_create_box_object(f"{name}_psu_{pi}_hdl",
                cx=px, cy=d + PSU_DEPTH + 0.007, cz=h * 0.88,
                w=PSU_W_EACH * 0.68, d=0.010, h=h * 0.07, collection=col))
            # C14 inlet (lower face)
            parts.append(_create_box_object(f"{name}_psu_{pi}_c14",
                cx=px, cy=d + PSU_DEPTH + 0.003, cz=h * 0.27,
                w=0.024, d=0.004, h=0.016, collection=col))
            # Exhaust grille bars
            _nb = 6 if qf["bay_3d"] else 3
            for bi in range(_nb):
                bz = h * 0.40 + bi * (h * 0.38 / _nb)
                parts.append(_create_box_object(f"{name}_psu_{pi}_vent_{bi}",
                    cx=px, cy=d + PSU_DEPTH + 0.003, cz=bz,
                    w=PSU_W_EACH * 0.80, d=0.0015, h=0.003, collection=col))
            # Status LED (green, top-right of face)
            parts.append(_create_box_object(f"{name}_psu_{pi}_led",
                cx=px + PSU_W_EACH * 0.36, cy=d + PSU_DEPTH + 0.004, cz=h * 0.85,
                w=0.005, d=0.003, h=0.005, collection=col))

        # Rear cable management bar
        parts.append(_create_box_object(f"{name}_rear_cable_bar",
            cx=0.0, cy=d + 0.015, cz=h - 0.006,
            w=w * 0.84, d=0.008, h=0.005, collection=col))

    if u_size >= 4 and qf["bezel"]:
        # ── 4U rear panel: PCIe zone | centre I/O+fans | 2+2 PSU corners ──
        # Upper ~63% height: 8 full-height PCIe slots (GPU server config)
        # Lower ~37% height: PSU corners (2L + 2R) | fan + I/O centre
        PCIE_H      = h * 0.630           # PCIe bracket height
        PCIE_Z0     = h * 0.370           # PCIe zone starts here
        LOW_H       = h * 0.370           # lower zone height
        LOW_CZ      = LOW_H / 2

        N_PCIE      = 8                   # GPU server has 8 full-height slots
        pcie_gap    = 0.003
        pcie_sw     = (w - pcie_gap * (N_PCIE - 1)) / N_PCIE   # ~53 mm each

        # Rear base panel
        parts.append(_create_box_object(f"{name}_rear_panel",
            cx=0.0, cy=d + 0.001, cz=h / 2,
            w=w, d=0.002, h=h, collection=col))

        # ── 8 PCIe bracket faces ─────────────────────────────────────────
        for si in range(N_PCIE):
            sx = -(w / 2) + (si + 0.5) * pcie_sw + si * pcie_gap
            parts.append(_create_box_object(f"{name}_pcie_brk_{si}",
                cx=sx, cy=d + 0.001, cz=PCIE_Z0 + PCIE_H / 2,
                w=pcie_sw - 0.002, d=0.002, h=PCIE_H * 0.96, collection=col))
            if qf["bay_3d"]:
                n_bars = 8 if qf["grille"] else 5
                for bi in range(n_bars):
                    bz = PCIE_Z0 + PCIE_H * 0.05 + bi * (PCIE_H * 0.88 / n_bars)
                    parts.append(_create_box_object(f"{name}_pcie_vent_{si}_{bi}",
                        cx=sx, cy=d + 0.003, cz=bz,
                        w=(pcie_sw - 0.003) * 0.80, d=0.0015, h=0.003, collection=col))

        # ── Lower zone layout ─────────────────────────────────────────────
        # 2 PSUs left | centre (fans + I/O) | 2 PSUs right
        PSU_W_4     = 0.074               # each PSU ~74 mm wide
        PSU_GAP_4   = 0.005               # gap between PSUs in a cluster
        PSU_CLUST   = 2 * PSU_W_4 + PSU_GAP_4   # ~153 mm per side
        CTR_W_4     = w - 2 * PSU_CLUST - 2 * 0.004   # ~136 mm centre
        PSU_L_X0    = -(w / 2)
        CTR_X0_4    = PSU_L_X0 + PSU_CLUST + 0.004
        CTR_CX_4    = CTR_X0_4 + CTR_W_4 / 2
        PSU_R_X0    = CTR_X0_4 + CTR_W_4 + 0.004
        PSU_DEPTH_4 = 0.070

        # 4 PSUs (2 left + 2 right)
        for cluster, x0 in [(0, PSU_L_X0), (1, PSU_R_X0)]:
            for pi in range(2):
                px = x0 + pi * (PSU_W_4 + PSU_GAP_4) + PSU_W_4 / 2
                # PSU body
                parts.append(_create_box_object(f"{name}_psu_{cluster}{pi}_body",
                    cx=px, cy=d + PSU_DEPTH_4 / 2, cz=LOW_CZ,
                    w=PSU_W_4, d=PSU_DEPTH_4, h=LOW_H * 0.94, collection=col))
                # PSU rear face
                parts.append(_create_box_object(f"{name}_psu_{cluster}{pi}_face",
                    cx=px, cy=d + PSU_DEPTH_4 + 0.001, cz=LOW_CZ,
                    w=PSU_W_4, d=0.002, h=LOW_H * 0.94, collection=col))
                # Orange handle (top)
                parts.append(_create_box_object(f"{name}_psu_{cluster}{pi}_hdl",
                    cx=px, cy=d + PSU_DEPTH_4 + 0.007, cz=LOW_H * 0.88,
                    w=PSU_W_4 * 0.65, d=0.010, h=LOW_H * 0.08, collection=col))
                # C20 inlet (larger than C14, ~28×20mm) — lower face
                parts.append(_create_box_object(f"{name}_psu_{cluster}{pi}_c20",
                    cx=px, cy=d + PSU_DEPTH_4 + 0.003, cz=LOW_H * 0.24,
                    w=0.028, d=0.004, h=0.020, collection=col))
                # Exhaust grille bars
                _nb4 = 5 if qf["bay_3d"] else 3
                for bi in range(_nb4):
                    bz = LOW_H * 0.38 + bi * (LOW_H * 0.40 / _nb4)
                    parts.append(_create_box_object(f"{name}_psu_{cluster}{pi}_vent_{bi}",
                        cx=px, cy=d + PSU_DEPTH_4 + 0.003, cz=bz,
                        w=PSU_W_4 * 0.78, d=0.0015, h=0.003, collection=col))
                # Status LED
                parts.append(_create_box_object(f"{name}_psu_{cluster}{pi}_led",
                    cx=px + PSU_W_4 * 0.34, cy=d + PSU_DEPTH_4 + 0.004,
                    cz=LOW_H * 0.84, w=0.005, d=0.003, h=0.005, collection=col))

        # ── Centre zone: 4 fan modules + I/O cluster ─────────────────────
        N_FANS_4  = 4
        FAN_W_4   = (CTR_W_4 * 0.68 - 0.003 * (N_FANS_4 - 1)) / N_FANS_4
        FAN_X0_4  = CTR_X0_4
        FAN_CZ_4  = LOW_H * 0.60

        for fi in range(N_FANS_4):
            fx4 = FAN_X0_4 + (fi + 0.5) * FAN_W_4 + fi * 0.003
            parts.append(_create_box_object(f"{name}_fan_hsg_{fi}",
                cx=fx4, cy=d + 0.001, cz=FAN_CZ_4,
                w=FAN_W_4 - 0.002, d=0.002, h=LOW_H * 0.55, collection=col))
            if qf["bay_3d"]:
                _fh4 = LOW_H * 0.46
                for bi in range(4):
                    bz = FAN_CZ_4 - _fh4 / 2 + (bi + 0.5) * (_fh4 / 4)
                    parts.append(_create_box_object(f"{name}_fan_bar_{fi}_{bi}",
                        cx=fx4, cy=d + 0.004, cz=bz,
                        w=(FAN_W_4 - 0.004) * 0.82, d=0.002, h=0.003, collection=col))

        # I/O cluster — lower portion of centre zone
        IO_CX_4 = CTR_CX_4
        IO_Z_4  = LOW_H * 0.25   # I/O sits low in the zone
        # IPMI management + 2× LAN
        parts.append(_create_box_object(f"{name}_io_ipmi",
            cx=IO_CX_4 - 0.022, cy=d + 0.005, cz=IO_Z_4 + 0.014,
            w=0.015, d=0.005, h=0.011, collection=col))
        for li in range(2):
            parts.append(_create_box_object(f"{name}_io_lan_{li}",
                cx=IO_CX_4 + (li - 0.5) * 0.020, cy=d + 0.005, cz=IO_Z_4 + 0.014,
                w=0.016, d=0.005, h=0.012, collection=col))
        # VGA + 4× USB 3.0
        parts.append(_create_box_object(f"{name}_io_vga",
            cx=IO_CX_4 - 0.012, cy=d + 0.004, cz=IO_Z_4 - 0.004,
            w=0.020, d=0.004, h=0.013, collection=col))
        for ui in range(4):
            parts.append(_create_box_object(f"{name}_io_usb_{ui}",
                cx=IO_CX_4 + 0.005 + (ui - 1.5) * 0.014, cy=d + 0.004,
                cz=IO_Z_4 - 0.018, w=0.012, d=0.004, h=0.006, collection=col))

        # Rear cable management bar
        parts.append(_create_box_object(f"{name}_rear_cable_bar",
            cx=0.0, cy=d + 0.015, cz=h - 0.006,
            w=w * 0.84, d=0.008, h=0.005, collection=col))

    # ── Hero: chamfer strips at key chassis edges (proper_bevels) ────────────
    if qf.get("proper_bevels"):
        bevel_t = 0.0015   # 1.5 mm chamfer strip thickness
        # Top-front edge
        parts.append(_create_box_object(f"{name}_bvl_top_front",
            cx=0.0, cy=-bevel_t / 2, cz=h - bevel_t / 2,
            w=w - 0.004, d=bevel_t, h=bevel_t, collection=col))
        # Bottom-front edge
        parts.append(_create_box_object(f"{name}_bvl_bot_front",
            cx=0.0, cy=-bevel_t / 2, cz=bevel_t / 2,
            w=w - 0.004, d=bevel_t, h=bevel_t, collection=col))
        # Top-rear edge
        parts.append(_create_box_object(f"{name}_bvl_top_rear",
            cx=0.0, cy=d + bevel_t / 2, cz=h - bevel_t / 2,
            w=w - 0.004, d=bevel_t, h=bevel_t, collection=col))

    # ── Join + origin ──────────────────────────────────────────────────────
    joined = _join_parts(parts, name)

    # ── Per-server material variation ─────────────────────────────────────
    # Always applied: keeps each chassis slightly unique even without full
    # random_variation. Full range (colour + sheen) when random_variation=True;
    # narrow roughness/metallic-only shift when False.
    _var_mat = bpy.data.materials.new(f"{name}_var")
    _var_mat.use_nodes = True
    _var_bsdf = _var_mat.node_tree.nodes.get("Principled BSDF")
    if _var_bsdf:
        if random_variation:
            _base = 0.07 + _random.uniform(-0.018, 0.022)
            _var_bsdf.inputs["Base Color"].default_value = (
                max(0.03, _base + _random.uniform(-0.008, 0.008)),
                max(0.03, _base + _random.uniform(-0.008, 0.008)),
                max(0.03, _base + _random.uniform(-0.008, 0.012)),
                1.0,
            )
            _var_bsdf.inputs["Roughness"].default_value = max(0.35, min(0.75,
                0.50 + _random.uniform(-0.10, 0.15)))
            _var_bsdf.inputs["Metallic"].default_value = max(0.50, min(0.85,
                0.70 + _random.uniform(-0.10, 0.08)))
        else:
            # Light wear/dirt: tiny darkening bias + dust-roughness upshift
            _dirt = _random.uniform(0.0, 0.020)
            _var_bsdf.inputs["Base Color"].default_value = (
                max(0.03, 0.070 - _dirt),
                max(0.03, 0.070 - _dirt),
                max(0.03, 0.080 - _dirt * 0.6),
                1.0,
            )
            _var_bsdf.inputs["Roughness"].default_value = max(0.42, min(0.68,
                0.53 + _random.uniform(-0.05, 0.07)))   # dust bias: mean +0.03
            _var_bsdf.inputs["Metallic"].default_value = max(0.58, min(0.78,
                0.68 + _random.uniform(-0.05, 0.05))
            )
    # Replace slot 0 so all faces (currently on the default slot) pick it up
    if joined.data.materials:
        joined.data.materials[0] = _var_mat
    else:
        joined.data.materials.append(_var_mat)

    # ── SOCKET_ empties parented to joined chassis ─────────────────────────
    sockets_created: List[str] = []

    pwr = _add_socket_empty(
        f"{name}_Power",
        location=(w * 0.35, d, h * 0.50),
        parent=joined, collection=col,
    )
    sockets_created.append(pwr.name)

    for i in range(data_ports):
        px = _jitter(-w * 0.30 + i * (w * 0.15), 0.005, random_variation)
        dp = _add_socket_empty(
            f"{name}_Data_{i:02d}",
            location=(px, d, h * 0.50),
            parent=joined, collection=col,
        )
        sockets_created.append(dp.name)

    joined["equipment_type"]   = "server"
    joined["u_size"]           = u_size
    joined["depth_mm"]         = depth_mm
    joined["random_variation"] = random_variation
    joined["quality"]          = quality

    return {
        "object":      name,
        "collection":  collection_name,
        "u_size":      u_size,
        "depth_mm":    depth_mm,
        "drive_bays":  actual_bays,
        "sockets":     sockets_created,
        "origin":      "front-face-bottom-centre (0, 0, 0)",
    }


# ── Tool 2: create_network_switch ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_network_switch(
    name: str = "Switch",
    u_size: int = 1,
    port_count: int = 48,
    collection_name: str = "Equipment",
    random_variation: bool = False,
    quality: str = "high",
    join_mesh: bool = True,
) -> Dict[str, Any]:
    """
    Create a 1U managed network switch with photorealistic bmesh geometry.

    Builds a complete hollow-shell switch with real RJ45 tunnel geometry,
    SFP+ cage tubes, swept fan blades, IEC C14 inlet with blade contacts,
    LCD display, top louvers, and side vents — all in centred coordinates,
    then translated to equipment-tools convention (origin = front-face-bottom-centre).

    name:             base name
    u_size:           rack unit height (1 or 2)
    port_count:       front-face data ports (24 or 48)
    collection_name:  Blender collection
    random_variation: when True use random seed for LED states; False = seed(42)
    quality:          parameter accepted for API compatibility; all tiers get full geometry
    join_mesh:        when True join all parts into one mesh; False parents them
    """
    import random as _rng
    if random_variation:
        _rng.seed(None)
    else:
        _rng.seed(42)

    # ── Chassis dimensions ─────────────────────────────────────────────────
    h  = u_size * RACK_U_M        # 44.45 mm per U
    w  = EIA_EQUIPMENT_BODY_M     # 446 mm (EIA body width, not rail span)
    d  = 0.280                    # 280 mm depth
    HW = w / 2
    HH = h / 2
    FRONT_Y = -d / 2              # -0.140
    BACK_Y  =  d / 2              #  0.140

    col   = _get_or_create_collection(collection_name)
    parts: List[bpy.types.Object] = []

    # Ensure all switch PBR materials exist
    _sw_ensure_materials()

    # ── RJ45 port constants ────────────────────────────────────────────────
    OW, OH  = 0.01600, 0.01180
    OD      = 0.01600
    WALL    = 0.00140
    IW      = OW - 2 * WALL       # 0.01320
    IH      = OH - 2 * WALL       # 0.00900
    CHAM    = 0.00048
    PORT_PROTRUDE = 0.00150
    PORT_FRONT_Y  = FRONT_Y - PORT_PROTRUDE   # -0.14150

    GAP_X   = 0.00115
    GRP_GAP = 0.00460
    G_SIZE  = 6
    N_GROUPS = 4 if port_count >= 48 else 2
    PORT_ZONE_CX = 0.0115
    single_grp_w  = G_SIZE * OW + (G_SIZE - 1) * GAP_X
    total_ports_w = N_GROUPS * single_grp_w + (N_GROUPS - 1) * GRP_GAP
    port_left_edge = PORT_ZONE_CX - total_ports_w / 2

    Z_OFF    = -0.0008
    P_ROW_GAP = 0.00400
    Z_UPPER  =  (OH / 2 + P_ROW_GAP / 2) + Z_OFF
    Z_LOWER  = -(OH / 2 + P_ROW_GAP / 2) + Z_OFF

    # ── SFP+ cage constants ────────────────────────────────────────────────
    SFP_OW, SFP_OH = 0.01520, 0.01150
    SFP_WALL       = 0.00150
    SFP_IW         = SFP_OW - 2 * SFP_WALL
    SFP_IH         = SFP_OH - 2 * SFP_WALL
    SFP_DEPTH      = 0.03700
    SFP_MOUTH_Y    = PORT_FRONT_Y
    SFP_BACK_Y     = SFP_MOUTH_Y + SFP_DEPTH
    SFP_CAGES_DEF  = [
        (0.1826, 0.1978,  0.0007,  0.0122),
        (0.2006, 0.2158,  0.0007,  0.0122),
        (0.1826, 0.1978, -0.0138, -0.0023),
        (0.2006, 0.2158, -0.0138, -0.0023),
    ]

    # ── Fan constants ──────────────────────────────────────────────────────
    FAN_SHROUD_R = 0.02175
    FAN_HOLE_R   = 0.02050
    FAN1_CX, FAN2_CX = 0.19800, 0.15000
    FAN_CZ       = 0.00160
    FAN_DUCT_D   = 0.02800

    # ── IEC C14 constants ──────────────────────────────────────────────────
    IEC_CX, IEC_CZ   = -0.1880, -0.0028
    IEC_CUT_W, IEC_CUT_H = 0.0280, 0.0220
    IEC_FLG_W, IEC_FLG_H = 0.0390, 0.0310
    IEC_SOCK_D   = 0.0200
    IEC_FLG_T    = 0.0025

    # ── Rear RJ45 port constants ───────────────────────────────────────────
    REAR_PORTS = [
        {'cx': -0.1500, 'cz': -0.0018},
        {'cx': -0.1260, 'cz': -0.0018},
    ]
    REAR_OW, REAR_OH = 0.01600, 0.01180
    REAR_PROTRUDE    = 0.00150
    REAR_MOUTH_Y     = BACK_Y + REAR_PROTRUDE
    REAR_DEEP_Y      = REAR_MOUTH_Y - 0.01600

    # ─────────────────────────────────────────────────────────────────────
    # CHASSIS: 5-sided open-front shell (back, top, bottom, left, right)
    # ─────────────────────────────────────────────────────────────────────
    bm_ch = bmesh.new()
    def _quad(v0, v1, v2, v3):
        _sw_F(bm_ch, [bm_ch.verts.new(v0), bm_ch.verts.new(v1),
                      bm_ch.verts.new(v2), bm_ch.verts.new(v3)])
    _quad((-HW, BACK_Y, -HH), ( HW, BACK_Y, -HH), ( HW, BACK_Y,  HH), (-HW, BACK_Y,  HH))  # back
    _quad((-HW, FRONT_Y, HH), ( HW, FRONT_Y,  HH), ( HW, BACK_Y,  HH), (-HW, BACK_Y,  HH))  # top
    _quad((-HW, FRONT_Y,-HH), (-HW, BACK_Y,  -HH), ( HW, BACK_Y, -HH), ( HW, FRONT_Y, -HH)) # bottom
    _quad((-HW, FRONT_Y,-HH), (-HW, FRONT_Y,  HH), (-HW, BACK_Y,  HH), (-HW, BACK_Y,  -HH)) # left
    _quad(( HW, FRONT_Y,-HH), ( HW, BACK_Y,  -HH), ( HW, BACK_Y,  HH), ( HW, FRONT_Y,  HH)) # right
    parts.append(_sw_mesh_obj(f"{name}_chassis", bm_ch, col, 'M_Aluminum'))

    # ─────────────────────────────────────────────────────────────────────
    # FRONT PLATE: aluminium plate with 48 RJ45 + 4 SFP+ holes
    # ─────────────────────────────────────────────────────────────────────
    fp_holes = []
    for g in range(N_GROUPS):
        gx = port_left_edge + g * (single_grp_w + GRP_GAP)
        for p in range(G_SIZE):
            x0 = gx + p * (OW + GAP_X)
            x1 = x0 + OW
            fp_holes += [
                (x0, x1, Z_UPPER - OH/2, Z_UPPER + OH/2),
                (x0, x1, Z_LOWER - OH/2, Z_LOWER + OH/2),
            ]
    for x0, x1, z0, z1 in SFP_CAGES_DEF:
        fp_holes.append((x0, x1, z0, z1))
    parts.append(_sw_holey_plate(
        f"{name}_front_plate", FRONT_Y,
        fp_holes, [],
        col, 'M_Aluminum',
        x_min=-HW, x_max=HW, z_min=-HH, z_max=HH,
    ))

    # ─────────────────────────────────────────────────────────────────────
    # RJ45 PORT HOUSINGS + GOLD CONTACTS
    # ─────────────────────────────────────────────────────────────────────
    bm_h = bmesh.new()
    bm_c = bmesh.new()
    for g in range(N_GROUPS):
        gx = port_left_edge + g * (single_grp_w + GRP_GAP)
        for p in range(G_SIZE):
            px = gx + p * (OW + GAP_X) + OW / 2
            for pz in [Z_UPPER, Z_LOWER]:
                py0 = PORT_FRONT_Y
                py1 = PORT_FRONT_Y + OD
                om = [bm_h.verts.new((px - OW/2, py0, pz - OH/2)),
                      bm_h.verts.new((px + OW/2, py0, pz - OH/2)),
                      bm_h.verts.new((px + OW/2, py0, pz + OH/2)),
                      bm_h.verts.new((px - OW/2, py0, pz + OH/2))]
                im = [bm_h.verts.new((px - IW/2 + CHAM, py0, pz - IH/2 + CHAM)),
                      bm_h.verts.new((px + IW/2 - CHAM, py0, pz - IH/2 + CHAM)),
                      bm_h.verts.new((px + IW/2 - CHAM, py0, pz + IH/2 - CHAM)),
                      bm_h.verts.new((px - IW/2 + CHAM, py0, pz + IH/2 - CHAM))]
                od = [bm_h.verts.new((px - OW/2, py1, pz - OH/2)),
                      bm_h.verts.new((px + OW/2, py1, pz - OH/2)),
                      bm_h.verts.new((px + OW/2, py1, pz + OH/2)),
                      bm_h.verts.new((px - OW/2, py1, pz + OH/2))]
                ib = [bm_h.verts.new((px - IW/2, py1 - WALL, pz - IH/2)),
                      bm_h.verts.new((px + IW/2, py1 - WALL, pz - IH/2)),
                      bm_h.verts.new((px + IW/2, py1 - WALL, pz + IH/2)),
                      bm_h.verts.new((px - IW/2, py1 - WALL, pz + IH/2))]
                # Front frame
                _sw_F(bm_h, [om[0], om[1], im[1], im[0]])
                _sw_F(bm_h, [om[2], om[3], im[3], im[2]])
                _sw_F(bm_h, [om[3], om[0], im[0], im[3]])
                _sw_F(bm_h, [om[1], om[2], im[2], im[1]])
                # Outer sides
                _sw_F(bm_h, [om[0], od[0], od[1], om[1]])
                _sw_F(bm_h, [om[2], od[2], od[3], om[3]])
                _sw_F(bm_h, [om[3], od[3], od[2], om[2]])
                _sw_F(bm_h, [om[3], om[0], od[0], od[3]])
                _sw_F(bm_h, [om[1], od[1], od[2], om[2]])
                # Outer back
                _sw_F(bm_h, [od[0], od[3], od[2], od[1]])
                # Inner tunnel walls
                _sw_F(bm_h, [im[0], im[1], ib[1], ib[0]])
                _sw_F(bm_h, [im[2], im[3], ib[3], ib[2]])
                _sw_F(bm_h, [im[3], im[0], ib[0], ib[3]])
                _sw_F(bm_h, [im[1], im[2], ib[2], ib[1]])
                _sw_F(bm_h, [ib[0], ib[1], ib[2], ib[3]])
                # 8 gold contact pins
                N_PINS = 8
                pin_y0 = py1 - WALL + 0.0002
                pin_y1 = pin_y0 + 0.0003
                pin_z0 = pz - IH/2 + 0.001
                pin_spacing = IW / (N_PINS + 1)
                for pi in range(N_PINS):
                    ppx = (px - IW/2) + (pi + 1) * pin_spacing
                    _sw_box(bm_c, ppx - 0.0003, ppx + 0.0003, pin_y0, pin_y1,
                            pin_z0, pin_z0 + 0.0011)
    parts.append(_sw_mesh_obj(f"{name}_port_housings", bm_h, col, 'M_PlasticDark'))
    bm_c.verts.ensure_lookup_table()
    n_contacts = N_GROUPS * G_SIZE * 2 * 8
    for i in range(n_contacts):
        b = i * 8
        vs_c = bm_c.verts[b:b+8]
        for f in [(0,1,2,3),(4,7,6,5),(0,4,5,1),(3,2,6,7),(0,3,7,4),(1,5,6,2)]:
            try: bm_c.faces.new([vs_c[j] for j in f])
            except: pass
    parts.append(_sw_mesh_obj(f"{name}_port_contacts", bm_c, col, 'M_Gold'))

    # ─────────────────────────────────────────────────────────────────────
    # LEDs — per-port above each RJ45
    # ─────────────────────────────────────────────────────────────────────
    led_groups: Dict[str, list] = {'M_LED_Green': [], 'M_LED_Amber': [], 'M_LED_Off': []}
    LED_W, LED_H_dim, LED_D_dim = 0.00230, 0.00220, 0.00100
    LED_Z_OFFSET = OH / 2 + 0.00250
    for g in range(N_GROUPS):
        gx = port_left_edge + g * (single_grp_w + GRP_GAP)
        for p in range(G_SIZE):
            px = gx + p * (OW + GAP_X) + OW / 2
            for pz in [Z_UPPER, Z_LOWER]:
                lz = pz + LED_Z_OFFSET
                ly = PORT_FRONT_Y - 0.0002
                r = _rng.random()
                mat_l = 'M_LED_Green' if r < 0.55 else 'M_LED_Amber' if r < 0.80 else 'M_LED_Off'
                led_groups[mat_l].append((px, ly, lz))
    for mat_name_l, positions in led_groups.items():
        if not positions:
            continue
        bm_l = bmesh.new()
        for (lx, ly, lz) in positions:
            _sw_box(bm_l, lx - LED_W/2, lx + LED_W/2,
                    ly - LED_D_dim, ly,
                    lz - LED_H_dim/2, lz + LED_H_dim/2)
        parts.append(_sw_mesh_obj(
            f"{name}_leds_{mat_name_l.replace('M_LED_', '').lower()}",
            bm_l, col, mat_name_l))

    # ─────────────────────────────────────────────────────────────────────
    # SFP+ CAGES: hollow tube shells + connector + contacts + guide rails
    # ─────────────────────────────────────────────────────────────────────
    for ci_sfp, (x0, x1, z0, z1) in enumerate(SFP_CAGES_DEF, 1):
        cx_sfp = (x0 + x1) / 2
        cz_sfp = (z0 + z1) / 2
        ix0 = cx_sfp - SFP_IW / 2;  ix1 = cx_sfp + SFP_IW / 2
        iz0 = cz_sfp - SFP_IH / 2;  iz1 = cz_sfp + SFP_IH / 2
        bm_sfp = bmesh.new()
        om_s = [bm_sfp.verts.new((x0, SFP_MOUTH_Y, z0)),
                bm_sfp.verts.new((x1, SFP_MOUTH_Y, z0)),
                bm_sfp.verts.new((x1, SFP_MOUTH_Y, z1)),
                bm_sfp.verts.new((x0, SFP_MOUTH_Y, z1))]
        im_s = [bm_sfp.verts.new((ix0, SFP_MOUTH_Y, iz0)),
                bm_sfp.verts.new((ix1, SFP_MOUTH_Y, iz0)),
                bm_sfp.verts.new((ix1, SFP_MOUTH_Y, iz1)),
                bm_sfp.verts.new((ix0, SFP_MOUTH_Y, iz1))]
        ob_s = [bm_sfp.verts.new((x0, SFP_BACK_Y, z0)),
                bm_sfp.verts.new((x1, SFP_BACK_Y, z0)),
                bm_sfp.verts.new((x1, SFP_BACK_Y, z1)),
                bm_sfp.verts.new((x0, SFP_BACK_Y, z1))]
        ib_s = [bm_sfp.verts.new((ix0, SFP_BACK_Y, iz0)),
                bm_sfp.verts.new((ix1, SFP_BACK_Y, iz0)),
                bm_sfp.verts.new((ix1, SFP_BACK_Y, iz1)),
                bm_sfp.verts.new((ix0, SFP_BACK_Y, iz1))]
        _sw_F(bm_sfp, [om_s[0], om_s[1], im_s[1], im_s[0]])
        _sw_F(bm_sfp, [om_s[2], om_s[3], im_s[3], im_s[2]])
        _sw_F(bm_sfp, [om_s[3], om_s[0], im_s[0], im_s[3]])
        _sw_F(bm_sfp, [om_s[1], om_s[2], im_s[2], im_s[1]])
        _sw_F(bm_sfp, [om_s[0], ob_s[0], ob_s[1], om_s[1]])
        _sw_F(bm_sfp, [om_s[2], ob_s[2], ob_s[3], om_s[3]])
        _sw_F(bm_sfp, [om_s[3], ob_s[3], ob_s[2], om_s[2]])
        _sw_F(bm_sfp, [om_s[3], om_s[0], ob_s[0], ob_s[3]])
        _sw_F(bm_sfp, [om_s[1], ob_s[1], ob_s[2], om_s[2]])
        _sw_F(bm_sfp, [ob_s[0], ob_s[3], ob_s[2], ob_s[1]])
        _sw_F(bm_sfp, [im_s[0], im_s[1], ib_s[1], ib_s[0]])
        _sw_F(bm_sfp, [im_s[2], im_s[3], ib_s[3], ib_s[2]])
        _sw_F(bm_sfp, [im_s[3], im_s[0], ib_s[0], ib_s[3]])
        _sw_F(bm_sfp, [im_s[1], im_s[2], ib_s[2], ib_s[1]])
        _sw_F(bm_sfp, [ib_s[0], ib_s[1], ib_s[2], ib_s[3]])
        parts.append(_sw_mesh_obj(f"{name}_sfp_cage_{ci_sfp}", bm_sfp, col, 'M_SFPCage'))

    # SFP connector bodies
    CON_Y0 = SFP_BACK_Y - 0.00150
    CON_Y1 = SFP_BACK_Y - 0.00030
    bm_con = bmesh.new()
    for x0, x1, z0, z1 in SFP_CAGES_DEF:
        cx_sfp = (x0 + x1) / 2; cz_sfp = (z0 + z1) / 2
        _sw_box(bm_con, cx_sfp - SFP_IW/2, cx_sfp + SFP_IW/2,
                CON_Y0, CON_Y1,
                cz_sfp - SFP_IH/2*0.88, cz_sfp + SFP_IH/2*0.88)
    parts.append(_sw_mesh_obj(f"{name}_sfp_connectors", bm_con, col, 'M_BlackMatte'))

    # SFP gold contacts
    PIN_Y0 = SFP_BACK_Y - 0.00200
    PIN_Y1 = CON_Y0
    N_SFP_C = 10; CW2 = 0.00080; CH2 = 0.00090; ROW_OFF = 0.00200
    bm_sfp_pins = bmesh.new()
    for x0, x1, z0, z1 in SFP_CAGES_DEF:
        cx_sfp = (x0 + x1) / 2; cz_sfp = (z0 + z1) / 2
        iw_sfp = (x1 - x0) - 2 * SFP_WALL
        sp_sfp = iw_sfp / (N_SFP_C + 1)
        for rz in [cz_sfp + ROW_OFF, cz_sfp - ROW_OFF]:
            for pi in range(N_SFP_C):
                ppx = (cx_sfp - iw_sfp/2) + (pi + 1) * sp_sfp
                _sw_box(bm_sfp_pins, ppx - CW2/2, ppx + CW2/2, PIN_Y0, PIN_Y1,
                        rz - CH2/2, rz + CH2/2)
    bm_sfp_pins.verts.ensure_lookup_table()
    for i in range(len(SFP_CAGES_DEF) * N_SFP_C * 2):
        b = i * 8
        vs_sp = bm_sfp_pins.verts[b:b+8]
        for f in [(0,1,2,3),(4,7,6,5),(0,4,5,1),(3,2,6,7),(0,3,7,4),(1,5,6,2)]:
            try: bm_sfp_pins.faces.new([vs_sp[j] for j in f])
            except: pass
    parts.append(_sw_mesh_obj(f"{name}_sfp_contacts", bm_sfp_pins, col, 'M_Gold'))

    # SFP guide rails
    RY0_sfp = SFP_MOUTH_Y + SFP_WALL + 0.001
    RY1_sfp = SFP_BACK_Y  - SFP_WALL - 0.001
    bm_rails = bmesh.new()
    for x0, x1, z0, z1 in SFP_CAGES_DEF:
        cx_sfp = (x0 + x1) / 2
        for (za, zb) in [(z1 - SFP_WALL - 0.001, z1 - SFP_WALL),
                         (z0 + SFP_WALL, z0 + SFP_WALL + 0.001)]:
            _sw_box(bm_rails, cx_sfp - SFP_IW/2, cx_sfp + SFP_IW/2,
                    RY0_sfp, RY1_sfp, za, zb)
    parts.append(_sw_mesh_obj(f"{name}_sfp_rails", bm_rails, col, 'M_DarkGrayMet'))

    # ─────────────────────────────────────────────────────────────────────
    # FANS: shroud ring + 7 swept blades + hub cylinder + duct box
    # ─────────────────────────────────────────────────────────────────────
    for fi, fcx in enumerate([FAN1_CX, FAN2_CX], 1):
        suffix = f"_{fi}"
        N_BLADES = 7
        bm_fan = bmesh.new()
        N_RING = 48; SR = FAN_SHROUD_R; ST = 0.0025
        outer_f_r = []; outer_b_r = []
        for i in range(N_RING):
            a = 2 * math.pi * i / N_RING
            outer_f_r.append(bm_fan.verts.new((fcx + SR*math.cos(a), BACK_Y,        FAN_CZ + SR*math.sin(a))))
            outer_b_r.append(bm_fan.verts.new((fcx + SR*math.cos(a), BACK_Y - ST*3, FAN_CZ + SR*math.sin(a))))
        IR = SR - 0.0035
        inner_f_r = []; inner_b_r = []
        for i in range(N_RING):
            a = 2 * math.pi * i / N_RING
            inner_f_r.append(bm_fan.verts.new((fcx + IR*math.cos(a), BACK_Y,        FAN_CZ + IR*math.sin(a))))
            inner_b_r.append(bm_fan.verts.new((fcx + IR*math.cos(a), BACK_Y - ST*3, FAN_CZ + IR*math.sin(a))))
        for i in range(N_RING):
            n = (i + 1) % N_RING
            _sw_F(bm_fan, [outer_f_r[i], outer_f_r[n], outer_b_r[n], outer_b_r[i]])
            _sw_F(bm_fan, [inner_f_r[i], inner_b_r[i], inner_b_r[n], inner_f_r[n]])
            _sw_F(bm_fan, [outer_f_r[i], inner_f_r[i], inner_f_r[n], outer_f_r[n]])
            _sw_F(bm_fan, [outer_b_r[i], outer_b_r[n], inner_b_r[n], inner_b_r[i]])
        parts.append(_sw_mesh_obj(f"{name}_fan_shroud{suffix}", bm_fan, col, 'M_DarkGrayMet'))

        # Blades
        bm_bl = bmesh.new()
        BLADE_R_IN = 0.003; BLADE_R_OUT = IR - 0.001; PITCH = 0.008
        for b_i in range(N_BLADES):
            angle_base = 2 * math.pi * b_i / N_BLADES
            angle_tip  = angle_base + 0.45
            y_in_f  = BACK_Y - 0.003
            y_out_f = BACK_Y - 0.003 + PITCH
            y_in_b  = y_in_f  - 0.002
            y_out_b = y_out_f - 0.002
            bl_w = 0.0030
            vs_bl = [
                bm_bl.verts.new((fcx + BLADE_R_IN*math.cos(angle_base)  - bl_w*math.sin(angle_base),  y_in_f,  FAN_CZ + BLADE_R_IN*math.sin(angle_base)  + bl_w*math.cos(angle_base))),
                bm_bl.verts.new((fcx + BLADE_R_OUT*math.cos(angle_tip)  - bl_w*math.sin(angle_tip),   y_out_f, FAN_CZ + BLADE_R_OUT*math.sin(angle_tip)  + bl_w*math.cos(angle_tip))),
                bm_bl.verts.new((fcx + BLADE_R_OUT*math.cos(angle_tip)  + bl_w*math.sin(angle_tip),   y_out_f, FAN_CZ + BLADE_R_OUT*math.sin(angle_tip)  - bl_w*math.cos(angle_tip))),
                bm_bl.verts.new((fcx + BLADE_R_IN*math.cos(angle_base)  + bl_w*math.sin(angle_base),  y_in_f,  FAN_CZ + BLADE_R_IN*math.sin(angle_base)  - bl_w*math.cos(angle_base))),
                bm_bl.verts.new((fcx + BLADE_R_IN*math.cos(angle_base)  - bl_w*math.sin(angle_base),  y_in_b,  FAN_CZ + BLADE_R_IN*math.sin(angle_base)  + bl_w*math.cos(angle_base))),
                bm_bl.verts.new((fcx + BLADE_R_OUT*math.cos(angle_tip)  - bl_w*math.sin(angle_tip),   y_out_b, FAN_CZ + BLADE_R_OUT*math.sin(angle_tip)  + bl_w*math.cos(angle_tip))),
                bm_bl.verts.new((fcx + BLADE_R_OUT*math.cos(angle_tip)  + bl_w*math.sin(angle_tip),   y_out_b, FAN_CZ + BLADE_R_OUT*math.sin(angle_tip)  - bl_w*math.cos(angle_tip))),
                bm_bl.verts.new((fcx + BLADE_R_IN*math.cos(angle_base)  + bl_w*math.sin(angle_base),  y_in_b,  FAN_CZ + BLADE_R_IN*math.sin(angle_base)  - bl_w*math.cos(angle_base))),
            ]
            for f in [(0,1,2,3),(4,7,6,5),(0,4,5,1),(3,2,6,7),(0,3,7,4),(1,5,6,2)]:
                try: bm_bl.faces.new([vs_bl[i] for i in f])
                except: pass
        parts.append(_sw_mesh_obj(f"{name}_fan_blades{suffix}", bm_bl, col, 'M_BlackMatte'))

        # Hub
        bm_hub = bmesh.new()
        HR = 0.0038; HY0 = BACK_Y - 0.003; HY1 = BACK_Y + 0.001
        hub_f_v = []; hub_b_v = []
        for i in range(16):
            a = 2 * math.pi * i / 16
            hub_f_v.append(bm_hub.verts.new((fcx + HR*math.cos(a), HY0, FAN_CZ + HR*math.sin(a))))
            hub_b_v.append(bm_hub.verts.new((fcx + HR*math.cos(a), HY1, FAN_CZ + HR*math.sin(a))))
        cf_v = bm_hub.verts.new((fcx, HY0, FAN_CZ))
        cb_v = bm_hub.verts.new((fcx, HY1, FAN_CZ))
        for i in range(16):
            n = (i + 1) % 16
            _sw_F(bm_hub, [hub_f_v[i], hub_f_v[n], hub_b_v[n], hub_b_v[i]])
            try: bm_hub.faces.new([cf_v, hub_f_v[n], hub_f_v[i]])
            except: pass
            try: bm_hub.faces.new([cb_v, hub_b_v[i], hub_b_v[n]])
            except: pass
        parts.append(_sw_mesh_obj(f"{name}_fan_hub{suffix}", bm_hub, col, 'M_DarkGrayMet'))

        # Duct box
        bm_duct = bmesh.new()
        DZ0 = max(FAN_CZ - FAN_SHROUD_R, -HH + 0.001)
        DZ1 = min(FAN_CZ + FAN_SHROUD_R,  HH - 0.001)
        DX0 = fcx - FAN_SHROUD_R; DX1 = fcx + FAN_SHROUD_R
        DY0 = BACK_Y; DY1 = BACK_Y - FAN_DUCT_D
        oo_d = [bm_duct.verts.new((DX0, DY0, DZ0)), bm_duct.verts.new((DX1, DY0, DZ0)),
                bm_duct.verts.new((DX1, DY0, DZ1)), bm_duct.verts.new((DX0, DY0, DZ1))]
        ii_d = [bm_duct.verts.new((DX0, DY1, DZ0)), bm_duct.verts.new((DX1, DY1, DZ0)),
                bm_duct.verts.new((DX1, DY1, DZ1)), bm_duct.verts.new((DX0, DY1, DZ1))]
        _sw_F(bm_duct, [ii_d[0], ii_d[1], ii_d[2], ii_d[3]])
        _sw_F(bm_duct, [oo_d[0], ii_d[0], ii_d[3], oo_d[3]])
        _sw_F(bm_duct, [oo_d[1], oo_d[2], ii_d[2], ii_d[1]])
        _sw_F(bm_duct, [oo_d[0], oo_d[1], ii_d[1], ii_d[0]])
        _sw_F(bm_duct, [oo_d[3], ii_d[3], ii_d[2], oo_d[2]])
        parts.append(_sw_mesh_obj(f"{name}_fan_duct{suffix}", bm_duct, col, 'M_BlackMatte'))

    # ─────────────────────────────────────────────────────────────────────
    # REAR PLATE with fan holes, console/mgmt RJ45 holes, IEC C14 hole
    # ─────────────────────────────────────────────────────────────────────
    rp_rect_holes = []
    for rp in REAR_PORTS:
        rp_rect_holes.append((
            rp['cx'] - REAR_OW/2, rp['cx'] + REAR_OW/2,
            rp['cz'] - REAR_OH/2, rp['cz'] + REAR_OH/2,
        ))
    rp_rect_holes.append((
        IEC_CX - IEC_CUT_W/2, IEC_CX + IEC_CUT_W/2,
        IEC_CZ - IEC_CUT_H/2, IEC_CZ + IEC_CUT_H/2,
    ))
    rp_circ_holes = [
        (FAN1_CX, FAN_CZ, FAN_HOLE_R),
        (FAN2_CX, FAN_CZ, FAN_HOLE_R),
    ]
    parts.append(_sw_holey_plate(
        f"{name}_rear_plate", BACK_Y,
        rp_rect_holes, rp_circ_holes,
        col, 'M_Aluminum',
        x_min=-HW, x_max=HW, z_min=-HH, z_max=HH,
        outward_plus_y=True,
    ))

    # ─────────────────────────────────────────────────────────────────────
    # REAR RJ45 PORTS (Console + Mgmt)
    # ─────────────────────────────────────────────────────────────────────
    RWALL  = 0.00140
    RIW    = REAR_OW - 2 * RWALL
    RIH    = REAR_OH - 2 * RWALL
    bm_rh  = bmesh.new()
    bm_rc  = bmesh.new()
    for rp in REAR_PORTS:
        px_r = rp['cx']; pz_r = rp['cz']
        py_mouth = REAR_MOUTH_Y; py_deep = REAR_DEEP_Y
        py_iback = py_deep + RWALL
        om_r = [bm_rh.verts.new((px_r - REAR_OW/2, py_mouth, pz_r - REAR_OH/2)),
                bm_rh.verts.new((px_r + REAR_OW/2, py_mouth, pz_r - REAR_OH/2)),
                bm_rh.verts.new((px_r + REAR_OW/2, py_mouth, pz_r + REAR_OH/2)),
                bm_rh.verts.new((px_r - REAR_OW/2, py_mouth, pz_r + REAR_OH/2))]
        im_r = [bm_rh.verts.new((px_r - RIW/2 + CHAM, py_mouth, pz_r - RIH/2 + CHAM)),
                bm_rh.verts.new((px_r + RIW/2 - CHAM, py_mouth, pz_r - RIH/2 + CHAM)),
                bm_rh.verts.new((px_r + RIW/2 - CHAM, py_mouth, pz_r + RIH/2 - CHAM)),
                bm_rh.verts.new((px_r - RIW/2 + CHAM, py_mouth, pz_r + RIH/2 - CHAM))]
        od_r = [bm_rh.verts.new((px_r - REAR_OW/2, py_deep, pz_r - REAR_OH/2)),
                bm_rh.verts.new((px_r + REAR_OW/2, py_deep, pz_r - REAR_OH/2)),
                bm_rh.verts.new((px_r + REAR_OW/2, py_deep, pz_r + REAR_OH/2)),
                bm_rh.verts.new((px_r - REAR_OW/2, py_deep, pz_r + REAR_OH/2))]
        ib_r = [bm_rh.verts.new((px_r - RIW/2, py_iback, pz_r - RIH/2)),
                bm_rh.verts.new((px_r + RIW/2, py_iback, pz_r - RIH/2)),
                bm_rh.verts.new((px_r + RIW/2, py_iback, pz_r + RIH/2)),
                bm_rh.verts.new((px_r - RIW/2, py_iback, pz_r + RIH/2))]
        _sw_F(bm_rh, [om_r[0], om_r[1], im_r[1], im_r[0]])
        _sw_F(bm_rh, [om_r[2], om_r[3], im_r[3], im_r[2]])
        _sw_F(bm_rh, [om_r[3], om_r[0], im_r[0], im_r[3]])
        _sw_F(bm_rh, [om_r[1], om_r[2], im_r[2], im_r[1]])
        _sw_F(bm_rh, [om_r[0], od_r[0], od_r[1], om_r[1]])
        _sw_F(bm_rh, [om_r[3], od_r[3], od_r[2], om_r[2]])
        _sw_F(bm_rh, [om_r[3], om_r[0], od_r[0], od_r[3]])
        _sw_F(bm_rh, [om_r[1], od_r[1], od_r[2], om_r[2]])
        _sw_F(bm_rh, [od_r[0], od_r[3], od_r[2], od_r[1]])
        _sw_F(bm_rh, [im_r[0], im_r[1], ib_r[1], ib_r[0]])
        _sw_F(bm_rh, [im_r[2], im_r[3], ib_r[3], ib_r[2]])
        _sw_F(bm_rh, [im_r[3], im_r[0], ib_r[0], ib_r[3]])
        _sw_F(bm_rh, [im_r[1], im_r[2], ib_r[2], ib_r[1]])
        _sw_F(bm_rh, [ib_r[0], ib_r[1], ib_r[2], ib_r[3]])
        pin_y0_r = py_iback + 0.0002; pin_y1_r = pin_y0_r + 0.0003
        pin_z0_r = pz_r - RIH/2 + 0.001
        sp_r = RIW / 9
        for pi in range(8):
            ppx_r = (px_r - RIW/2) + (pi + 1) * sp_r
            _sw_box(bm_rc, ppx_r - 0.0003, ppx_r + 0.0003,
                    pin_y0_r, pin_y1_r, pin_z0_r, pin_z0_r + 0.0011)
    parts.append(_sw_mesh_obj(f"{name}_rear_port_housings", bm_rh, col, 'M_PlasticDark'))
    bm_rc.verts.ensure_lookup_table()
    for i in range(len(REAR_PORTS) * 8):
        b = i * 8
        vs_rc = bm_rc.verts[b:b+8]
        for f in [(0,1,2,3),(4,7,6,5),(0,4,5,1),(3,2,6,7),(0,3,7,4),(1,5,6,2)]:
            try: bm_rc.faces.new([vs_rc[j] for j in f])
            except: pass
    parts.append(_sw_mesh_obj(f"{name}_rear_port_contacts", bm_rc, col, 'M_Gold'))

    # ─────────────────────────────────────────────────────────────────────
    # IEC C14 POWER INLET
    # ─────────────────────────────────────────────────────────────────────
    CX_i = IEC_CX; CZ_i = IEC_CZ
    FLG_Y0 = BACK_Y; FLG_Y1 = BACK_Y + IEC_FLG_T
    SOCK_Y1 = BACK_Y - IEC_SOCK_D
    S_WALL = 0.002
    ox0 = CX_i - IEC_FLG_W/2; ox1 = CX_i + IEC_FLG_W/2
    oz0 = CZ_i - IEC_FLG_H/2; oz1 = CZ_i + IEC_FLG_H/2
    cx0_i = CX_i - IEC_CUT_W/2; cx1_i = CX_i + IEC_CUT_W/2
    cz0_i = CZ_i - IEC_CUT_H/2; cz1_i = CZ_i + IEC_CUT_H/2
    ix0_i = cx0_i + S_WALL; ix1_i = cx1_i - S_WALL
    iz0_i = cz0_i + S_WALL; iz1_i = cz1_i - S_WALL

    bm_iec = bmesh.new()
    of_i = [bm_iec.verts.new((ox0, FLG_Y0, oz0)), bm_iec.verts.new((ox1, FLG_Y0, oz0)),
            bm_iec.verts.new((ox1, FLG_Y0, oz1)), bm_iec.verts.new((ox0, FLG_Y0, oz1))]
    ob_i = [bm_iec.verts.new((ox0, SOCK_Y1, oz0)), bm_iec.verts.new((ox1, SOCK_Y1, oz0)),
            bm_iec.verts.new((ox1, SOCK_Y1, oz1)), bm_iec.verts.new((ox0, SOCK_Y1, oz1))]
    cf_i = [bm_iec.verts.new((cx0_i, FLG_Y0, cz0_i)), bm_iec.verts.new((cx1_i, FLG_Y0, cz0_i)),
            bm_iec.verts.new((cx1_i, FLG_Y0, cz1_i)), bm_iec.verts.new((cx0_i, FLG_Y0, cz1_i))]
    it_i = [bm_iec.verts.new((ix0_i, FLG_Y0, iz0_i)), bm_iec.verts.new((ix1_i, FLG_Y0, iz0_i)),
            bm_iec.verts.new((ix1_i, FLG_Y0, iz1_i)), bm_iec.verts.new((ix0_i, FLG_Y0, iz1_i))]
    ib_i = [bm_iec.verts.new((ix0_i, SOCK_Y1, iz0_i)), bm_iec.verts.new((ix1_i, SOCK_Y1, iz0_i)),
            bm_iec.verts.new((ix1_i, SOCK_Y1, iz1_i)), bm_iec.verts.new((ix0_i, SOCK_Y1, iz1_i))]
    _sw_F(bm_iec, [of_i[0], of_i[1], cf_i[1], cf_i[0]])
    _sw_F(bm_iec, [of_i[3], cf_i[3], cf_i[2], of_i[2]])
    _sw_F(bm_iec, [of_i[0], cf_i[0], cf_i[3], of_i[3]])
    _sw_F(bm_iec, [of_i[1], of_i[2], cf_i[2], cf_i[1]])
    _sw_F(bm_iec, [of_i[0], ob_i[0], ob_i[1], of_i[1]])
    _sw_F(bm_iec, [of_i[3], of_i[2], ob_i[2], ob_i[3]])
    _sw_F(bm_iec, [of_i[0], of_i[3], ob_i[3], ob_i[0]])
    _sw_F(bm_iec, [of_i[1], ob_i[1], ob_i[2], of_i[2]])
    _sw_F(bm_iec, [ob_i[0], ob_i[3], ob_i[2], ob_i[1]])
    _sw_F(bm_iec, [cf_i[0], cf_i[1], it_i[1], it_i[0]])
    _sw_F(bm_iec, [cf_i[3], it_i[3], it_i[2], cf_i[2]])
    _sw_F(bm_iec, [cf_i[0], it_i[0], it_i[3], cf_i[3]])
    _sw_F(bm_iec, [cf_i[1], cf_i[2], it_i[2], it_i[1]])
    _sw_F(bm_iec, [it_i[0], it_i[1], ib_i[1], ib_i[0]])
    _sw_F(bm_iec, [it_i[3], ib_i[3], ib_i[2], it_i[2]])
    _sw_F(bm_iec, [it_i[0], ib_i[0], ib_i[3], it_i[3]])
    _sw_F(bm_iec, [it_i[1], it_i[2], ib_i[2], ib_i[1]])
    _sw_F(bm_iec, [ib_i[0], ib_i[1], ib_i[2], ib_i[3]])
    parts.append(_sw_mesh_obj(f"{name}_iec_body", bm_iec, col, 'M_BlackMatte'))

    # IEC Flange
    bm_flg = bmesh.new()
    py0_f = FLG_Y0; py1_f = FLG_Y1
    f0_v = [bm_flg.verts.new((ox0, py0_f, oz0)), bm_flg.verts.new((ox1, py0_f, oz0)),
            bm_flg.verts.new((ox1, py0_f, oz1)), bm_flg.verts.new((ox0, py0_f, oz1))]
    f1_v = [bm_flg.verts.new((ox0, py1_f, oz0)), bm_flg.verts.new((ox1, py1_f, oz0)),
            bm_flg.verts.new((ox1, py1_f, oz1)), bm_flg.verts.new((ox0, py1_f, oz1))]
    c0_v = [bm_flg.verts.new((cx0_i, py0_f, cz0_i)), bm_flg.verts.new((cx1_i, py0_f, cz0_i)),
            bm_flg.verts.new((cx1_i, py0_f, cz1_i)), bm_flg.verts.new((cx0_i, py0_f, cz1_i))]
    c1_v = [bm_flg.verts.new((cx0_i, py1_f, cz0_i)), bm_flg.verts.new((cx1_i, py1_f, cz0_i)),
            bm_flg.verts.new((cx1_i, py1_f, cz1_i)), bm_flg.verts.new((cx0_i, py1_f, cz1_i))]
    _sw_F(bm_flg, [f1_v[0], f1_v[1], c1_v[1], c1_v[0]])
    _sw_F(bm_flg, [f1_v[3], c1_v[3], c1_v[2], f1_v[2]])
    _sw_F(bm_flg, [f1_v[0], c1_v[0], c1_v[3], f1_v[3]])
    _sw_F(bm_flg, [f1_v[1], f1_v[2], c1_v[2], c1_v[1]])
    _sw_F(bm_flg, [f0_v[0], c0_v[0], c0_v[1], f0_v[1]])
    _sw_F(bm_flg, [f0_v[3], f0_v[2], c0_v[2], c0_v[3]])
    _sw_F(bm_flg, [f0_v[0], f0_v[3], c0_v[3], c0_v[0]])
    _sw_F(bm_flg, [f0_v[1], c0_v[1], c0_v[2], f0_v[2]])
    for i in range(4):
        _sw_F(bm_flg, [f0_v[i], f1_v[i], f1_v[(i+1)%4], f0_v[(i+1)%4]])
    parts.append(_sw_mesh_obj(f"{name}_iec_flange", bm_flg, col, 'M_DarkGrayMet'))

    # IEC Screws
    bm_scr = bmesh.new()
    SR_i = 0.002; ST_i = 0.001; NS_i = 12
    for scx_i in [CX_i - (IEC_CUT_W/2 + (IEC_FLG_W/2 - IEC_CUT_W/2)/2),
                  CX_i + (IEC_CUT_W/2 + (IEC_FLG_W/2 - IEC_CUT_W/2)/2)]:
        rim_b_s = []; rim_f_s = []
        for i in range(NS_i):
            a = 2 * math.pi * i / NS_i
            rim_b_s.append(bm_scr.verts.new((scx_i + SR_i*math.cos(a), FLG_Y1,          CZ_i + SR_i*math.sin(a))))
            rim_f_s.append(bm_scr.verts.new((scx_i + SR_i*math.cos(a), FLG_Y1 + ST_i,   CZ_i + SR_i*math.sin(a))))
        cf_scr = bm_scr.verts.new((scx_i, FLG_Y1 + ST_i, CZ_i))
        for i in range(NS_i):
            _sw_F(bm_scr, [rim_b_s[i], rim_f_s[i], rim_f_s[(i+1)%NS_i], rim_b_s[(i+1)%NS_i]])
            try: bm_scr.faces.new([cf_scr, rim_f_s[i], rim_f_s[(i+1)%NS_i]])
            except: pass
    parts.append(_sw_mesh_obj(f"{name}_iec_screws", bm_scr, col, 'M_DarkGrayMet'))

    # IEC Contacts (Earth/L/N)
    bm_iec_con = bmesh.new()
    PY0_iec = SOCK_Y1 + 0.0005; PY1_iec = PY0_iec + 0.001
    def _blade(cx_b, cz_b, bw, bh):
        _sw_box(bm_iec_con, cx_b - bw/2, cx_b + bw/2, PY0_iec, PY1_iec,
                cz_b - bh/2, cz_b + bh/2)
    _blade(CX_i,         CZ_i + 0.0055, 0.007,  0.005)  # Earth
    _blade(CX_i + 0.0075, CZ_i - 0.0045, 0.0038, 0.009)  # Live
    _blade(CX_i - 0.0075, CZ_i - 0.0045, 0.0038, 0.009)  # Neutral
    parts.append(_sw_mesh_obj(f"{name}_iec_contacts", bm_iec_con, col, 'M_Gold'))

    # ─────────────────────────────────────────────────────────────────────
    # LCD DISPLAY (left of front panel)
    # ─────────────────────────────────────────────────────────────────────
    DX0 = -0.2060; DX1 = -0.1480; DZ0 = -0.0120; DZ1 = 0.0140
    PY_disp = FRONT_Y - 0.0005
    bm_disp_bz = bmesh.new()
    _sw_box(bm_disp_bz, DX0 - 0.003, DX1 + 0.003,
            PY_disp, PY_disp + 0.004,
            DZ0 - 0.003, DZ1 + 0.003)
    parts.append(_sw_mesh_obj(f"{name}_display_bezel", bm_disp_bz, col, 'M_BlackMatte'))
    bm_disp_sc = bmesh.new()
    _sw_box(bm_disp_sc, DX0, DX1, PY_disp, PY_disp + 0.0015, DZ0, DZ1)
    parts.append(_sw_mesh_obj(f"{name}_display_screen", bm_disp_sc, col, 'M_Display'))

    # ─────────────────────────────────────────────────────────────────────
    # TOP LOUVERS
    # ─────────────────────────────────────────────────────────────────────
    LOUVER_W = 0.001; LOUVER_GAP = 0.003; N_LOUVERS = 18
    for side_off in [0.0, 0.08]:
        bm_louv = bmesh.new()
        START_X_L = -0.05
        for i in range(N_LOUVERS):
            lx = START_X_L + side_off + i * (LOUVER_W + LOUVER_GAP)
            _sw_box(bm_louv, lx, lx + LOUVER_W, -0.05, 0.05, HH, HH + 0.012)
        parts.append(_sw_mesh_obj(f"{name}_top_louvers_{int(side_off*100)}", bm_louv, col, 'M_DarkGrayMet'))

    # ─────────────────────────────────────────────────────────────────────
    # SIDE VENTS
    # ─────────────────────────────────────────────────────────────────────
    VENT_W = 0.001; VENT_GAP = 0.0025; N_VENTS = 24; VENT_H_sv = 0.020
    for x_pos in [HW, -HW]:
        bm_vent = bmesh.new()
        start_z_v = -VENT_H_sv * N_VENTS * 0.5 * (VENT_W + VENT_GAP)
        for i in range(N_VENTS):
            vz = start_z_v + i * (VENT_W + VENT_GAP)
            x0_v = x_pos - VENT_W if x_pos > 0 else x_pos
            x1_v = x_pos           if x_pos > 0 else x_pos + VENT_W
            _sw_box(bm_vent, x0_v, x1_v, -0.04, 0.04, vz, vz + VENT_H_sv)
        side_label = 'R' if x_pos > 0 else 'L'
        parts.append(_sw_mesh_obj(f"{name}_side_vents_{side_label}", bm_vent, col, 'M_DarkGrayMet'))

    # ─────────────────────────────────────────────────────────────────────
    # TRANSLATE all vertices: centred coords → equipment-origin convention
    # local (0, 0, 0) = front-face-bottom-centre
    # shift = (0, +d/2, +h/2)
    # ─────────────────────────────────────────────────────────────────────
    tx, ty, tz = 0.0, d / 2, h / 2
    for obj in parts:
        me = obj.data
        for v in me.vertices:
            v.co.x += tx
            v.co.y += ty
            v.co.z += tz
        me.update()
        obj.hide_render = False

    # ─────────────────────────────────────────────────────────────────────
    # MOUNTING EARS — always present (built in equipment-origin space)
    # ─────────────────────────────────────────────────────────────────────
    ear_w = (EIA_RAIL_SPAN_M - EIA_EQUIPMENT_BODY_M) / 2
    ear_d = 0.002
    ear_h_dim = h * 0.68
    for side_sign in (-1, 1):
        side_label = 'L' if side_sign < 0 else 'R'
        ear_cx = side_sign * (w / 2 + ear_w / 2)
        # In equipment-origin space: Y=0 is front face, centre is at d/2
        ear_cy = ear_d / 2  # just proud of front face
        ear_cz = h / 2
        bm_ear = bmesh.new()
        _sw_box(bm_ear,
                ear_cx - ear_w/2, ear_cx + ear_w/2,
                -ear_d, 0.0,
                (h - ear_h_dim)/2, (h + ear_h_dim)/2)
        parts.append(_sw_mesh_obj(f"{name}_ear_{side_label}", bm_ear, col, 'M_Aluminum'))

    # ─────────────────────────────────────────────────────────────────────
    # JOIN or PARENT
    # ─────────────────────────────────────────────────────────────────────
    sockets_created: List[bpy.types.Object] = []

    if join_mesh:
        joined = _join_parts(parts, name)
        # Recalculate normals on the joined mesh
        bpy.ops.object.select_all(action='DESELECT')
        joined.select_set(True)
        bpy.context.view_layer.objects.active = joined
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.normals_make_consistent(inside=False)
        bpy.ops.object.mode_set(mode='OBJECT')
        _set_origin_to(joined, (0.0, 0.0, 0.0))
    else:
        joined = parts[0]
        for p in parts[1:]:
            p.parent = joined
        _set_origin_to(joined, (0.0, 0.0, 0.0))

    # ── SOCKET_ empties ────────────────────────────────────────────────────
    # SFP+ uplink sockets — positions in equipment-origin space
    SFP_X0_socket = 0.1826  # left edge of SFP zone (from centred coords, shifted +HW ≈ 0.223 + 0 shift)
    for i in range(2):
        ux = SFP_X0_socket + i * 0.020
        up = _add_socket_empty(
            f"{name}_Uplink_{i:02d}",
            location=(ux, 0.0, h * 0.50),
            parent=joined, collection=col,
        )
        up.visible_camera = False
        sockets_created.append(up)

    # Power socket at rear
    pwr = _add_socket_empty(
        f"{name}_Power",
        location=(IEC_CX, d, h * 0.50),
        parent=joined, collection=col,
    )
    pwr.visible_camera = False
    sockets_created.append(pwr)

    joined["equipment_type"] = "switch"
    joined["u_size"]         = u_size
    joined["port_count"]     = port_count
    joined["quality"]        = quality

    return {
        "object":      name,
        "collection":  collection_name,
        "u_size":      u_size,
        "port_count":  port_count,
        "parts":       len(parts),
        "sockets":     [s.name for s in sockets_created],
        "origin":      "front-face-bottom-centre (0, 0, 0)",
    }


# ── Tool 3: create_patch_panel ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_patch_panel(
    name: str = "PatchPanel",
    u_size: int = 1,
    port_count: int = 24,
    collection_name: str = "Equipment",
    random_variation: bool = False,
    quality: str = "high",
) -> Dict[str, Any]:
    """
    Create a 1U/2U patch panel with detailed front-face geometry.

    Builds a panel body, proud bezel frame (top strip, bottom label strip,
    right ID plate), a port grid organised in groups of 8 with vertical
    dividers, cable management rings at the bottom, and EIA mounting ears.

    Port tiles project toward the aisle (negative Y) so directional light
    creates visible shadow relief between ports — no Boolean cuts.

    name:             base name
    u_size:           rack unit height (1 = 24-port, 2 = 48-port typical)
    port_count:       total number of ports to generate
    collection_name:  Blender collection
    random_variation: randomize port positions and per-unit material
    """
    h  = u_size * RACK_U_M
    w  = EIA_EQUIPMENT_BODY_M   # 446 mm body
    # ── Quality flags ─────────────────────────────────────────────────────
    qf = QUALITY_TIERS.get(quality, QUALITY_TIERS["high"])

    d  = 0.040
    st = RACK_SHEET_THICK_M

    col   = _get_or_create_collection(collection_name)
    parts: List[bpy.types.Object] = []
    rv    = random_variation
    socket_specs: List[Tuple[float, float, int]] = []

    # ── Panel body ─────────────────────────────────────────────────────────
    parts.append(_create_box_object(f"{name}_body",
        cx=0.0, cy=d / 2, cz=h / 2, w=w, d=d, h=h, collection=col))

    if qf["bezel"]:
        bz_y = -st / 2
        bz_d = st
        parts.append(_create_box_object(f"{name}_bz_top",
            cx=0.0, cy=bz_y, cz=h - h * 0.10,
            w=w - 0.006, d=bz_d, h=h * 0.16, collection=col))
        parts.append(_create_box_object(f"{name}_bz_bot",
            cx=0.0, cy=bz_y, cz=h * 0.10,
            w=w - 0.006, d=bz_d, h=h * 0.20, collection=col))
        parts.append(_create_box_object(f"{name}_bz_right",
            cx=w * 0.43, cy=bz_y, cz=h / 2,
            w=w * 0.08, d=bz_d, h=h - 0.004, collection=col))
        # ── Port label identification strip (between ports and top bezel) ─
        parts.append(_create_box_object(f"{name}_label",
            cx=0.0, cy=-0.001, cz=h * 0.86,
            w=w * 0.82, d=0.0015, h=h * 0.05, collection=col))

    if qf["server_bays"]:
        # ── Port grid ─────────────────────────────────────────────────────
        ports_per_row = min(port_count, 24)
        rows          = max(1, (port_count + ports_per_row - 1) // ports_per_row)
        port_area_w   = w * 0.82
        port_area_h   = h * 0.48
        port_w        = port_area_w / ports_per_row
        port_h        = port_area_h / rows
        port_cz_base  = h * 0.34

        # Port zone recessed background
        PP_BG_D = 0.010
        parts.append(_create_box_object(f"{name}_port_bg",
            cx=0.0, cy=PP_BG_D / 2, cz=port_cz_base + port_area_h / 2,
            w=port_area_w + 0.010, d=PP_BG_D, h=port_area_h + 0.010, collection=col))

        for row in range(rows):
            for p in range(ports_per_row):
                idx = row * ports_per_row + p
                if idx >= port_count:
                    break
                px = -(port_area_w / 2) + p * port_w + port_w / 2
                pz = port_cz_base + row * (port_h + 0.002)
                px = _jitter(px, 0.0003, rv)
                pz = _jitter(pz, 0.0003, rv)
                # Outer bezel frame
                parts.append(_create_box_object(f"{name}_port_{idx:02d}_frm",
                    cx=px, cy=-0.0010, cz=pz,
                    w=port_w * 0.68 + 0.0025, d=0.0020, h=port_h * 0.74 + 0.0025, collection=col))
                # Recessed inner face
                parts.append(_create_box_object(f"{name}_port_{idx:02d}_inn",
                    cx=px, cy=0.0080, cz=pz,
                    w=port_w * 0.68 - 0.0020, d=0.0025, h=port_h * 0.74 - 0.0020, collection=col))
                socket_specs.append((px, pz, idx))

        # ── Port group dividers ───────────────────────────────────────────
        group_size = 8
        num_groups = ports_per_row // group_size
        for g in range(1, num_groups):
            sep_x = -(port_area_w / 2) + g * group_size * port_w
            parts.append(_create_box_object(f"{name}_sep_{g}",
                cx=sep_x, cy=-0.0015, cz=h * 0.55,
                w=0.0015, d=0.003, h=h * 0.65, collection=col))

        # ── Cable management D-rings (two-post + arch geometry) ───────────
        ring_count   = 4 if port_count <= 24 else 6
        ring_area_w  = w * 0.70
        ring_spacing = ring_area_w / (ring_count + 1)
        ring_cz      = h * 0.11
        ring_h_dim   = 0.013
        ring_post_w  = 0.003
        ring_top_h   = 0.003
        ring_d       = 0.005
        ring_span    = 0.019
        for i in range(ring_count):
            rx = -(ring_area_w / 2) + ring_spacing * (i + 1)
            # Left post
            parts.append(_create_box_object(f"{name}_ring_L_{i}",
                cx=rx - ring_span / 2, cy=-0.005, cz=ring_cz,
                w=ring_post_w, d=ring_d, h=ring_h_dim, collection=col))
            # Right post
            parts.append(_create_box_object(f"{name}_ring_R_{i}",
                cx=rx + ring_span / 2, cy=-0.005, cz=ring_cz,
                w=ring_post_w, d=ring_d, h=ring_h_dim, collection=col))
            # Top arch bar
            parts.append(_create_box_object(f"{name}_ring_T_{i}",
                cx=rx, cy=-0.005,
                cz=ring_cz + ring_h_dim / 2 + ring_top_h / 2,
                w=ring_span + ring_post_w, d=ring_d, h=ring_top_h, collection=col))

    # ── Mounting ears — always present ────────────────────────────────────
    ear_w = (EIA_RAIL_SPAN_M - EIA_EQUIPMENT_BODY_M) / 2
    ear_d = 0.002
    ear_h = h * 0.70
    for side_sign in (-1, 1):
        side_label = 'L' if side_sign < 0 else 'R'
        ear_cx = side_sign * (w / 2 + ear_w / 2)
        parts.append(_create_box_object(f"{name}_ear_{side_label}",
            cx=ear_cx, cy=-ear_d / 2, cz=h / 2,
            w=ear_w, d=ear_d, h=ear_h, collection=col))
        if qf["bezel"]:
            parts.append(_create_box_object(f"{name}_ear_slot_{side_label}",
                cx=ear_cx, cy=-ear_d + 0.001, cz=h * 0.50,
                w=ear_w * 0.30, d=0.001, h=h * 0.25, collection=col))

    # ── Join + origin ──────────────────────────────────────────────────────
    joined = _join_parts(parts, name)

    # ── Per-panel material variation ──────────────────────────────────────
    # Patch panels are typically lighter than servers (grey/silver anodised).
    # Always applied — full range when random_variation=True, narrow when False.
    _var_mat = bpy.data.materials.new(f"{name}_var")
    _var_mat.use_nodes = True
    _var_bsdf = _var_mat.node_tree.nodes.get("Principled BSDF")
    if _var_bsdf:
        if random_variation:
            _base = 0.68 + _random.uniform(-0.06, 0.10)
            _var_bsdf.inputs["Base Color"].default_value = (
                max(0.50, _base + _random.uniform(-0.03, 0.03)),
                max(0.50, _base + _random.uniform(-0.03, 0.03)),
                max(0.50, _base + _random.uniform(-0.03, 0.06)),
                1.0,
            )
            _var_bsdf.inputs["Roughness"].default_value = max(0.30, min(0.65,
                0.42 + _random.uniform(-0.10, 0.15)))
            _var_bsdf.inputs["Metallic"].default_value = max(0.45, min(0.80,
                0.60 + _random.uniform(-0.10, 0.10)))
        else:
            _dirt = _random.uniform(0.0, 0.030)   # panels show more age
            _var_bsdf.inputs["Base Color"].default_value = (
                max(0.45, 0.680 - _dirt),
                max(0.45, 0.680 - _dirt),
                max(0.45, 0.700 - _dirt * 0.7),
                1.0,
            )
            _var_bsdf.inputs["Roughness"].default_value = max(0.32, min(0.58,
                0.42 + _random.uniform(-0.05, 0.07)))
            _var_bsdf.inputs["Metallic"].default_value = max(0.48, min(0.73,
                0.58 + _random.uniform(-0.05, 0.05))
            )
        if joined.data.materials:
            joined.data.materials[0] = _var_mat
        else:
            joined.data.materials.append(_var_mat)

    # ── SOCKET_ empties ────────────────────────────────────────────────────
    sockets_created: List[str] = []
    for (px, pz, idx) in socket_specs:
        s = _add_socket_empty(
            f"{name}_Port_{idx:02d}",
            location=(px, 0.0, pz),
            parent=joined, collection=col,
        )
        sockets_created.append(s.name)

    rear = _add_socket_empty(
        f"{name}_Rear_00",
        location=(0.0, d, h * 0.50),
        parent=joined, collection=col,
    )
    sockets_created.append(rear.name)

    joined["equipment_type"] = "patch_panel"
    joined["u_size"]         = u_size
    joined["port_count"]     = port_count
    joined["quality"]        = quality

    return {
        "object":      name,
        "collection":  collection_name,
        "u_size":      u_size,
        "port_count":  port_count,
        "sockets":     sockets_created,
        "origin":      "front-face-bottom-centre (0, 0, 0)",
    }


# ── Tool 4: create_pdu ────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_pdu(
    name: str = "PDU",
    pdu_type: str = "0U",
    u_size: int = 1,
    outlet_count: int = 0,
    collection_name: str = "Equipment",
    random_variation: bool = False,
    quality: str = "high",
) -> Dict[str, Any]:
    """
    Create a Power Distribution Unit for rack mounting.

    pdu_type='0U' — Vertical 0U strip, zero rack-unit footprint.
        Mounts on the side post of the rack exterior.
        Body: 62 mm wide × 44 mm deep × RACK_INTERIOR_HEIGHT_M tall.
        Outlets: C13 column along the face + 2 C19 (heavy-duty) near ends.
        Control section (breaker + LED + optional ammeter) at ~55 % height.
        C20 input block at top. Rear keyhole mounting tabs.
        outlet_count=0 → default 16.

    pdu_type='1U' — Horizontal rackmount shelf at u_size U height.
        Standard 446 mm body, 200 mm depth.
        Outlets: C13 row across face (landscape orientation).
        C14 inlet on right end. Metered zone (ammeter display + breaker +
        power LED) on left when quality is high or ultra.
        Mounting ears always present.
        outlet_count=0 → default 8.

    name:             base name
    pdu_type:         '0U' (vertical strip) | '1U' (rackmount shelf)
    u_size:           rack unit height for 1U type only
    outlet_count:     number of C13 outlets (0 = auto per type)
    collection_name:  Blender collection
    random_variation: subtle spacing jitter and material variation
    quality:          quality tier controlling outlet detail level
    """
    qf       = QUALITY_TIERS.get(quality, QUALITY_TIERS["high"])
    pdu_type = pdu_type.upper()
    if pdu_type not in ("0U", "1U"):
        raise ValueError("pdu_type must be '0U' or '1U'")

    col              = _get_or_create_collection(collection_name)
    parts: List[bpy.types.Object] = []
    sockets_created: List[str]    = []
    rv                            = random_variation

    # ── IEC outlet geometry helpers (proud geometry, no Booleans) ────────
    # All helpers append to `parts` via closure.

    def _c13(tag, cx, cz, fy, portrait):
        """
        C13 outlet housing + face inset + optional 3-pin stubs (ultra).
        portrait=True  → taller than wide  (0U column layout)
        portrait=False → wider than tall   (1U row layout)
        """
        hw, hh = (0.034, 0.038) if portrait else (0.038, 0.030)
        hd     = 0.0080   # 8 mm recess depth
        # Outer bezel surround (slightly proud of face)
        parts.append(_create_box_object(f"{tag}_hsg",
            cx=cx, cy=fy - 0.0010, cz=cz,
            w=hw + 0.004, d=0.0020, h=hh + 0.004, collection=col))
        # Recessed socket back face
        parts.append(_create_box_object(f"{tag}_face",
            cx=cx, cy=fy + hd, cz=cz,
            w=hw - 0.004, d=0.0025, h=hh - 0.004, collection=col))
        if qf["bay_3d"]:   # ultra: suggest IEC 3-pin pattern
            gz_off = hh * 0.28 if portrait else 0.0
            parts.append(_create_box_object(f"{tag}_gnd",
                cx=cx, cy=fy + hd * 0.50, cz=cz + gz_off,
                w=0.005, d=0.003, h=0.010 if portrait else 0.005,
                collection=col))
            for sx, lbl in [(-1, "L"), (1, "N")]:
                pz_off = -hh * 0.18 if portrait else 0.0
                parts.append(_create_box_object(f"{tag}_pin{lbl}",
                    cx=cx + sx * hw * 0.30, cy=fy + hd * 0.50, cz=cz + pz_off,
                    w=0.004, d=0.003, h=0.010 if portrait else 0.005,
                    collection=col))

    def _c19(tag, cx, cz, fy):
        """C19 heavy-duty outlet (portrait, 0U only)."""
        hw, hh = 0.048, 0.044
        hd     = 0.0100   # 10 mm recess
        # Outer bezel
        parts.append(_create_box_object(f"{tag}_hsg",
            cx=cx, cy=fy - 0.0010, cz=cz,
            w=hw + 0.004, d=0.0020, h=hh + 0.004, collection=col))
        # Recessed back face
        parts.append(_create_box_object(f"{tag}_face",
            cx=cx, cy=fy + hd, cz=cz,
            w=hw - 0.004, d=0.0025, h=hh - 0.004, collection=col))

    def _c14(tag, cx, cz, fy):
        """C14 IEC inlet (1U right end)."""
        # Outer bezel
        parts.append(_create_box_object(f"{tag}_c14_hsg",
            cx=cx, cy=fy - 0.0010, cz=cz,
            w=0.034, d=0.0020, h=0.022, collection=col))
        # Recessed back face (8 mm)
        parts.append(_create_box_object(f"{tag}_c14_face",
            cx=cx, cy=fy + 0.0080, cz=cz,
            w=0.026, d=0.0025, h=0.014, collection=col))

    # ═════════════════════════════════════════════════════════════════════
    # 0U VERTICAL STRIP
    # ═════════════════════════════════════════════════════════════════════
    if pdu_type == "0U":
        n_outlets = outlet_count if outlet_count > 0 else 16
        w_pdu = 0.062
        d_pdu = 0.044
        h_pdu = RACK_INTERIOR_HEIGHT_M   # 1866.9 mm

        # Extruded body
        parts.append(_create_box_object(f"{name}_body",
            cx=0.0, cy=d_pdu / 2, cz=h_pdu / 2,
            w=w_pdu, d=d_pdu, h=h_pdu, collection=col))

        # Input head at top (~65 mm) — C20 inlet + cable entry block
        HEAD_H = 0.065
        parts.append(_create_box_object(f"{name}_head",
            cx=0.0, cy=d_pdu / 2, cz=h_pdu - HEAD_H / 2,
            w=w_pdu, d=d_pdu + 0.006, h=HEAD_H, collection=col))
        if qf["bezel"]:
            # C20 inlet face on head
            parts.append(_create_box_object(f"{name}_c20_hsg",
                cx=0.0, cy=-0.004, cz=h_pdu - HEAD_H / 2,
                w=0.032, d=0.006, h=0.020, collection=col))
            parts.append(_create_box_object(f"{name}_c20_face",
                cx=0.0, cy=-0.0015, cz=h_pdu - HEAD_H / 2,
                w=0.024, d=0.004, h=0.014, collection=col))

        # Foot block (~40 mm)
        FOOT_H = 0.040
        parts.append(_create_box_object(f"{name}_foot",
            cx=0.0, cy=d_pdu / 2, cz=FOOT_H / 2,
            w=w_pdu, d=d_pdu + 0.004, h=FOOT_H, collection=col))

        # Control section at ~55 % height
        CTRL_Z = h_pdu * 0.55
        CTRL_H = 0.080
        if qf["bezel"]:
            parts.append(_create_box_object(f"{name}_ctrl_hsg",
                cx=0.0, cy=-0.004, cz=CTRL_Z,
                w=w_pdu - 0.008, d=0.008, h=CTRL_H, collection=col))
            parts.append(_create_box_object(f"{name}_breaker",
                cx=0.0, cy=-0.006, cz=CTRL_Z + 0.018,
                w=0.016, d=0.005, h=0.012, collection=col))
            parts.append(_create_box_object(f"{name}_ctrl_led",
                cx=_jitter(0.010, 0.002, rv), cy=-0.0055,
                cz=_jitter(CTRL_Z - 0.018, 0.002, rv),
                w=0.006, d=0.004, h=0.006, collection=col))
            if qf["bay_3d"]:   # ultra: ammeter display strip
                parts.append(_create_box_object(f"{name}_meter",
                    cx=0.0, cy=-0.0020, cz=CTRL_Z - 0.006,
                    w=w_pdu - 0.016, d=0.003, h=0.020, collection=col))

        # Rear mounting tabs (keyhole bracket stubs on back)
        if qf["bezel"]:
            for ti, tab_z in enumerate([h_pdu * 0.10, h_pdu * 0.50, h_pdu * 0.90]):
                parts.append(_create_box_object(f"{name}_tab_{ti}",
                    cx=0.0, cy=d_pdu + 0.005, cz=tab_z,
                    w=w_pdu - 0.010, d=0.010, h=0.020, collection=col))

        # ── Outlets along face ────────────────────────────────────────────
        fy_0u        = -0.002
        outlet_z0    = FOOT_H + 0.010
        outlet_zone  = h_pdu - HEAD_H - FOOT_H - 0.020
        c13_spacing  = outlet_zone / (n_outlets + 1)

        # Two C19 positions (one near each end of the outlet zone)
        c19_zs = [outlet_z0 + outlet_zone * 0.06,
                  outlet_z0 + outlet_zone * 0.94]

        outlet_positions: List[float] = []   # for socket placement

        for i in range(n_outlets):
            oz = outlet_z0 + c13_spacing * (i + 1)
            oz = _jitter(oz, 0.003 if rv else 0.0, rv)
            # Skip if near the control section or a C19 position
            too_close = abs(oz - CTRL_Z) < CTRL_H * 0.65
            for c19z in c19_zs:
                if abs(oz - c19z) < 0.042:
                    too_close = True
            if too_close:
                continue
            outlet_positions.append(oz)
            if qf["server_bays"]:
                _c13(f"{name}_c13_{len(outlet_positions) - 1:02d}",
                     cx=0.0, cz=oz, fy=fy_0u, portrait=True)
                # Per-outlet LED (managed PDUs)
                if qf["bezel"]:
                    parts.append(_create_box_object(
                        f"{name}_out_led_{len(outlet_positions) - 1:02d}",
                        cx=w_pdu * 0.34, cy=fy_0u - 0.0018, cz=oz,
                        w=0.005, d=0.003, h=0.005, collection=col))

        # C19 outlets at both ends
        if qf["server_bays"]:
            for ci, c19z in enumerate(c19_zs):
                _c19(f"{name}_c19_{ci}", cx=0.0, cz=c19z, fy=fy_0u)

        joined = _join_parts(parts, name)

        for i, oz in enumerate(outlet_positions):
            s = _add_socket_empty(
                f"{name}_Outlet_{i:02d}",
                location=(0.0, d_pdu, oz),
                parent=joined, collection=col,
            )
            sockets_created.append(s.name)

    # ═════════════════════════════════════════════════════════════════════
    # 1U HORIZONTAL RACKMOUNT
    # ═════════════════════════════════════════════════════════════════════
    else:
        n_outlets = outlet_count if outlet_count > 0 else 8
        h_pdu     = u_size * RACK_U_M
        w_pdu     = EIA_EQUIPMENT_BODY_M   # 446 mm body
        d_pdu     = 0.200

        parts.append(_create_box_object(f"{name}_body",
            cx=0.0, cy=d_pdu / 2, cz=h_pdu / 2,
            w=w_pdu, d=d_pdu, h=h_pdu, collection=col))

        bz_d = RACK_SHEET_THICK_M
        bz_y = -bz_d / 2

        if qf["bezel"]:
            parts.append(_create_box_object(f"{name}_bz_top",
                cx=0.0, cy=bz_y, cz=h_pdu - h_pdu * 0.09,
                w=w_pdu - 0.004, d=bz_d, h=h_pdu * 0.14, collection=col))
            parts.append(_create_box_object(f"{name}_bz_bot",
                cx=0.0, cy=bz_y, cz=h_pdu * 0.09,
                w=w_pdu - 0.004, d=bz_d, h=h_pdu * 0.14, collection=col))

        # ── Zone layout ───────────────────────────────────────────────────
        # [L_MARGIN][METER_W][outlet zone][R_MARGIN][INLET_W]
        INLET_W   = 0.052   # C14 inlet zone on right
        METER_W   = 0.075 if qf["bezel"] else 0.0   # ammeter zone on left
        L_MARGIN  = 0.008
        R_MARGIN  = 0.008
        OUT_ZONE  = w_pdu - METER_W - L_MARGIN - INLET_W - R_MARGIN
        out_step  = OUT_ZONE / n_outlets
        out_z     = h_pdu / 2
        fy_1u     = -0.002

        out_x0    = -w_pdu / 2 + METER_W + L_MARGIN

        # Recessed outlet zone background plate
        out_cx = out_x0 + OUT_ZONE / 2
        if qf["server_bays"]:
            parts.append(_create_box_object(f"{name}_out_bg",
                cx=out_cx, cy=0.0010, cz=out_z,
                w=OUT_ZONE, d=0.002, h=h_pdu * 0.80, collection=col))

        # C13 outlets
        outlet_xs: List[float] = []
        for i in range(n_outlets):
            ox = out_x0 + i * out_step + out_step / 2
            ox = _jitter(ox, 0.001, rv)
            outlet_xs.append(ox)
            if qf["server_bays"]:
                _c13(f"{name}_c13_{i:02d}", cx=ox, cz=out_z,
                     fy=fy_1u, portrait=False)

        # C14 inlet on right end
        inlet_cx = w_pdu / 2 - INLET_W / 2 - R_MARGIN
        if qf["server_bays"]:
            _c14(f"{name}", cx=inlet_cx, cz=out_z, fy=fy_1u)

        # Metered zone (left side): ammeter display + circuit breaker + LED
        meter_cx = -w_pdu / 2 + METER_W / 2
        if qf["bezel"]:
            # 7-segment display background
            parts.append(_create_box_object(f"{name}_meter_bg",
                cx=meter_cx, cy=fy_1u + 0.0008, cz=out_z + h_pdu * 0.12,
                w=METER_W - 0.014, d=0.003, h=h_pdu * 0.44, collection=col))
            # Display digits inset
            parts.append(_create_box_object(f"{name}_meter_disp",
                cx=meter_cx, cy=fy_1u - 0.0005, cz=out_z + h_pdu * 0.12,
                w=METER_W - 0.022, d=0.003, h=h_pdu * 0.28, collection=col))
            # Circuit breaker button
            parts.append(_create_box_object(f"{name}_breaker",
                cx=meter_cx, cy=fy_1u - 0.0035, cz=out_z - h_pdu * 0.24,
                w=0.014, d=0.004, h=0.010, collection=col))
            # Power LED dot
            parts.append(_create_box_object(f"{name}_pwr_led",
                cx=_jitter(meter_cx + 0.016, 0.002, rv),
                cy=fy_1u - 0.0030,
                cz=_jitter(out_z - h_pdu * 0.12, 0.001, rv),
                w=0.005, d=0.003, h=0.005, collection=col))

        # Mounting ears
        ear_w = (EIA_RAIL_SPAN_M - EIA_EQUIPMENT_BODY_M) / 2
        ear_d = 0.002
        ear_h = h_pdu * 0.68
        for side_sign in (-1, 1):
            side_label = 'L' if side_sign < 0 else 'R'
            ear_cx = side_sign * (w_pdu / 2 + ear_w / 2)
            parts.append(_create_box_object(f"{name}_ear_{side_label}",
                cx=ear_cx, cy=-ear_d / 2, cz=h_pdu / 2,
                w=ear_w, d=ear_d, h=ear_h, collection=col))
            if qf["ear_screws"]:
                parts.append(_create_box_object(f"{name}_ear_slot_{side_label}",
                    cx=ear_cx, cy=-ear_d + 0.001, cz=h_pdu * 0.50,
                    w=ear_w * 0.30, d=0.001, h=h_pdu * 0.22, collection=col))

        joined = _join_parts(parts, name)

        for i, ox in enumerate(outlet_xs):
            s = _add_socket_empty(
                f"{name}_Outlet_{i:02d}",
                location=(ox, d_pdu, h_pdu / 2),
                parent=joined, collection=col,
            )
            sockets_created.append(s.name)

    # ── Material ──────────────────────────────────────────────────────────
    mat = bpy.data.materials.new(f"{name}_mat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        _dirt = _random.uniform(0.0, 0.015)
        _base = (0.035 + _random.uniform(-0.010, 0.010) if rv
                 else max(0.022, 0.038 - _dirt))
        bsdf.inputs["Base Color"].default_value = (
            max(0.02, _base), max(0.02, _base), max(0.02, _base + 0.003), 1.0)
        bsdf.inputs["Roughness"].default_value = max(0.35, min(0.65,
            0.50 + _random.uniform(-0.08, 0.10)))
        bsdf.inputs["Metallic"].default_value = max(0.40, min(0.75,
            0.55 + _random.uniform(-0.08, 0.08)))
    if joined.data.materials:
        joined.data.materials[0] = mat
    else:
        joined.data.materials.append(mat)

    joined["equipment_type"] = "pdu"
    joined["pdu_type"]       = pdu_type
    joined["outlet_count"]   = outlet_count
    joined["quality"]        = quality

    return {
        "object":       name,
        "collection":   collection_name,
        "pdu_type":     pdu_type,
        "outlet_count": n_outlets,
        "sockets":      sockets_created,
        "origin":       "front-face-bottom-centre (0, 0, 0)",
    }


# ── Tool 5: add_equipment_sockets ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_equipment_sockets(
    object_name: str,
    sockets: List[Dict[str, Any]],
    collection_name: str = "",
) -> Dict[str, Any]:
    """
    Add named SOCKET_ empties to any existing equipment or rack object.

    Each socket dict requires:
      name:     socket identifier (SOCKET_ prefix added automatically)
      location: [x, y, z] local offset from object origin in metres
      rotation: optional [rx, ry, rz] in radians (default [0, 0, 0])

    object_name:     target object to parent sockets to
    sockets:         list of socket specification dicts
    collection_name: collection to link empties into
                     (defaults to object's first collection)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    if collection_name:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
    else:
        cols = list(obj.users_collection)
        if not cols:
            raise ValueError(f"Object '{object_name}' is not in any collection")
        col = cols[0]

    created: List[str] = []
    for spec in sockets:
        sock_name = spec.get("name", "Socket")
        loc       = tuple(spec.get("location", [0.0, 0.0, 0.0]))
        rot       = tuple(spec.get("rotation", [0.0, 0.0, 0.0]))

        full_name = sock_name if sock_name.startswith(SOCKET_PREFIX) else f"{SOCKET_PREFIX}{sock_name}"
        existing  = bpy.data.objects.get(full_name)
        if existing:
            bpy.data.objects.remove(existing, do_unlink=True)

        e = bpy.data.objects.new(full_name, None)
        e.empty_display_type = 'ARROWS'
        e.empty_display_size = 0.015
        e.location       = loc
        e.rotation_euler = rot
        col.objects.link(e)
        e.parent = obj
        e.matrix_parent_inverse = obj.matrix_world.inverted()
        created.append(full_name)

    return {"object": object_name, "added": created, "count": len(created)}


# ── Equipment type → creator function mapping ─────────────────────────────
# Defined after all creator functions so the references are valid.
# Calling these decorated functions from within another @thread_safe function
# is safe: thread_safe detects the main-thread context and calls directly.

_EQUIPMENT_CREATORS = {
    "server":      create_server_chassis,
    "switch":      create_network_switch,
    "patch_panel": create_patch_panel,
    "pdu":         create_pdu,
}

_CREATOR_EXTRA_KEYS = {
    "server":      ("depth_mm", "drive_bays", "data_ports"),
    "switch":      ("port_count",),
    "patch_panel": ("port_count",),
    "pdu":         ("pdu_type", "outlet_count"),
}

_CREATOR_DEFAULTS = {
    "server":      {"depth_mm": 700.0, "drive_bays": 4, "data_ports": 2},
    "switch":      {"port_count": 48},
    "patch_panel": {"port_count": 24},
    "pdu":         {"pdu_type": "1U", "outlet_count": 0},
}


# ── Tool 6: populate_rack_from_json ───────────────────────────────────────

@mcp.tool()
@thread_safe
def populate_rack_from_json(
    json_path: str,
    collection_name: str = "",
    random_variation: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Populate a rack with equipment by reading a JSON layout file.

    Required JSON structure:
      {
        "rack": "<collection name>",
        "equipment": [
          {
            "u_slot":      1,          // U slot number (1 = bottom)
            "u_size":      2,          // U slots occupied
            "type":        "server",   // "server"|"switch"|"patch_panel"|"pdu"
            "name":        "SVR_01",   // unique object name
            "depth_mm":    700,        // server only (optional, default 700)
            "port_count":  48,         // switch/patch_panel (optional)
            "pdu_type":    "1U",       // pdu only (optional, default "1U")
            "outlet_count": 0          // pdu only (0 = auto: 16 for 0U, 8 for 1U)
          }, ...
        ]
      }

    Compatible with JSON produced by export_rack_layout_json when an
    "equipment" key is appended to the payload before saving.

    json_path:         absolute path to equipment layout JSON
    collection_name:   override the "rack" key in the JSON (optional)
    random_variation:  pass to each equipment creator for visual variety
    dry_run:           validate and report without creating any objects
    """
    if not os.path.isfile(json_path):
        raise FileNotFoundError(f"JSON not found: {json_path}")

    with open(json_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    rack_col_name = collection_name or payload.get("rack", "")
    if not rack_col_name:
        raise ValueError("JSON missing 'rack' key — provide collection_name or add it to the JSON")

    col = bpy.data.collections.get(rack_col_name)
    if not col:
        raise ValueError(f"Rack collection '{rack_col_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{rack_col_name}' is not a rack cabinet collection")

    equipment_specs = payload.get("equipment", [])
    if not equipment_specs:
        raise ValueError("JSON 'equipment' list is empty or missing")

    bh             = col["rack_base_height_m"]
    u_height       = col["rack_u_height"]
    # Equipment bezel sits 2 mm behind the cabinet front face (Y=0 in rack-local
    # space). This gives realistic door-clearance without the bezel protruding into
    # the aisle. Do NOT use ps_m/2 — that puts the bezel inside the front posts.
    y_front        = 0.002
    equip_col_name = f"{rack_col_name}_Equipment"

    # Rack world transform — used to convert rack-local positions to world space.
    # Handles racks at any world XYZ and any Z rotation (including 180° for Row B).
    _rack_body  = bpy.data.objects.get(f"{rack_col_name}_Body")
    _rack_mat   = _rack_body.matrix_world.copy() if _rack_body else mathutils.Matrix.Identity(4)
    _rack_rot_z = _rack_body.rotation_euler.z if _rack_body else 0.0

    placed:  List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for spec in equipment_specs:
        u_slot  = spec.get("u_slot", 1)
        u_size  = spec.get("u_size", 1)
        eq_type = spec.get("type", "server").lower().replace(" ", "_")
        eq_name = spec.get("name", f"{eq_type}_{u_slot:02d}U")

        if u_slot < 1 or (u_slot + u_size - 1) > u_height:
            skipped.append({
                "name": eq_name, "u_slot": u_slot,
                "reason": f"U{u_slot}+{u_size} out of range for {u_height}U rack",
            })
            continue

        if eq_type not in _EQUIPMENT_CREATORS:
            skipped.append({
                "name": eq_name, "u_slot": u_slot,
                "reason": f"Unknown type '{eq_type}' — valid: {list(_EQUIPMENT_CREATORS)}",
            })
            continue

        z_bottom = bh + (u_slot - 1) * RACK_U_M

        if dry_run:
            placed.append({
                "name": eq_name, "type": eq_type,
                "u_slot": u_slot, "u_size": u_size,
                "z_bottom_m": round(z_bottom, 5), "dry_run": True,
            })
            continue

        try:
            kwargs: Dict[str, Any] = {
                "name":             eq_name,
                "u_size":           u_size,
                "collection_name":  equip_col_name,
                "random_variation": random_variation,
            }
            defaults = _CREATOR_DEFAULTS.get(eq_type, {})
            for key in _CREATOR_EXTRA_KEYS.get(eq_type, ()):
                kwargs[key] = spec.get(key, defaults.get(key))

            _EQUIPMENT_CREATORS[eq_type](**kwargs)

            eq_obj = bpy.data.objects.get(eq_name)
            if eq_obj:
                local_pos = mathutils.Vector((0.0, y_front, z_bottom))
                eq_obj.location = _rack_mat @ local_pos
                eq_obj.rotation_euler.z = _rack_rot_z

            placed.append({
                "name":       eq_name,
                "type":       eq_type,
                "u_slot":     u_slot,
                "u_size":     u_size,
                "z_bottom_m": round(z_bottom, 5),
            })

        except Exception as exc:
            skipped.append({"name": eq_name, "u_slot": u_slot, "reason": str(exc)})

    return {
        "rack":     rack_col_name,
        "placed":   placed,
        "skipped":  skipped,
        "count":    len(placed),
        "dry_run":  dry_run,
    }


# ── Tool 7: populate_rack_procedural ──────────────────────────────────────

@mcp.tool()
@thread_safe
def populate_rack_procedural(
    collection_name: str,
    preset: str = "server_dense",
    random_variation: bool = False,
    start_u: int = 1,
    end_u: int = 0,
) -> Dict[str, Any]:
    """
    Procedurally fill a rack with equipment based on a named preset.

    No JSON file required — equipment is created and positioned automatically.
    Useful for rapid level dressing and pipeline testing.

    Presets:
      'server_dense':  2U servers filling the rack, 1U blanks at boundaries
      'spine_leaf':    bottom 65% = 2U servers, top 35% = switches + patch panels
      'mixed_dc':      cycling pattern of patch panels / switches / servers

    collection_name:  rack collection (must have rack metadata)
    preset:           layout preset name
    random_variation: randomize equipment detail geometry for visual variety
    start_u:          first U slot to fill (default 1 = bottom of rack)
    end_u:            last U slot to fill (default 0 = rack top)
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    preset = preset.lower().replace(" ", "_")
    valid_presets = ("server_dense", "spine_leaf", "mixed_dc")
    if preset not in valid_presets:
        raise ValueError(f"preset must be one of {valid_presets}")

    u_height   = col["rack_u_height"]
    bh         = col["rack_base_height_m"]
    # Equipment bezel 2 mm behind cabinet front face — see populate_rack_from_json.
    y_front    = 0.002
    actual_end = end_u if end_u > 0 else u_height

    equip_col_name = f"{collection_name}_Equipment"

    # Rack world transform for local → world positioning.
    _rack_body  = bpy.data.objects.get(f"{collection_name}_Body")
    _rack_mat   = _rack_body.matrix_world.copy() if _rack_body else mathutils.Matrix.Identity(4)
    _rack_rot_z = _rack_body.rotation_euler.z if _rack_body else 0.0

    # ── Build placement sequence ───────────────────────────────────────────
    # Each entry: (u_size, eq_type, extra_kwargs)
    sequence: List[Tuple[int, str, Dict[str, Any]]] = []

    if preset == "server_dense":
        # Repeating 2U server — list is longer than any rack; truncated by loop
        sequence = [(2, "server", {"depth_mm": 700.0})] * 60

    elif preset == "spine_leaf":
        # Bottom 65 %: 2U servers; top 35 %: 1U switches + 1U patch panels
        boundary = max(start_u, int(u_height * 0.65))
        u = start_u
        while u <= boundary:
            sequence.append((2, "server",      {"depth_mm": 700.0}))
            u += 2
        while u <= actual_end:
            sequence.append((1, "switch",      {"port_count": 48}))
            sequence.append((1, "patch_panel", {"port_count": 24}))
            u += 2

    elif preset == "mixed_dc":
        pattern = [
            (1, "patch_panel", {"port_count": 24}),
            (1, "switch",      {"port_count": 48}),
            (2, "server",      {"depth_mm": 700.0}),
            (2, "server",      {"depth_mm": 700.0}),
            (1, "patch_panel", {"port_count": 24}),
            (2, "server",      {"depth_mm": 700.0}),
        ]
        repeats  = (u_height // 9) + 2
        sequence = pattern * repeats

    # ── Place equipment ────────────────────────────────────────────────────
    placed:    List[Dict[str, Any]] = []
    current_u = start_u
    seq_idx   = 0

    while current_u <= actual_end and seq_idx < len(sequence):
        u_size, eq_type, extra_kwargs = sequence[seq_idx]
        seq_idx += 1

        if current_u + u_size - 1 > actual_end:
            break

        eq_name  = f"{collection_name}_{eq_type}_{current_u:02d}U"
        z_bottom = bh + (current_u - 1) * RACK_U_M

        try:
            kwargs: Dict[str, Any] = {
                "name":             eq_name,
                "u_size":           u_size,
                "collection_name":  equip_col_name,
                "random_variation": random_variation,
                **extra_kwargs,
            }
            _EQUIPMENT_CREATORS[eq_type](**kwargs)

            eq_obj = bpy.data.objects.get(eq_name)
            if eq_obj:
                local_pos = mathutils.Vector((0.0, y_front, z_bottom))
                eq_obj.location = _rack_mat @ local_pos
                eq_obj.rotation_euler.z = _rack_rot_z

            placed.append({
                "name":       eq_name,
                "type":       eq_type,
                "u_slot":     current_u,
                "u_size":     u_size,
                "z_bottom_m": round(z_bottom, 5),
            })

        except Exception as exc:
            _log(f"populate_rack_procedural: skipped {eq_name} — {exc}")

        current_u += u_size

    return {
        "collection": collection_name,
        "preset":     preset,
        "placed":     placed,
        "count":      len(placed),
        "u_filled":   current_u - start_u,
    }


# ── Tool 8: clear_rack_population ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def clear_rack_population(
    collection_name: str,
    also_clear_sub_collection: bool = True,
) -> Dict[str, Any]:
    """
    Remove all equipment objects from a rack while preserving the cabinet.

    Targets the '<collection_name>_Equipment' sub-collection created by the
    population tools, plus any mesh objects in the rack collection that carry
    an 'equipment_type' custom property.

    The cabinet structure (posts, panels, rails, doors) is never touched.

    collection_name:             rack collection
    also_clear_sub_collection:   also remove the _Equipment collection itself
                                 (default True)
    """
    removed: List[str] = []

    equip_col_name = f"{collection_name}_Equipment"
    equip_col = bpy.data.collections.get(equip_col_name)
    if equip_col:
        for obj in list(equip_col.objects):
            removed.append(obj.name)
            bpy.data.objects.remove(obj, do_unlink=True)
        if also_clear_sub_collection:
            for parent_col in list(bpy.data.collections):
                if equip_col_name in [c.name for c in parent_col.children]:
                    parent_col.children.unlink(equip_col)
            scene_children = [c.name for c in bpy.context.scene.collection.children]
            if equip_col_name in scene_children:
                bpy.context.scene.collection.children.unlink(equip_col)
            bpy.data.collections.remove(equip_col)

    # Catch any stray equipment objects left in the rack collection itself
    rack_col = bpy.data.collections.get(collection_name)
    if rack_col:
        for obj in list(rack_col.objects):
            if obj.get("equipment_type"):
                removed.append(obj.name)
                bpy.data.objects.remove(obj, do_unlink=True)

    return {
        "collection": collection_name,
        "removed":    removed,
        "count":      len(removed),
    }


# ── Tool 9: export_equipment_set_ue5 ─────────────────────────────────────

@mcp.tool()
@thread_safe
def export_equipment_set_ue5(
    collection_name: str = "",
    output_dir: str = "",
) -> Dict[str, Any]:
    """
    Export all unique equipment types as individual FBX files for UE5.

    Groups objects by their 'equipment_type' custom property and exports one
    representative FBX per type (not per instance). UE5 uses a single
    StaticMesh for all identical instances, so one FBX per chassis type is
    sufficient — the engine instances it at runtime.

    Child SOCKET_ empties are included in each export so UE5 imports them
    as socket attachment points on the StaticMesh.

    collection_name: source collection — checks '<name>_Equipment' first, then
                     the named collection itself; empty = scan entire scene
    output_dir:      export directory (falls back to ue5_export_root scene property)
    """
    from constants import UE5_AXIS_FORWARD, UE5_AXIS_UP, UE5_SCALE_OPTIONS, UE5_MESH_SMOOTH

    if not output_dir:
        output_dir = bpy.context.scene.get("ue5_export_root", "")
    if not output_dir:
        raise ValueError(
            "No output_dir provided and ue5_export_root not set — "
            "run set_export_root first or pass output_dir explicitly"
        )
    os.makedirs(output_dir, exist_ok=True)

    # Collect candidates
    candidates: List[bpy.types.Object] = []
    if collection_name:
        equip_col = bpy.data.collections.get(f"{collection_name}_Equipment")
        rack_col  = bpy.data.collections.get(collection_name)
        if equip_col:
            candidates.extend(equip_col.objects)
        if rack_col:
            for o in rack_col.objects:
                if o not in candidates:
                    candidates.append(o)
    else:
        candidates = list(bpy.context.scene.objects)

    # One representative mesh per equipment_type
    seen_types: Dict[str, bpy.types.Object] = {}
    for obj in candidates:
        if obj.type != 'MESH':
            continue
        eq_type = obj.get("equipment_type")
        if eq_type and eq_type not in seen_types:
            seen_types[eq_type] = obj

    if not seen_types:
        return {"output_dir": output_dir, "exported": [], "errors": [],
                "type_count": 0, "message": "No equipment objects found"}

    exported: List[Dict[str, Any]] = []
    errors:   List[Dict[str, Any]] = []

    for eq_type, obj in seen_types.items():
        out_path = os.path.join(output_dir, f"EQ_{eq_type}.fbx")
        try:
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            for child in obj.children:
                child.select_set(True)
            bpy.context.view_layer.objects.active = obj

            bpy.ops.export_scene.fbx(
                filepath=out_path,
                use_selection=True,
                apply_unit_scale=True,
                apply_scale_options=UE5_SCALE_OPTIONS,
                use_mesh_modifiers=True,
                mesh_smooth_type=UE5_MESH_SMOOTH,
                axis_forward=UE5_AXIS_FORWARD,
                axis_up=UE5_AXIS_UP,
                add_leaf_bones=False,
                bake_anim=False,
            )
            tris = sum(len(f.vertices) - 2 for f in obj.data.polygons)
            exported.append({
                "equipment_type": eq_type,
                "source_object":  obj.name,
                "file":           out_path,
                "triangles":      tris,
                "sockets":        [c.name for c in obj.children
                                   if c.name.startswith(SOCKET_PREFIX)],
            })
        except Exception as exc:
            errors.append({"equipment_type": eq_type, "error": str(exc)})

    return {
        "output_dir": output_dir,
        "exported":   exported,
        "errors":     errors,
        "type_count": len(seen_types),
    }
