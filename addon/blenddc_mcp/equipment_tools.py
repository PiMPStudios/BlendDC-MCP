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
    material: str = "",
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
    if material:
        mat = bpy.data.materials.get(material)
        if mat:
            obj.data.materials.append(mat)
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

    _pbr('M_Aluminum',    (0.66, 0.66, 0.66), metallic=1.0, roughness=0.15)
    _pbr('M_Black',       (0.02, 0.02, 0.02), metallic=0.0, roughness=0.80)
    _pbr('M_BlackMatte',  (0.04, 0.04, 0.04), metallic=0.0, roughness=0.80)
    _pbr('M_DarkGrayMet', (0.12, 0.12, 0.13), metallic=0.85, roughness=0.28)
    _pbr('M_PlasticDark', (0.072, 0.078, 0.085), metallic=0.0, roughness=0.65)
    _pbr('M_Gold',        (0.82, 0.67, 0.22), metallic=1.0, roughness=0.12)
    _pbr('M_SFPCage',     (0.48, 0.50, 0.52), metallic=0.9, roughness=0.20)
    _pbr('M_PortVoid',    (0.01, 0.01, 0.01), metallic=0.0, roughness=0.95)
    _pbr('M_Display',     (0.003, 0.039, 0.093), metallic=0.0, roughness=0.02)
    _pbr('M_LED_Green',   (0.007, 0.150, 0.027), metallic=0.0, roughness=0.25,
          emission=(0.007, 0.150, 0.027), strength=8.0)
    _pbr('M_LED_Amber',   (0.150, 0.078, 0.003), metallic=0.0, roughness=0.25,
          emission=(0.150, 0.078, 0.003), strength=8.0)
    _pbr('M_LED_Off',     (0.040, 0.070, 0.040), metallic=0.0, roughness=0.55)
    _pbr('M_LED_White',   (0.90, 0.90, 0.90), metallic=0.0, roughness=0.20,
          emission=(0.90, 0.90, 0.90), strength=3.0)
    _pbr('M_White',       (0.90, 0.90, 0.90), metallic=0.0, roughness=0.50)
    _pbr('M_ServerBody',  (0.055, 0.060, 0.065), metallic=0.7,  roughness=0.35)
    _pbr('M_LED_Blue',    (0.003, 0.050, 0.180), metallic=0.0,  roughness=0.25,
          emission=(0.003, 0.050, 0.180), strength=8.0)


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
    join_mesh: bool = False,
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
        # ── Hero 1U server front + rear — all geometry in centred coords ──
        # Centred: X=0 = chassis CL, Y=0 = mid-depth, Z=0 = vertical midpoint
        # After the translation loop below, origin moves to front-face-bottom-centre.
        _sw_ensure_materials()

        HW = w / 2       # 0.223
        HH = h / 2       # 0.022225
        FRONT_Y = -(d / 2)
        BACK_Y  =  d / 2

        # Delete chassis rear face — replaced by holey bm_rear_bg panel below
        _bm_rear_del = bmesh.new()
        _bm_rear_del.from_mesh(parts[0].data)
        _rear_faces = [f for f in _bm_rear_del.faces
                       if f.calc_center_median().y > (d / 2 * 0.97)]
        bmesh.ops.delete(_bm_rear_del, geom=_rear_faces, context='FACES_ONLY')
        _bm_rear_del.to_mesh(parts[0].data)
        _bm_rear_del.free()
        parts[0].data.update()

        # ── Drive bay layout ──────────────────────────────────────────────
        L_MARG   = 0.010   # left margin (service tag zone)
        R_MARG   = 0.006   # right margin
        CTRL_W   = 0.058   # control panel zone width

        BAY_X0   = -HW + L_MARG
        BAY_X1   =  HW - R_MARG - CTRL_W
        BAY_ZONE_W = BAY_X1 - BAY_X0

        CTRL_X0  = BAY_X1
        CTRL_X1  = HW - R_MARG
        CTRL_CX  = (CTRL_X0 + CTRL_X1) / 2

        BAY_ZONE_H = h * 0.82
        BAY_Z0   = -BAY_ZONE_H / 2
        BAY_Z1   =  BAY_ZONE_H / 2
        BAY_RECESS_D = 0.010

        # Carrier grid
        bay_cols = max(1, (drive_bays + 1) // 2) if drive_bays > 0 else 0
        bay_rows = 2 if drive_bays > 1 else (1 if drive_bays == 1 else 0)
        if bay_cols > 0:
            GAP_X_BAY = 0.0015
            GAP_Z_BAY = 0.0018
            carrier_w = (BAY_ZONE_W - GAP_X_BAY * (bay_cols - 1)) / bay_cols
            carrier_h = (BAY_ZONE_H - GAP_Z_BAY * (bay_rows - 1)) / bay_rows
        actual_bays = drive_bays  # hero always uses exact requested count

        # ── Front plate ──────────────────────────────────────────────────
        fp_rect_holes = [
            (BAY_X0, BAY_X1, BAY_Z0, BAY_Z1),
            (-HW + 0.001, -HW + 0.013, -h * 0.22, h * 0.22),
            (CTRL_X0 + 0.001, CTRL_X1 - 0.001, BAY_Z0 + 0.001, BAY_Z1 - 0.001),
        ]
        parts.append(_sw_holey_plate(
            f"{name}_front_plate", FRONT_Y,
            fp_rect_holes, [],
            col, 'M_DarkGrayMet',
            x_min=-HW, x_max=HW,
            z_min=-HH, z_max=HH,
            outward_plus_y=False,
        ))

        # ── Bay background plate ──────────────────────────────────────────
        bm_bay_bg = bmesh.new()
        _sw_box(bm_bay_bg, BAY_X0, BAY_X1,
                FRONT_Y + 0.002, FRONT_Y + BAY_RECESS_D,
                BAY_Z0, BAY_Z1)
        parts.append(_sw_mesh_obj(f"{name}_bay_bg", bm_bay_bg, col, 'M_PlasticDark'))

        # ── Top louver strip ──────────────────────────────────────────────
        bm_louv = bmesh.new()
        LOUVER_H_DIM = 0.0004
        LOUVER_GAP_Z = 0.0014
        for i in range(6):
            z_top = HH - 0.0003 - i * (LOUVER_H_DIM + LOUVER_GAP_Z)
            _sw_box(bm_louv, -HW + 0.005, HW - 0.005,
                    FRONT_Y + h * 0.3, FRONT_Y + h * 0.8,
                    z_top - LOUVER_H_DIM, z_top + 0.00005)
        parts.append(_sw_mesh_obj(f"{name}_top_louvers", bm_louv, col, 'M_DarkGrayMet'))

        # ── Service tag ───────────────────────────────────────────────────
        bm_tag = bmesh.new()
        TAG_W = 0.011; TAG_H = h * 0.55; TAG_D = 0.0008
        TAG_X = -HW + 0.0075
        _sw_box(bm_tag, TAG_X - TAG_W / 2, TAG_X + TAG_W / 2,
                FRONT_Y - TAG_D, FRONT_Y,
                -TAG_H / 2, TAG_H / 2)
        # Knob at bottom
        _sw_box(bm_tag, TAG_X - 0.003, TAG_X + 0.003,
                FRONT_Y - 0.004, FRONT_Y,
                -TAG_H / 2 - 0.004, -TAG_H / 2)
        parts.append(_sw_mesh_obj(f"{name}_svc_tag", bm_tag, col, 'M_PlasticDark'))

        # ── Carrier faces, vents, handles, LEDs ──────────────────────────
        if bay_cols > 0:
            bm_carriers  = bmesh.new()
            bm_vents     = bmesh.new()
            bm_handles   = bmesh.new()
            bm_leds      = bmesh.new()
            bm_leds_write = bmesh.new()

            _lbl_objs_carr = []
            LABEL_SIZE = 0.0030
            LABEL_EXT  = 0.00030
            LABEL_Y_carr = FRONT_Y - 0.0020   # sits on carrier front face (CARR_Y1)

            def _add_carr_lbl(text_str, lx, lz):
                fc = bpy.data.curves.new("_srv_lbl_fc", type='FONT')
                fc.body = text_str
                fc.size = LABEL_SIZE
                fc.extrude = LABEL_EXT
                fc.align_x = 'CENTER'
                fc.align_y = 'CENTER'
                o = bpy.data.objects.new("_srv_lbl_obj", fc)
                bpy.context.scene.collection.objects.link(o)
                o.rotation_euler = (math.pi / 2, 0, 0)
                o.location = (lx, LABEL_Y_carr, lz)
                _lbl_objs_carr.append(o)

            for row in range(bay_rows):
                for col_i in range(bay_cols):
                    idx = row * bay_cols + col_i
                    if idx >= drive_bays:
                        break
                    cx = BAY_X0 + (col_i + 0.5) * carrier_w + col_i * GAP_X_BAY
                    cz = BAY_Z0 + (row + 0.5) * carrier_h + row * GAP_Z_BAY

                    # Carrier face (slightly proud of front plate)
                    CARR_Y0 = FRONT_Y + 0.0002
                    CARR_Y1 = FRONT_Y - 0.0018
                    _sw_box(bm_carriers,
                            cx - carrier_w / 2 + 0.001, cx + carrier_w / 2 - 0.001,
                            CARR_Y1, CARR_Y0,
                            cz - carrier_h / 2 + 0.001, cz + carrier_h / 2 - 0.001)

                    # Vent slots (3 horizontal, right 65% of carrier)
                    VENT_H_DIM = 0.0007; VENT_D = 0.0003; VENT_W = carrier_w * 0.60
                    vx0 = cx - VENT_W / 2 + carrier_w * 0.10
                    vx1 = cx + VENT_W / 2 + carrier_w * 0.10
                    for vi in range(3):
                        vz = cz + (vi - 1) * 0.0030
                        _sw_box(bm_vents, vx0, vx1,
                                CARR_Y1 - VENT_D, CARR_Y1,
                                vz - VENT_H_DIM / 2, vz + VENT_H_DIM / 2)

                    if qf["detailed_handles"]:
                        # L-shaped pull handle (left side)
                        HDL_X = cx - carrier_w / 2 + 0.0045
                        HDL_W_DIM = 0.0055; HDL_H_DIM = carrier_h * 0.72; HDL_D = 0.0038
                        # Vertical shaft
                        _sw_box(bm_handles,
                                HDL_X - HDL_W_DIM / 2, HDL_X + HDL_W_DIM / 2,
                                FRONT_Y - HDL_D, FRONT_Y - 0.0002,
                                cz - HDL_H_DIM / 2, cz + HDL_H_DIM / 2)
                        # Toe tab (horizontal hook at bottom)
                        _sw_box(bm_handles,
                                HDL_X - HDL_W_DIM / 2, HDL_X - HDL_W_DIM / 2 + carrier_w * 0.22,
                                FRONT_Y - HDL_D, FRONT_Y - 0.0002,
                                cz - HDL_H_DIM / 2, cz - HDL_H_DIM / 2 + 0.0042)

                    if qf["led_emissive"]:
                        # Read LED (green) top-right, write LED (amber) directly below
                        LED_X    = cx + carrier_w / 2 - 0.0060
                        LED_RZ   = cz + carrier_h / 2 - 0.0035          # read Z
                        LED_WZ   = LED_RZ - 0.0035                       # write Z, 3.5mm below
                        _sw_box(bm_leds,
                                LED_X - 0.0012, LED_X + 0.0012,
                                CARR_Y1 - 0.0008, CARR_Y1,
                                LED_RZ - 0.0012, LED_RZ + 0.0012)
                        _sw_box(bm_leds_write,
                                LED_X - 0.0012, LED_X + 0.0012,
                                CARR_Y1 - 0.0008, CARR_Y1,
                                LED_WZ - 0.0012, LED_WZ + 0.0012)

                    if qf["bezel"]:
                        # Drive bay label
                        _add_carr_lbl(str(idx + 1), cx, cz + carrier_h / 2 - 0.0025)

            parts.append(_sw_mesh_obj(f"{name}_carrier_faces",       bm_carriers,   col, 'M_PlasticDark'))
            parts.append(_sw_mesh_obj(f"{name}_carrier_vents",       bm_vents,      col, 'M_Black'))
            parts.append(_sw_mesh_obj(f"{name}_carrier_handles",     bm_handles,    col, 'M_Black'))
            parts.append(_sw_mesh_obj(f"{name}_carrier_leds",        bm_leds,       col, 'M_LED_Green'))
            parts.append(_sw_mesh_obj(f"{name}_carrier_leds_write",  bm_leds_write, col, 'M_LED_Amber'))

            # Bake drive bay labels
            if qf["bezel"] and _lbl_objs_carr:
                bpy.context.view_layer.update()
                dep = bpy.context.evaluated_depsgraph_get()
                bm_lbl_carr = bmesh.new()
                for fo in _lbl_objs_carr:
                    me_tmp = bpy.data.meshes.new_from_object(fo.evaluated_get(dep))
                    bm_t = bmesh.new()
                    bm_t.from_mesh(me_tmp)
                    bmesh.ops.transform(bm_t, matrix=fo.matrix_world, verts=bm_t.verts[:])
                    nv = [bm_lbl_carr.verts.new(v.co) for v in bm_t.verts]
                    bm_lbl_carr.verts.ensure_lookup_table()
                    bm_t.verts.ensure_lookup_table()
                    bm_t.faces.ensure_lookup_table()
                    for f_t in bm_t.faces:
                        try: bm_lbl_carr.faces.new([nv[v.index] for v in f_t.verts])
                        except: pass
                    bm_t.free()
                    bpy.data.meshes.remove(me_tmp)
                    fc_data = fo.data
                    bpy.data.objects.remove(fo)
                    bpy.data.curves.remove(fc_data)
                parts.append(_sw_mesh_obj(f"{name}_bay_labels", bm_lbl_carr, col, 'M_White'))

        # ── Control panel — tiled around USB cutouts ──────────────────────
        bm_ctrl = bmesh.new()
        _CB_X0 = CTRL_X0 + 0.001;  _CB_X1 = CTRL_X1 - 0.001
        _CB_Y0 = FRONT_Y - 0.0005; _CB_Y1 = FRONT_Y + 0.0020
        _CB_Z0 = BAY_Z0 + 0.001;   _CB_Z1 = BAY_Z1 - 0.001
        # Per-port tiling — centres spaced ≥8mm apart so frames don't overlap
        _USB_OW = 0.0130; _USB_OH = 0.0060
        _USB_CX = CTRL_CX - 0.008
        _USB_X0 = _USB_CX - _USB_OW / 2; _USB_X1 = _USB_CX + _USB_OW / 2
        _P1_CZ  = -HH * 0.15;  _P1_Z0 = _P1_CZ - _USB_OH / 2;  _P1_Z1 = _P1_CZ + _USB_OH / 2
        _P2_CZ  = -HH * 0.55;  _P2_Z0 = _P2_CZ - _USB_OH / 2;  _P2_Z1 = _P2_CZ + _USB_OH / 2
        # Left / right columns (full ctrl height)
        _sw_box(bm_ctrl, _CB_X0,  _USB_X0, _CB_Y0, _CB_Y1, _CB_Z0, _CB_Z1)
        _sw_box(bm_ctrl, _USB_X1, _CB_X1,  _CB_Y0, _CB_Y1, _CB_Z0, _CB_Z1)
        # USB column strips: below port2 | [hole P2] | between | [hole P1] | above port1
        _sw_box(bm_ctrl, _USB_X0, _USB_X1, _CB_Y0, _CB_Y1, _CB_Z0,  _P2_Z0)
        _sw_box(bm_ctrl, _USB_X0, _USB_X1, _CB_Y0, _CB_Y1, _P2_Z1,  _P1_Z0)
        _sw_box(bm_ctrl, _USB_X0, _USB_X1, _CB_Y0, _CB_Y1, _P1_Z1,  _CB_Z1)
        parts.append(_sw_mesh_obj(f"{name}_ctrl_bg", bm_ctrl, col, 'M_DarkGrayMet'))

        # Power button — 8-sided cap head
        PWR_CX = CTRL_CX; PWR_CZ = HH * 0.52; PWR_R = 0.0038; PWR_T = 0.0030
        PWR_SEG = 8; PWR_Y = FRONT_Y - 0.0030
        bm_pwr = bmesh.new()
        fv_p = []; bv_p = []
        for i in range(PWR_SEG):
            a = math.pi / PWR_SEG + 2 * math.pi * i / PWR_SEG
            fv_p.append(bm_pwr.verts.new((PWR_CX + PWR_R * math.cos(a), PWR_Y,           PWR_CZ + PWR_R * math.sin(a))))
            bv_p.append(bm_pwr.verts.new((PWR_CX + PWR_R * math.cos(a), PWR_Y + PWR_T,   PWR_CZ + PWR_R * math.sin(a))))
        cf_p = bm_pwr.verts.new((PWR_CX, PWR_Y,           PWR_CZ))
        cb_p = bm_pwr.verts.new((PWR_CX, PWR_Y + PWR_T,   PWR_CZ))
        for i in range(PWR_SEG):
            n = (i + 1) % PWR_SEG
            _sw_F(bm_pwr, [fv_p[i], fv_p[n], bv_p[n], bv_p[i]])
            try: bm_pwr.faces.new([cf_p, fv_p[n], fv_p[i]])
            except: pass
            try: bm_pwr.faces.new([cb_p, bv_p[i], bv_p[n]])
            except: pass
        parts.append(_sw_mesh_obj(f"{name}_pwr_btn", bm_pwr, col, 'M_Black'))

        # Power LED ring
        if qf["led_emissive"]:
            PWR_RING_OR = PWR_R + 0.0018; PWR_RING_IR = PWR_R + 0.0004; PWR_RING_D = 0.0005
            bm_pwr_led = bmesh.new()
            N_RNG = 16
            fr_o = []; fr_i = []; bk_o = []; bk_i = []
            for i in range(N_RNG):
                a = 2 * math.pi * i / N_RNG
                co = math.cos(a); si = math.sin(a)
                fr_o.append(bm_pwr_led.verts.new((PWR_CX + PWR_RING_OR * co, PWR_Y,                  PWR_CZ + PWR_RING_OR * si)))
                fr_i.append(bm_pwr_led.verts.new((PWR_CX + PWR_RING_IR * co, PWR_Y,                  PWR_CZ + PWR_RING_IR * si)))
                bk_o.append(bm_pwr_led.verts.new((PWR_CX + PWR_RING_OR * co, PWR_Y + PWR_RING_D,     PWR_CZ + PWR_RING_OR * si)))
                bk_i.append(bm_pwr_led.verts.new((PWR_CX + PWR_RING_IR * co, PWR_Y + PWR_RING_D,     PWR_CZ + PWR_RING_IR * si)))
            for i in range(N_RNG):
                n = (i + 1) % N_RNG
                _sw_F(bm_pwr_led, [fr_o[i], fr_i[i], fr_i[n], fr_o[n]])   # front annulus
                _sw_F(bm_pwr_led, [bk_o[i], bk_o[n], bk_i[n], bk_i[i]])   # back annulus
                _sw_F(bm_pwr_led, [fr_o[i], fr_o[n], bk_o[n], bk_o[i]])   # outer wall
                _sw_F(bm_pwr_led, [fr_i[i], bk_i[i], bk_i[n], fr_i[n]])   # inner wall
            parts.append(_sw_mesh_obj(f"{name}_pwr_led", bm_pwr_led, col, 'M_LED_Green'))

        # UID button — small square
        bm_uid = bmesh.new()
        _sw_box(bm_uid,
                CTRL_CX + 0.012 - 0.0025, CTRL_CX + 0.012 + 0.0025,
                FRONT_Y - 0.0028, FRONT_Y - 0.0005,
                HH * 0.18 - 0.0025, HH * 0.18 + 0.0025)
        parts.append(_sw_mesh_obj(f"{name}_uid_btn", bm_uid, col, 'M_DarkGrayMet'))

        if qf["led_emissive"]:
            # UID LED ring — thin box frame around button
            bm_uid_led = bmesh.new()
            UID_CX_u = CTRL_CX + 0.012; UID_CZ_u = HH * 0.18
            UID_OR = 0.0040; UID_IR = 0.0025; UID_D = 0.0004
            _sw_box(bm_uid_led,  # top rail
                    UID_CX_u - UID_OR, UID_CX_u + UID_OR,
                    FRONT_Y - 0.0028, FRONT_Y - 0.0028 + UID_D,
                    UID_CZ_u + UID_IR, UID_CZ_u + UID_OR)
            _sw_box(bm_uid_led,  # bottom rail
                    UID_CX_u - UID_OR, UID_CX_u + UID_OR,
                    FRONT_Y - 0.0028, FRONT_Y - 0.0028 + UID_D,
                    UID_CZ_u - UID_OR, UID_CZ_u - UID_IR)
            _sw_box(bm_uid_led,  # left rail
                    UID_CX_u - UID_OR, UID_CX_u - UID_IR,
                    FRONT_Y - 0.0028, FRONT_Y - 0.0028 + UID_D,
                    UID_CZ_u - UID_OR, UID_CZ_u + UID_OR)
            _sw_box(bm_uid_led,  # right rail
                    UID_CX_u + UID_IR, UID_CX_u + UID_OR,
                    FRONT_Y - 0.0028, FRONT_Y - 0.0028 + UID_D,
                    UID_CZ_u - UID_OR, UID_CZ_u + UID_OR)
            parts.append(_sw_mesh_obj(f"{name}_uid_led", bm_uid_led, col, 'M_LED_Blue'))

        # Status LEDs (3 small, vertically stacked left of power button)
        SLED_CX = CTRL_X0 + 0.010
        _sled_mat_defs = [
            (0.38,  'M_LED_Green'),
            (0.12,  'M_LED_Amber'),
            (-0.14, 'M_LED_Green'),
        ]
        _sled_bms: dict = {}
        for lz_frac, mat in _sled_mat_defs:
            lz = HH * lz_frac
            if mat not in _sled_bms:
                _sled_bms[mat] = bmesh.new()
            _sw_box(_sled_bms[mat],
                    SLED_CX - 0.0015, SLED_CX + 0.0015,
                    FRONT_Y - 0.0025, FRONT_Y - 0.0005,
                    lz - 0.0015, lz + 0.0015)
        for mat, bm_s in _sled_bms.items():
            suffix = mat.replace('M_LED_', '').lower()
            parts.append(_sw_mesh_obj(f"{name}_sled_{suffix}", bm_s, col, mat))

        # Front USB-A ports (×2) — annular frame + tunnel + tongue
        bm_usb = bmesh.new()
        USB_OW = 0.0130; USB_OH = 0.0060; USB_IW = 0.0100; USB_IH = 0.0035; USB_D = 0.0100
        USB_CX = CTRL_CX - 0.008
        USB_WALL = (USB_OW - USB_IW) / 2
        for ui, USB_CZ in enumerate([-HH * 0.15, -HH * 0.55]):
            FY = FRONT_Y - 0.0005  # front face of frame (slightly recessed)
            BY = FY + USB_D         # back of tunnel
            # Front face annular frame (4 rails)
            _sw_box(bm_usb, USB_CX - USB_OW/2, USB_CX + USB_OW/2,   # top
                    FY - 0.0008, FY,
                    USB_CZ + USB_IH/2, USB_CZ + USB_OH/2)
            _sw_box(bm_usb, USB_CX - USB_OW/2, USB_CX + USB_OW/2,   # bottom
                    FY - 0.0008, FY,
                    USB_CZ - USB_OH/2, USB_CZ - USB_IH/2)
            _sw_box(bm_usb, USB_CX - USB_OW/2, USB_CX - USB_IW/2,   # left
                    FY - 0.0008, FY,
                    USB_CZ - USB_OH/2, USB_CZ + USB_OH/2)
            _sw_box(bm_usb, USB_CX + USB_IW/2, USB_CX + USB_OW/2,   # right
                    FY - 0.0008, FY,
                    USB_CZ - USB_OH/2, USB_CZ + USB_OH/2)
            # Tunnel walls (top, bottom, left, right)
            _sw_box(bm_usb, USB_CX - USB_OW/2, USB_CX + USB_OW/2,
                    FY, BY,
                    USB_CZ + USB_IH/2, USB_CZ + USB_OH/2)
            _sw_box(bm_usb, USB_CX - USB_OW/2, USB_CX + USB_OW/2,
                    FY, BY,
                    USB_CZ - USB_OH/2, USB_CZ - USB_IH/2)
            _sw_box(bm_usb, USB_CX - USB_OW/2, USB_CX - USB_IW/2,
                    FY, BY,
                    USB_CZ - USB_OH/2, USB_CZ + USB_OH/2)
            _sw_box(bm_usb, USB_CX + USB_IW/2, USB_CX + USB_OW/2,
                    FY, BY,
                    USB_CZ - USB_OH/2, USB_CZ + USB_OH/2)
            # Back cap
            _sw_box(bm_usb, USB_CX - USB_OW/2, USB_CX + USB_OW/2,
                    BY - 0.0005, BY,
                    USB_CZ - USB_OH/2, USB_CZ + USB_OH/2)
            # Plastic tongue (upper half of inner cavity)
            _sw_box(bm_usb, USB_CX - USB_IW/2 + 0.001, USB_CX + USB_IW/2 - 0.001,
                    FY + 0.002, BY - 0.001,
                    USB_CZ, USB_CZ + USB_IH/2 - 0.0003)
        parts.append(_sw_mesh_obj(f"{name}_usb_front", bm_usb, col, 'M_PlasticDark'))

        # ── Rear face ───────────────────────────────────────────────────
        # Dual PSU blocks
        PSU_W_EA = 0.078; PSU_GAP  = 0.006
        PSU_X0_L = -HW + 0.003
        PSU_X1_L = PSU_X0_L + PSU_W_EA
        PSU_X0_R = PSU_X1_L + PSU_GAP
        PSU_X1_R = PSU_X0_R + PSU_W_EA

        bm_psu         = bmesh.new()
        bm_psu_hdl     = bmesh.new()
        bm_psu_exhaust = bmesh.new()
        bm_psu_led     = bmesh.new()

        # IEC C14 shared geometry for each PSU (bmesh per PSU to stay clean)
        IEC_CUT_W_s = 0.0280; IEC_CUT_H_s = 0.0220
        IEC_FLG_W_s = 0.0390; IEC_FLG_H_s = 0.0310
        IEC_SOCK_D_s = 0.0200; IEC_FLG_T_s = 0.0025
        S_WALL_s = 0.002

        bm_iec_all  = bmesh.new()
        bm_flg_all  = bmesh.new()
        bm_iec_scr_all = bmesh.new()
        bm_iec_con_all = bmesh.new()

        def _build_iec_at(psu_cx_iec, psu_cz_iec):
            """Build IEC C14 inlet geometry into shared bmeshes at given centre."""
            CX_iec = psu_cx_iec; CZ_iec = psu_cz_iec
            ox0_iec = CX_iec - IEC_FLG_W_s/2; ox1_iec = CX_iec + IEC_FLG_W_s/2
            oz0_iec = CZ_iec - IEC_FLG_H_s/2; oz1_iec = CZ_iec + IEC_FLG_H_s/2
            cx0_iec = CX_iec - IEC_CUT_W_s/2; cx1_iec = CX_iec + IEC_CUT_W_s/2
            cz0_iec = CZ_iec - IEC_CUT_H_s/2; cz1_iec = CZ_iec + IEC_CUT_H_s/2
            ix0_iec = cx0_iec + S_WALL_s;      ix1_iec = cx1_iec - S_WALL_s
            iz0_iec = cz0_iec + S_WALL_s;      iz1_iec = cz1_iec - S_WALL_s
            FLG_Y0_iec = BACK_Y; FLG_Y1_iec = BACK_Y + IEC_FLG_T_s
            SOCK_Y1_iec = BACK_Y - IEC_SOCK_D_s

            # Body
            of_v = [bm_iec_all.verts.new((ox0_iec, FLG_Y0_iec, oz0_iec)),
                    bm_iec_all.verts.new((ox1_iec, FLG_Y0_iec, oz0_iec)),
                    bm_iec_all.verts.new((ox1_iec, FLG_Y0_iec, oz1_iec)),
                    bm_iec_all.verts.new((ox0_iec, FLG_Y0_iec, oz1_iec))]
            ob_v = [bm_iec_all.verts.new((ox0_iec, SOCK_Y1_iec, oz0_iec)),
                    bm_iec_all.verts.new((ox1_iec, SOCK_Y1_iec, oz0_iec)),
                    bm_iec_all.verts.new((ox1_iec, SOCK_Y1_iec, oz1_iec)),
                    bm_iec_all.verts.new((ox0_iec, SOCK_Y1_iec, oz1_iec))]
            cf_v = [bm_iec_all.verts.new((cx0_iec, FLG_Y0_iec, cz0_iec)),
                    bm_iec_all.verts.new((cx1_iec, FLG_Y0_iec, cz0_iec)),
                    bm_iec_all.verts.new((cx1_iec, FLG_Y0_iec, cz1_iec)),
                    bm_iec_all.verts.new((cx0_iec, FLG_Y0_iec, cz1_iec))]
            it_v = [bm_iec_all.verts.new((ix0_iec, FLG_Y0_iec, iz0_iec)),
                    bm_iec_all.verts.new((ix1_iec, FLG_Y0_iec, iz0_iec)),
                    bm_iec_all.verts.new((ix1_iec, FLG_Y0_iec, iz1_iec)),
                    bm_iec_all.verts.new((ix0_iec, FLG_Y0_iec, iz1_iec))]
            ib_v = [bm_iec_all.verts.new((ix0_iec, SOCK_Y1_iec, iz0_iec)),
                    bm_iec_all.verts.new((ix1_iec, SOCK_Y1_iec, iz0_iec)),
                    bm_iec_all.verts.new((ix1_iec, SOCK_Y1_iec, iz1_iec)),
                    bm_iec_all.verts.new((ix0_iec, SOCK_Y1_iec, iz1_iec))]
            _sw_F(bm_iec_all, [of_v[0], of_v[1], cf_v[1], cf_v[0]])
            _sw_F(bm_iec_all, [of_v[3], cf_v[3], cf_v[2], of_v[2]])
            _sw_F(bm_iec_all, [of_v[0], cf_v[0], cf_v[3], of_v[3]])
            _sw_F(bm_iec_all, [of_v[1], of_v[2], cf_v[2], cf_v[1]])
            _sw_F(bm_iec_all, [of_v[0], ob_v[0], ob_v[1], of_v[1]])
            _sw_F(bm_iec_all, [of_v[3], of_v[2], ob_v[2], ob_v[3]])
            _sw_F(bm_iec_all, [of_v[0], of_v[3], ob_v[3], ob_v[0]])
            _sw_F(bm_iec_all, [of_v[1], ob_v[1], ob_v[2], of_v[2]])
            _sw_F(bm_iec_all, [ob_v[0], ob_v[3], ob_v[2], ob_v[1]])
            _sw_F(bm_iec_all, [cf_v[0], cf_v[1], it_v[1], it_v[0]])
            _sw_F(bm_iec_all, [cf_v[3], it_v[3], it_v[2], cf_v[2]])
            _sw_F(bm_iec_all, [cf_v[0], it_v[0], it_v[3], cf_v[3]])
            _sw_F(bm_iec_all, [cf_v[1], cf_v[2], it_v[2], it_v[1]])
            _sw_F(bm_iec_all, [it_v[0], it_v[1], ib_v[1], ib_v[0]])
            _sw_F(bm_iec_all, [it_v[3], ib_v[3], ib_v[2], it_v[2]])
            _sw_F(bm_iec_all, [it_v[0], ib_v[0], ib_v[3], it_v[3]])
            _sw_F(bm_iec_all, [it_v[1], it_v[2], ib_v[2], ib_v[1]])
            _sw_F(bm_iec_all, [ib_v[0], ib_v[1], ib_v[2], ib_v[3]])

            # Flange
            f0_v2 = [bm_flg_all.verts.new((ox0_iec, FLG_Y0_iec, oz0_iec)),
                     bm_flg_all.verts.new((ox1_iec, FLG_Y0_iec, oz0_iec)),
                     bm_flg_all.verts.new((ox1_iec, FLG_Y0_iec, oz1_iec)),
                     bm_flg_all.verts.new((ox0_iec, FLG_Y0_iec, oz1_iec))]
            f1_v2 = [bm_flg_all.verts.new((ox0_iec, FLG_Y1_iec, oz0_iec)),
                     bm_flg_all.verts.new((ox1_iec, FLG_Y1_iec, oz0_iec)),
                     bm_flg_all.verts.new((ox1_iec, FLG_Y1_iec, oz1_iec)),
                     bm_flg_all.verts.new((ox0_iec, FLG_Y1_iec, oz1_iec))]
            c0_v2 = [bm_flg_all.verts.new((cx0_iec, FLG_Y0_iec, cz0_iec)),
                     bm_flg_all.verts.new((cx1_iec, FLG_Y0_iec, cz0_iec)),
                     bm_flg_all.verts.new((cx1_iec, FLG_Y0_iec, cz1_iec)),
                     bm_flg_all.verts.new((cx0_iec, FLG_Y0_iec, cz1_iec))]
            c1_v2 = [bm_flg_all.verts.new((cx0_iec, FLG_Y1_iec, cz0_iec)),
                     bm_flg_all.verts.new((cx1_iec, FLG_Y1_iec, cz0_iec)),
                     bm_flg_all.verts.new((cx1_iec, FLG_Y1_iec, cz1_iec)),
                     bm_flg_all.verts.new((cx0_iec, FLG_Y1_iec, cz1_iec))]
            _sw_F(bm_flg_all, [f1_v2[0], f1_v2[1], c1_v2[1], c1_v2[0]])
            _sw_F(bm_flg_all, [f1_v2[3], c1_v2[3], c1_v2[2], f1_v2[2]])
            _sw_F(bm_flg_all, [f1_v2[0], c1_v2[0], c1_v2[3], f1_v2[3]])
            _sw_F(bm_flg_all, [f1_v2[1], f1_v2[2], c1_v2[2], c1_v2[1]])
            _sw_F(bm_flg_all, [f0_v2[0], c0_v2[0], c0_v2[1], f0_v2[1]])
            _sw_F(bm_flg_all, [f0_v2[3], f0_v2[2], c0_v2[2], c0_v2[3]])
            _sw_F(bm_flg_all, [f0_v2[0], f0_v2[3], c0_v2[3], c0_v2[0]])
            _sw_F(bm_flg_all, [f0_v2[1], c0_v2[1], c0_v2[2], f0_v2[2]])
            for i in range(4):
                _sw_F(bm_flg_all, [f0_v2[i], f1_v2[i], f1_v2[(i+1)%4], f0_v2[(i+1)%4]])

            # IEC screws (2 per inlet)
            SR_iec = 0.002; ST_iec = 0.001; NS_iec = 12
            for scx_iec in [CX_iec - (IEC_CUT_W_s/2 + (IEC_FLG_W_s/2 - IEC_CUT_W_s/2)/2),
                            CX_iec + (IEC_CUT_W_s/2 + (IEC_FLG_W_s/2 - IEC_CUT_W_s/2)/2)]:
                rim_b_v = []; rim_f_v = []
                for i in range(NS_iec):
                    a = 2 * math.pi * i / NS_iec
                    rim_b_v.append(bm_iec_scr_all.verts.new((scx_iec + SR_iec*math.cos(a), FLG_Y1_iec,            CZ_iec + SR_iec*math.sin(a))))
                    rim_f_v.append(bm_iec_scr_all.verts.new((scx_iec + SR_iec*math.cos(a), FLG_Y1_iec + ST_iec,   CZ_iec + SR_iec*math.sin(a))))
                cf_iec = bm_iec_scr_all.verts.new((scx_iec, FLG_Y1_iec + ST_iec, CZ_iec))
                for i in range(NS_iec):
                    _sw_F(bm_iec_scr_all, [rim_b_v[i], rim_f_v[i], rim_f_v[(i+1)%NS_iec], rim_b_v[(i+1)%NS_iec]])
                    try: bm_iec_scr_all.faces.new([cf_iec, rim_f_v[i], rim_f_v[(i+1)%NS_iec]])
                    except: pass

            # IEC contacts (E/L/N)
            PY0_iec2 = SOCK_Y1_iec + 0.0005; PY1_iec2 = PY0_iec2 + 0.001
            def _blade_psu(cx_b, cz_b, bw, bh):
                _sw_box(bm_iec_con_all, cx_b - bw/2, cx_b + bw/2,
                        PY0_iec2, PY1_iec2, cz_b - bh/2, cz_b + bh/2)
            _blade_psu(CX_iec,            CZ_iec + 0.0055, 0.007,  0.005)
            _blade_psu(CX_iec + 0.0075,   CZ_iec - 0.0045, 0.0038, 0.009)
            _blade_psu(CX_iec - 0.0075,   CZ_iec - 0.0045, 0.0038, 0.009)

        for psu_x0, psu_x1 in [(PSU_X0_L, PSU_X1_L), (PSU_X0_R, PSU_X1_R)]:
            psu_cx_l = (psu_x0 + psu_x1) / 2

            # PSU face plate — tiled around IEC C14 cutout opening
            _fp_cx_iec = psu_cx_l
            _fp_iz0 = -HH * 0.35 - IEC_CUT_H_s / 2
            _fp_iz1 = -HH * 0.35 + IEC_CUT_H_s / 2
            _fp_ix0 = _fp_cx_iec - IEC_CUT_W_s / 2
            _fp_ix1 = _fp_cx_iec + IEC_CUT_W_s / 2
            _fp_x0 = psu_x0 + 0.002;  _fp_x1 = psu_x1 - 0.002
            _fp_z0 = -HH + 0.003;     _fp_z1 = HH - 0.003
            _sw_box(bm_psu, _fp_x0,  _fp_ix0, BACK_Y, BACK_Y+0.002, _fp_z0, _fp_z1)   # left
            _sw_box(bm_psu, _fp_ix1, _fp_x1,  BACK_Y, BACK_Y+0.002, _fp_z0, _fp_z1)   # right
            _sw_box(bm_psu, _fp_ix0, _fp_ix1, BACK_Y, BACK_Y+0.002, _fp_z0, _fp_iz0)  # below IEC
            _sw_box(bm_psu, _fp_ix0, _fp_ix1, BACK_Y, BACK_Y+0.002, _fp_iz1, _fp_z1)  # above IEC

            # Handle bar at top
            _sw_box(bm_psu_hdl, psu_x0 + 0.005, psu_x1 - 0.005,
                    BACK_Y + 0.001, BACK_Y + 0.006,
                    HH - 0.006, HH - 0.002)

            # IEC C14 at each PSU
            _build_iec_at(psu_cx_l, -HH * 0.35)

            # Exhaust slots — confined between IEC flange top and handle bar
            _IEC_CZ  = -HH * 0.35
            _EX_Z0   = _IEC_CZ + IEC_FLG_H_s / 2 + 0.0020   # 2mm above flange top (not cutout)
            _EX_Z1   = HH - 0.0080                            # clear of handle bar
            _N_EXHST = 5; _SL_H = 0.0011
            _gap     = max(0.0006, (_EX_Z1 - _EX_Z0 - _N_EXHST * _SL_H) / (_N_EXHST - 1))
            for ei in range(_N_EXHST):
                ez = _EX_Z0 + ei * (_SL_H + _gap)
                _sw_box(bm_psu_exhaust,
                        psu_x0 + 0.006, psu_x1 - 0.006,
                        BACK_Y + 0.0025, BACK_Y + 0.0030,
                        ez, ez + _SL_H)

            # Flanking exhaust slots — left and right of IEC flange, same Z band as flange
            _FLK_FX0  = psu_cx_l - IEC_FLG_W_s / 2   # flange left X edge
            _FLK_FX1  = psu_cx_l + IEC_FLG_W_s / 2   # flange right X edge
            _FLK_Z0   = _IEC_CZ - IEC_FLG_H_s / 2 + 0.002
            _FLK_Z1   = _IEC_CZ + IEC_FLG_H_s / 2 - 0.002
            _N_FLK = 10
            _flk_gap = max(0.0006, (_FLK_Z1 - _FLK_Z0 - _N_FLK * _SL_H) / (_N_FLK - 1))
            for fi in range(_N_FLK):
                fz = _FLK_Z0 + fi * (_SL_H + _flk_gap)
                _sw_box(bm_psu_exhaust,                          # left of flange
                        psu_x0 + 0.004, _FLK_FX0 - 0.002,
                        BACK_Y + 0.0025, BACK_Y + 0.0030,
                        fz, fz + _SL_H)
                _sw_box(bm_psu_exhaust,                          # right of flange
                        _FLK_FX1 + 0.002, psu_x1 - 0.004,
                        BACK_Y + 0.0025, BACK_Y + 0.0030,
                        fz, fz + _SL_H)

            # PSU LED
            _sw_box(bm_psu_led,
                    psu_cx_l + PSU_W_EA * 0.30, psu_cx_l + PSU_W_EA * 0.30 + 0.004,
                    BACK_Y + 0.001, BACK_Y + 0.003,
                    HH * 0.72, HH * 0.72 + 0.004)

        parts.append(_sw_mesh_obj(f"{name}_psu_faces",    bm_psu,         col, 'M_Aluminum'))
        parts.append(_sw_mesh_obj(f"{name}_psu_handles",  bm_psu_hdl,     col, 'M_Black'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_body", bm_iec_all,     col, 'M_BlackMatte'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_flange", bm_flg_all,   col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_screws", bm_iec_scr_all, col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_contacts", bm_iec_con_all, col, 'M_Gold'))
        parts.append(_sw_mesh_obj(f"{name}_psu_exhaust",  bm_psu_exhaust, col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_psu_leds",     bm_psu_led,     col, 'M_LED_Green'))

        # PCIe bracket zone — fixed 46mm for 2 × 21mm brackets + margins
        PCIE_X0     = PSU_X1_R + 0.008   # 8mm gap after right PSU
        PCIE_W      = 0.046               # 2 slots × ~21mm + small margins
        pcie_slot_w = (PCIE_W - 0.004) / 2   # ~21mm per slot

        bm_pcie      = bmesh.new()
        bm_pcie_screws = bmesh.new()

        for si in range(2):
            sx0 = PCIE_X0 + si * (pcie_slot_w + 0.004)
            sx1 = sx0 + pcie_slot_w
            scx_p = (sx0 + sx1) / 2

            # Bracket face
            _sw_box(bm_pcie, sx0 + 0.001, sx1 - 0.001,
                    BACK_Y, BACK_Y + 0.0015,
                    -HH + 0.002, HH - 0.003)

            # Vent bars (10 horizontal)
            for vi in range(10):
                vz_p = -HH * 0.65 + vi * (h * 0.78 / 10)
                _sw_box(bm_pcie, sx0 + 0.003, sx1 - 0.003,
                        BACK_Y + 0.0002, BACK_Y + 0.0015,
                        vz_p, vz_p + 0.0015)

            # Retention screw (8-sided cap head, same pattern as ear screw)
            SCR_R_P = 0.0022; SCR_T_P = 0.0018; SCR_Y_P = BACK_Y + 0.0030; SCR_SEG_P = 8
            SCR_CZ_P = HH - 0.006
            fvp = []; bvp = []
            for i in range(SCR_SEG_P):
                a = math.pi / SCR_SEG_P + 2 * math.pi * i / SCR_SEG_P
                fvp.append(bm_pcie_screws.verts.new((scx_p + SCR_R_P * math.cos(a), SCR_Y_P,               SCR_CZ_P + SCR_R_P * math.sin(a))))
                bvp.append(bm_pcie_screws.verts.new((scx_p + SCR_R_P * math.cos(a), SCR_Y_P + SCR_T_P,     SCR_CZ_P + SCR_R_P * math.sin(a))))
            cfp = bm_pcie_screws.verts.new((scx_p, SCR_Y_P,               SCR_CZ_P))
            cbp = bm_pcie_screws.verts.new((scx_p, SCR_Y_P + SCR_T_P,     SCR_CZ_P))
            for i in range(SCR_SEG_P):
                n = (i + 1) % SCR_SEG_P
                _sw_F(bm_pcie_screws, [fvp[i], fvp[n], bvp[n], bvp[i]])
                try: bm_pcie_screws.faces.new([cfp, fvp[n], fvp[i]])
                except: pass
                try: bm_pcie_screws.faces.new([cbp, bvp[i], bvp[n]])
                except: pass

        parts.append(_sw_mesh_obj(f"{name}_pcie_brackets", bm_pcie,        col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_pcie_screws",   bm_pcie_screws, col, 'M_DarkGrayMet'))

        # Rear I/O cluster
        IO_X0 = PCIE_X0 + PCIE_W + 0.006

        # RJ45 port constants (rear-style — same as switch REAR_PORTS geometry)
        RJ_OW = 0.0160; RJ_OH = 0.0130; RJ_WALL = 0.0014
        RJ_IW = RJ_OW - 2 * RJ_WALL; RJ_IH = RJ_OH - 2 * RJ_WALL
        RJ_CHAM = 0.00048
        RJ_PROTRUDE = 0.00150
        RJ_MOUTH_Y = BACK_Y + RJ_PROTRUDE
        RJ_DEEP_Y  = RJ_MOUTH_Y - 0.0160

        bm_io_rj   = bmesh.new()
        bm_io_contacts = bmesh.new()
        bm_io_usb_r = bmesh.new()
        bm_io_misc = bmesh.new()

        def _build_rear_rj45(px_r, pz_r):
            py_mouth = RJ_MOUTH_Y; py_deep = RJ_DEEP_Y
            py_iback = py_deep + RJ_WALL
            om_r = [bm_io_rj.verts.new((px_r - RJ_OW/2, py_mouth, pz_r - RJ_OH/2)),
                    bm_io_rj.verts.new((px_r + RJ_OW/2, py_mouth, pz_r - RJ_OH/2)),
                    bm_io_rj.verts.new((px_r + RJ_OW/2, py_mouth, pz_r + RJ_OH/2)),
                    bm_io_rj.verts.new((px_r - RJ_OW/2, py_mouth, pz_r + RJ_OH/2))]
            im_r = [bm_io_rj.verts.new((px_r - RJ_IW/2 + RJ_CHAM, py_mouth, pz_r - RJ_IH/2 + RJ_CHAM)),
                    bm_io_rj.verts.new((px_r + RJ_IW/2 - RJ_CHAM, py_mouth, pz_r - RJ_IH/2 + RJ_CHAM)),
                    bm_io_rj.verts.new((px_r + RJ_IW/2 - RJ_CHAM, py_mouth, pz_r + RJ_IH/2 - RJ_CHAM)),
                    bm_io_rj.verts.new((px_r - RJ_IW/2 + RJ_CHAM, py_mouth, pz_r + RJ_IH/2 - RJ_CHAM))]
            od_r = [bm_io_rj.verts.new((px_r - RJ_OW/2, py_deep, pz_r - RJ_OH/2)),
                    bm_io_rj.verts.new((px_r + RJ_OW/2, py_deep, pz_r - RJ_OH/2)),
                    bm_io_rj.verts.new((px_r + RJ_OW/2, py_deep, pz_r + RJ_OH/2)),
                    bm_io_rj.verts.new((px_r - RJ_OW/2, py_deep, pz_r + RJ_OH/2))]
            ib_r = [bm_io_rj.verts.new((px_r - RJ_IW/2, py_iback, pz_r - RJ_IH/2)),
                    bm_io_rj.verts.new((px_r + RJ_IW/2, py_iback, pz_r - RJ_IH/2)),
                    bm_io_rj.verts.new((px_r + RJ_IW/2, py_iback, pz_r + RJ_IH/2)),
                    bm_io_rj.verts.new((px_r - RJ_IW/2, py_iback, pz_r + RJ_IH/2))]
            _sw_F(bm_io_rj, [om_r[0], om_r[1], im_r[1], im_r[0]])
            _sw_F(bm_io_rj, [om_r[2], om_r[3], im_r[3], im_r[2]])
            _sw_F(bm_io_rj, [om_r[3], om_r[0], im_r[0], im_r[3]])
            _sw_F(bm_io_rj, [om_r[1], om_r[2], im_r[2], im_r[1]])
            _sw_F(bm_io_rj, [om_r[0], od_r[0], od_r[1], om_r[1]])
            _sw_F(bm_io_rj, [om_r[3], od_r[3], od_r[2], om_r[2]])
            _sw_F(bm_io_rj, [om_r[3], om_r[0], od_r[0], od_r[3]])
            _sw_F(bm_io_rj, [om_r[1], od_r[1], od_r[2], om_r[2]])
            _sw_F(bm_io_rj, [od_r[0], od_r[3], od_r[2], od_r[1]])
            _sw_F(bm_io_rj, [im_r[0], im_r[1], ib_r[1], ib_r[0]])
            _sw_F(bm_io_rj, [im_r[2], im_r[3], ib_r[3], ib_r[2]])
            _sw_F(bm_io_rj, [im_r[3], im_r[0], ib_r[0], ib_r[3]])
            _sw_F(bm_io_rj, [im_r[1], im_r[2], ib_r[2], ib_r[1]])
            _sw_F(bm_io_rj, [ib_r[0], ib_r[1], ib_r[2], ib_r[3]])
            # Gold contact pins
            pin_y0_r = py_iback + 0.0002; pin_y1_r = pin_y0_r + 0.0003
            pin_z0_r = pz_r - RJ_IH / 2 + 0.001
            sp_r = RJ_IW / 9
            for pi_r in range(8):
                ppx_r = (px_r - RJ_IW/2) + (pi_r + 1) * sp_r
                _sw_box(bm_io_contacts, ppx_r - 0.0003, ppx_r + 0.0003,
                        pin_y0_r, pin_y1_r, pin_z0_r, pin_z0_r + 0.0011)

        # iDRAC + 2× LAN RJ45 — all at same height, evenly spaced horizontally
        _build_rear_rj45(IO_X0 + 0.010, HH * 0.40)
        _build_rear_rj45(IO_X0 + 0.030, HH * 0.40)
        _build_rear_rj45(IO_X0 + 0.050, HH * 0.40)

        # Bake contacts faces from raw boxes
        bm_io_contacts.verts.ensure_lookup_table()
        _n_io_contacts = len(bm_io_contacts.verts) // 8
        for i in range(_n_io_contacts):
            b = i * 8
            vs_rc2 = bm_io_contacts.verts[b:b + 8]
            for f_idx in [(0,1,2,3),(4,7,6,5),(0,4,5,1),(3,2,6,7),(0,3,7,4),(1,5,6,2)]:
                try: bm_io_contacts.faces.new([vs_rc2[j] for j in f_idx])
                except: pass

        # 2× rear USB-A
        USB_OW_R = 0.0130; USB_OH_R = 0.0060; USB_IW_R = 0.0100; USB_IH_R = 0.0035; USB_D_R = 0.0100
        USB_WALL_R = (USB_OW_R - USB_IW_R) / 2
        USB_CX_R = IO_X0 + 0.078   # own column, clear of LAN ports
        for ui_r, USB_CZ_R in enumerate([-HH * 0.15, -HH * 0.42]):
            FY_R = BACK_Y + 0.0005   # opening face — slightly proud of rear wall
            BY_R = FY_R - USB_D_R    # tunnel goes INTO chassis (toward -Y)
            # Face frame strips — proud of rear wall at opening
            _sw_box(bm_io_usb_r, USB_CX_R - USB_OW_R/2, USB_CX_R + USB_OW_R/2,
                    FY_R, FY_R + 0.0008,
                    USB_CZ_R + USB_IH_R/2, USB_CZ_R + USB_OH_R/2)
            _sw_box(bm_io_usb_r, USB_CX_R - USB_OW_R/2, USB_CX_R + USB_OW_R/2,
                    FY_R, FY_R + 0.0008,
                    USB_CZ_R - USB_OH_R/2, USB_CZ_R - USB_IH_R/2)
            _sw_box(bm_io_usb_r, USB_CX_R - USB_OW_R/2, USB_CX_R - USB_IW_R/2,
                    FY_R, FY_R + 0.0008,
                    USB_CZ_R - USB_OH_R/2, USB_CZ_R + USB_OH_R/2)
            _sw_box(bm_io_usb_r, USB_CX_R + USB_IW_R/2, USB_CX_R + USB_OW_R/2,
                    FY_R, FY_R + 0.0008,
                    USB_CZ_R - USB_OH_R/2, USB_CZ_R + USB_OH_R/2)
            # Tunnel walls — going inward (BY_R < FY_R)
            for _wall_pair in [
                (USB_CX_R - USB_OW_R/2, USB_CX_R + USB_OW_R/2, USB_CZ_R + USB_IH_R/2, USB_CZ_R + USB_OH_R/2),
                (USB_CX_R - USB_OW_R/2, USB_CX_R + USB_OW_R/2, USB_CZ_R - USB_OH_R/2, USB_CZ_R - USB_IH_R/2),
                (USB_CX_R - USB_OW_R/2, USB_CX_R - USB_IW_R/2, USB_CZ_R - USB_OH_R/2, USB_CZ_R + USB_OH_R/2),
                (USB_CX_R + USB_IW_R/2, USB_CX_R + USB_OW_R/2, USB_CZ_R - USB_OH_R/2, USB_CZ_R + USB_OH_R/2),
            ]:
                _sw_box(bm_io_usb_r, _wall_pair[0], _wall_pair[1], BY_R, FY_R, _wall_pair[2], _wall_pair[3])
            # Back cap at innermost end
            _sw_box(bm_io_usb_r, USB_CX_R - USB_OW_R/2, USB_CX_R + USB_OW_R/2,
                    BY_R, BY_R + 0.0005,
                    USB_CZ_R - USB_OH_R/2, USB_CZ_R + USB_OH_R/2)
            # Plastic tongue (upper half of inner cavity)
            _sw_box(bm_io_usb_r, USB_CX_R - USB_IW_R/2 + 0.001, USB_CX_R + USB_IW_R/2 - 0.001,
                    BY_R + 0.001, FY_R - 0.002,
                    USB_CZ_R, USB_CZ_R + USB_IH_R/2 - 0.0003)

        # VGA DE-15 rect — own column right of USB
        _sw_box(bm_io_misc, IO_X0 + 0.098, IO_X0 + 0.129,
                BACK_Y + 0.001, BACK_Y + 0.004,
                HH * 0.25 - 0.0075, HH * 0.25 + 0.0075)

        # DB9 serial rect — same X column as VGA, lower Z (6mm gap between them)
        _sw_box(bm_io_misc, IO_X0 + 0.098, IO_X0 + 0.116,
                BACK_Y + 0.001, BACK_Y + 0.004,
                -HH * 0.58 - 0.005, -HH * 0.58 + 0.005)

        parts.append(_sw_mesh_obj(f"{name}_rear_rj45_housings",  bm_io_rj,       col, 'M_PlasticDark'))
        parts.append(_sw_mesh_obj(f"{name}_rear_rj45_contacts",  bm_io_contacts, col, 'M_Gold'))
        parts.append(_sw_mesh_obj(f"{name}_usb_rear",            bm_io_usb_r,    col, 'M_PlasticDark'))
        parts.append(_sw_mesh_obj(f"{name}_rear_io_misc",        bm_io_misc,     col, 'M_DarkGrayMet'))

        # Rear exhaust grille (between PSUs and PCIe zone)
        EXGRL_X0 = PSU_X1_R + 0.003
        EXGRL_X1 = PCIE_X0 - 0.003
        if EXGRL_X1 > EXGRL_X0:
            bm_exhaust = bmesh.new()
            N_EXGRL = 14
            for ei in range(N_EXGRL):
                ez_e = BAY_Z0 + ei * (BAY_ZONE_H / N_EXGRL)
                _sw_box(bm_exhaust, EXGRL_X0, EXGRL_X1,
                        BACK_Y, BACK_Y + 0.0012,
                        ez_e, ez_e + BAY_ZONE_H / N_EXGRL - 0.0010)
            parts.append(_sw_mesh_obj(f"{name}_rear_exhaust", bm_exhaust, col, 'M_DarkGrayMet'))

        # ── Rear background panel — covers chassis rear where no faceplate exists ──
        # Tiles around the RJ45 / USB connector openings; gap strips fill zones
        # between PSUs and between PCIe and IO cluster.
        bm_rear_bg = bmesh.new()
        _RBG_Y0 = BACK_Y - 0.002;  _RBG_Y1 = BACK_Y   # 2mm wall thickness

        def _rbg(x0, x1, z0, z1):
            _sw_box(bm_rear_bg, x0, x1, _RBG_Y0, _RBG_Y1, z0, z1)

        # Solid zone backgrounds (proud faceplates sit on top at BACK_Y)
        _rbg(-HW,              PSU_X0_L,       -HH, HH)   # left edge strip
        _rbg(PSU_X1_L,         PSU_X0_R,       -HH, HH)   # between PSUs
        _rbg(PSU_X1_R,         PCIE_X0,        -HH, HH)   # exhaust grille zone
        _rbg(PCIE_X0,          PCIE_X0+PCIE_W, -HH, HH)   # PCIe zone
        _rbg(PCIE_X0 + PCIE_W, IO_X0,          -HH, HH)   # PCIe-to-IO gap

        # PSU bays — background tiled around IEC openings (must leave IEC hole open)
        for _psu_x0, _psu_x1 in [(PSU_X0_L, PSU_X1_L), (PSU_X0_R, PSU_X1_R)]:
            _psu_cx = (_psu_x0 + _psu_x1) / 2
            _bg_iz0 = -HH * 0.35 - IEC_CUT_H_s / 2
            _bg_iz1 = -HH * 0.35 + IEC_CUT_H_s / 2
            _bg_ix0 = _psu_cx - IEC_CUT_W_s / 2
            _bg_ix1 = _psu_cx + IEC_CUT_W_s / 2
            _rbg(_psu_x0, _bg_ix0, -HH, HH)         # left of IEC
            _rbg(_bg_ix1, _psu_x1, -HH, HH)         # right of IEC
            _rbg(_bg_ix0, _bg_ix1, -HH,     _bg_iz0) # below IEC
            _rbg(_bg_ix0, _bg_ix1, _bg_iz1, HH)      # above IEC

        # IO cluster background — tiled around connector openings
        _RJ_Z0 = HH * 0.40 - RJ_OH / 2          # bottom of RJ45 opening
        _RJ_Z1 = HH * 0.40 + RJ_OH / 2          # top of RJ45 opening

        # X column boundaries
        _C_A0  = IO_X0;            _C_A1  = IO_X0 + 0.002   # left margin
        _C_R1a = IO_X0 + 0.002;    _C_R1b = IO_X0 + 0.018   # RJ45 #1
        _C_G1a = IO_X0 + 0.018;    _C_G1b = IO_X0 + 0.022   # gap
        _C_R2a = IO_X0 + 0.022;    _C_R2b = IO_X0 + 0.038   # RJ45 #2
        _C_G2a = IO_X0 + 0.038;    _C_G2b = IO_X0 + 0.042   # gap
        _C_R3a = IO_X0 + 0.042;    _C_R3b = IO_X0 + 0.058   # RJ45 #3
        _C_Ga  = IO_X0 + 0.058;    _C_Gb  = IO_X0 + 0.0715  # gap to USB
        _C_Ua  = IO_X0 + 0.0715;   _C_Ub  = IO_X0 + 0.0845  # USB column
        _C_Ra  = IO_X0 + 0.0845;   _C_Rb  = HW              # rest to right edge

        # Solid columns
        for _cx0, _cx1 in [(_C_A0, _C_A1), (_C_G1a, _C_G1b), (_C_G2a, _C_G2b),
                            (_C_Ga, _C_Gb), (_C_Ra, _C_Rb)]:
            _rbg(_cx0, _cx1, -HH, HH)

        # RJ45 columns — open at RJ45 z band
        for _rx0, _rx1 in [(_C_R1a, _C_R1b), (_C_R2a, _C_R2b), (_C_R3a, _C_R3b)]:
            _rbg(_rx0, _rx1, -HH,    _RJ_Z0)   # below hole
            _rbg(_rx0, _rx1, _RJ_Z1, HH)       # above hole

        # USB column — two holes at different Z positions
        _U1Z0 = -HH * 0.15 - USB_OH_R / 2;   _U1Z1 = -HH * 0.15 + USB_OH_R / 2
        _U2Z0 = -HH * 0.42 - USB_OH_R / 2;   _U2Z1 = -HH * 0.42 + USB_OH_R / 2
        _rbg(_C_Ua, _C_Ub, -HH,   _U2Z0)   # below lower USB
        _rbg(_C_Ua, _C_Ub, _U2Z1, _U1Z0)   # between USB ports
        _rbg(_C_Ua, _C_Ub, _U1Z1, HH)      # above upper USB

        parts.append(_sw_mesh_obj(f"{name}_rear_panel_bg", bm_rear_bg, col, 'M_Aluminum'))

        # ── Translation: centred coords → equipment-origin convention ─────
        # parts[0] is the chassis, already built in equipment-origin space
        # (cy=d/2, cz=h/2) by _create_box_object. All new 1U geometry in
        # parts[1:] was built in centred coords — translate those only.
        tx, ty, tz = 0.0, d / 2, h / 2
        for obj in parts[1:]:
            me = obj.data
            for v in me.vertices:
                v.co.x += tx
                v.co.y += ty
                v.co.z += tz
            me.update()
            obj.hide_render = False
        parts[0].hide_render = False   # chassis

        # ── Mounting ears — built in equipment-origin space ───────────────
        ear_w_1u = (EIA_RAIL_SPAN_M - EIA_EQUIPMENT_BODY_M) / 2
        ear_d_1u = 0.002
        ear_h_dim_1u = h * 0.68
        for side_sign in (-1, 1):
            side_label = 'L' if side_sign < 0 else 'R'
            ear_cx_1u = side_sign * (w / 2 + ear_w_1u / 2)
            bm_ear = bmesh.new()
            _sw_box(bm_ear,
                    ear_cx_1u - ear_w_1u / 2, ear_cx_1u + ear_w_1u / 2,
                    -ear_d_1u, 0.0,
                    (h - ear_h_dim_1u) / 2, (h + ear_h_dim_1u) / 2)
            parts.append(_sw_mesh_obj(f"{name}_ear_{side_label}", bm_ear, col, 'M_Aluminum'))

            # M6 rack screw — 8-sided cap head + Phillips cross
            SCR_R_1u   = 0.0038
            SCR_T_1u   = 0.0028
            SCR_Y_1u   = -(ear_d_1u + 0.0010)
            SCR_Z_1u   = h / 2
            SCR_SEG_1u = 8
            bm_scr_1u  = bmesh.new()
            fv_1u = []; bv_1u = []
            for i in range(SCR_SEG_1u):
                a = math.pi / SCR_SEG_1u + 2 * math.pi * i / SCR_SEG_1u
                fv_1u.append(bm_scr_1u.verts.new((ear_cx_1u + SCR_R_1u * math.cos(a), SCR_Y_1u,               SCR_Z_1u + SCR_R_1u * math.sin(a))))
                bv_1u.append(bm_scr_1u.verts.new((ear_cx_1u + SCR_R_1u * math.cos(a), SCR_Y_1u + SCR_T_1u,    SCR_Z_1u + SCR_R_1u * math.sin(a))))
            cf_1u = bm_scr_1u.verts.new((ear_cx_1u, SCR_Y_1u,               SCR_Z_1u))
            cb_1u = bm_scr_1u.verts.new((ear_cx_1u, SCR_Y_1u + SCR_T_1u,    SCR_Z_1u))
            for i in range(SCR_SEG_1u):
                n = (i + 1) % SCR_SEG_1u
                _sw_F(bm_scr_1u, [fv_1u[i], fv_1u[n], bv_1u[n], bv_1u[i]])
                try: bm_scr_1u.faces.new([cf_1u, fv_1u[n], fv_1u[i]])
                except: pass
                try: bm_scr_1u.faces.new([cb_1u, bv_1u[i], bv_1u[n]])
                except: pass
            # Phillips cross grooves
            GRV_1u = 0.0006; GRL_1u = SCR_R_1u * 1.6
            _sw_box(bm_scr_1u, ear_cx_1u - GRL_1u/2, ear_cx_1u + GRL_1u/2,
                    SCR_Y_1u - 0.0003, SCR_Y_1u, SCR_Z_1u - GRV_1u/2, SCR_Z_1u + GRV_1u/2)
            _sw_box(bm_scr_1u, ear_cx_1u - GRV_1u/2, ear_cx_1u + GRV_1u/2,
                    SCR_Y_1u - 0.0003, SCR_Y_1u, SCR_Z_1u - GRL_1u/2, SCR_Z_1u + GRL_1u/2)
            parts.append(_sw_mesh_obj(f"{name}_ear_screw_{side_label}", bm_scr_1u, col, 'M_DarkGrayMet'))

        # ── Optional join ─────────────────────────────────────────────────
        if join_mesh:
            joined_1u = _join_parts(parts, name)
            bpy.ops.object.select_all(action='DESELECT')
            joined_1u.select_set(True)
            bpy.context.view_layer.objects.active = joined_1u
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.normals_make_consistent(inside=False)
            bpy.ops.object.mode_set(mode='OBJECT')

    elif u_size == 2:
        # ── Hero 2U server front + rear — centred coordinate system ──────
        _sw_ensure_materials()
        HW = w / 2;  HH = h / 2
        FRONT_Y = -(d / 2);  BACK_Y = d / 2

        # Delete chassis rear face
        _bm_r2 = bmesh.new()
        _bm_r2.from_mesh(parts[0].data)
        bmesh.ops.delete(_bm_r2,
            geom=[f for f in _bm_r2.faces if f.calc_center_median().y > d/2*0.97],
            context='FACES_ONLY')
        _bm_r2.to_mesh(parts[0].data); _bm_r2.free(); parts[0].data.update()

        # ── Front layout ──────────────────────────────────────────────────
        L_M2 = 0.010; R_M2 = 0.008
        CTRL_W2 = 0.055; VENT_W2 = 0.082; ZG2 = 0.004
        CTRL_X0_2 = -HW + L_M2;  CTRL_X1_2 = CTRL_X0_2 + CTRL_W2
        CTRL_CX_2 = (CTRL_X0_2 + CTRL_X1_2) / 2
        VENT_X1_2 = HW - R_M2;   VENT_X0_2 = VENT_X1_2 - VENT_W2
        BAY_X0_2  = CTRL_X1_2 + ZG2;  BAY_X1_2 = VENT_X0_2 - ZG2
        BAY_ZW_2  = BAY_X1_2 - BAY_X0_2
        BAY_H_2   = h * 0.84;  BAY_Z0_2 = -BAY_H_2 / 2;  BAY_Z1_2 = BAY_H_2 / 2
        BAY_RD_2  = 0.012

        # Carrier grid (4 cols × 3 rows)
        NCOLS_2 = 4;  NROWS_2 = 3;  GX_2 = 0.0015;  GZ_2 = 0.0018
        cw_2 = (BAY_ZW_2 - GX_2*(NCOLS_2-1)) / NCOLS_2
        ch_2 = (BAY_H_2  - GZ_2*(NROWS_2-1)) / NROWS_2

        # ── IEC C14 constants ─────────────────────────────────────────────
        IEC_CUT_W_2 = 0.0280; IEC_CUT_H_2 = 0.0220
        IEC_FLG_W_2 = 0.0390; IEC_FLG_H_2 = 0.0310
        IEC_SOCK_D_2 = 0.0200; IEC_FLG_T_2 = 0.0025; S_WALL_2 = 0.002

        # ── Rear layout ───────────────────────────────────────────────────
        _io2_w = 0.110; _pcie2_w = 0.050; _fan2_w = 0.090
        _psu2_ea = 0.085; _psu2_gap = 0.006; _rg2 = 0.005
        _io2_x0   = -HW
        _pcie2_x0 = _io2_x0   + _io2_w    + _rg2
        _fan2_x0  = _pcie2_x0 + _pcie2_w  + _rg2
        _psu2_x0L = _fan2_x0  + _fan2_w   + _rg2
        _psu2_x1L = _psu2_x0L + _psu2_ea
        _psu2_x0R = _psu2_x1L + _psu2_gap
        _psu2_x1R = _psu2_x0R + _psu2_ea
        _IEC2_CZ  = -HH * 0.35

        # ── Front plate ───────────────────────────────────────────────────
        parts.append(_sw_holey_plate(
            f"{name}_front_plate", FRONT_Y,
            [(BAY_X0_2,       BAY_X1_2,       BAY_Z0_2,       BAY_Z1_2),
             (VENT_X0_2+0.001,VENT_X1_2-0.001,BAY_Z0_2+0.001, BAY_Z1_2-0.001),
             (CTRL_X0_2+0.001,CTRL_X1_2-0.001,BAY_Z0_2+0.001, BAY_Z1_2-0.001)],
            [], col, 'M_DarkGrayMet',
            x_min=-HW, x_max=HW, z_min=-HH, z_max=HH,
            outward_plus_y=False))

        # Top louver strip
        bm_louv2 = bmesh.new()
        for _li2 in range(5):
            _zt2 = HH - 0.0003 - _li2 * 0.0018
            _sw_box(bm_louv2, -HW+0.005, HW-0.005,
                    FRONT_Y+h*0.3, FRONT_Y+h*0.8, _zt2-0.0004, _zt2+0.00005)
        parts.append(_sw_mesh_obj(f"{name}_top_louvers", bm_louv2, col, 'M_DarkGrayMet'))

        # Service tag
        bm_tag2 = bmesh.new()
        _TX2 = -HW + 0.0065
        _sw_box(bm_tag2, _TX2-0.0055, _TX2+0.0055, FRONT_Y-0.0008, FRONT_Y, -h*0.26, h*0.26)
        _sw_box(bm_tag2, _TX2-0.003,  _TX2+0.003,  FRONT_Y-0.004,  FRONT_Y, -h*0.26-0.004, -h*0.26)
        parts.append(_sw_mesh_obj(f"{name}_svc_tag", bm_tag2, col, 'M_PlasticDark'))

        # Bay background (recessed)
        bm_bybg2 = bmesh.new()
        _sw_box(bm_bybg2, BAY_X0_2, BAY_X1_2, FRONT_Y+0.002, FRONT_Y+BAY_RD_2, BAY_Z0_2, BAY_Z1_2)
        parts.append(_sw_mesh_obj(f"{name}_bay_bg", bm_bybg2, col, 'M_PlasticDark'))

        # ── 4×3 carrier grid ─────────────────────────────────────────────
        bm_cf2 = bmesh.new(); bm_cv2 = bmesh.new()
        bm_ch2 = bmesh.new(); bm_cl2 = bmesh.new(); bm_clw2 = bmesh.new()
        _lbl2_objs = []
        LBL_Y2 = FRONT_Y - 0.0020

        def _add_lbl2(txt, lx, lz):
            _fc2 = bpy.data.curves.new("_s2lbl", type='FONT')
            _fc2.body = txt; _fc2.size = 0.0030; _fc2.extrude = 0.00030
            _fc2.align_x = 'CENTER'; _fc2.align_y = 'CENTER'
            _o2 = bpy.data.objects.new("_s2lbl", _fc2)
            bpy.context.scene.collection.objects.link(_o2)
            _o2.rotation_euler = (math.pi/2, 0, 0)
            _o2.location = (lx, LBL_Y2, lz)
            _lbl2_objs.append(_o2)

        for _r2 in range(NROWS_2):
            for _c2 in range(NCOLS_2):
                _idx2  = _r2*NCOLS_2 + _c2
                _cx2   = BAY_X0_2 + (_c2+0.5)*cw_2 + _c2*GX_2
                _cz2   = BAY_Z0_2 + (_r2+0.5)*ch_2 + _r2*GZ_2
                _CY2_0 = FRONT_Y + 0.0002;  _CY2_1 = FRONT_Y - 0.0018
                _sw_box(bm_cf2, _cx2-cw_2/2+0.001, _cx2+cw_2/2-0.001,
                        _CY2_1, _CY2_0, _cz2-ch_2/2+0.001, _cz2+ch_2/2-0.001)
                _VW2 = cw_2*0.58; _vx2_0 = _cx2-_VW2/2+cw_2*0.08; _vx2_1 = _cx2+_VW2/2+cw_2*0.08
                for _vi2 in range(3):
                    _vz2 = _cz2 + (_vi2-1)*0.0032
                    _sw_box(bm_cv2, _vx2_0, _vx2_1, _CY2_1-0.0003, _CY2_1,
                            _vz2-0.00035, _vz2+0.00035)
                if qf["detailed_handles"]:
                    _HX2 = _cx2 - cw_2/2 + 0.0045
                    _sw_box(bm_ch2, _HX2-0.00275, _HX2+0.00275,
                            FRONT_Y-0.0038, FRONT_Y-0.0002, _cz2-ch_2*0.36, _cz2+ch_2*0.36)
                    _sw_box(bm_ch2, _HX2-0.00275, _HX2-0.00275+cw_2*0.22,
                            FRONT_Y-0.0038, FRONT_Y-0.0002, _cz2-ch_2*0.36, _cz2-ch_2*0.36+0.0042)
                if qf["led_emissive"]:
                    _LX2 = _cx2+cw_2/2-0.006
                    _LRZ2 = _cz2+ch_2/2-0.0035;  _LWZ2 = _LRZ2-0.0035
                    _sw_box(bm_cl2, _LX2-0.0012, _LX2+0.0012,
                            _CY2_1-0.0008, _CY2_1, _LRZ2-0.0012, _LRZ2+0.0012)
                    _sw_box(bm_clw2, _LX2-0.0012, _LX2+0.0012,
                            _CY2_1-0.0008, _CY2_1, _LWZ2-0.0012, _LWZ2+0.0012)
                if qf["bezel"]:
                    _add_lbl2(str(_idx2+1), _cx2, _cz2+ch_2/2-0.0025)

        parts.append(_sw_mesh_obj(f"{name}_carrier_faces",      bm_cf2,  col, 'M_PlasticDark'))
        parts.append(_sw_mesh_obj(f"{name}_carrier_vents",      bm_cv2,  col, 'M_Black'))
        parts.append(_sw_mesh_obj(f"{name}_carrier_handles",    bm_ch2,  col, 'M_Black'))
        parts.append(_sw_mesh_obj(f"{name}_carrier_leds",       bm_cl2,  col, 'M_LED_Green'))
        parts.append(_sw_mesh_obj(f"{name}_carrier_leds_write", bm_clw2, col, 'M_LED_Amber'))

        if qf["bezel"] and _lbl2_objs:
            bpy.context.view_layer.update()
            _dep2 = bpy.context.evaluated_depsgraph_get()
            bm_lbl2 = bmesh.new()
            for _fo2 in _lbl2_objs:
                _me_t2 = bpy.data.meshes.new_from_object(_fo2.evaluated_get(_dep2))
                _bm_t2 = bmesh.new(); _bm_t2.from_mesh(_me_t2)
                bmesh.ops.transform(_bm_t2, matrix=_fo2.matrix_world, verts=_bm_t2.verts[:])
                _nv2 = [bm_lbl2.verts.new(v.co) for v in _bm_t2.verts]
                bm_lbl2.verts.ensure_lookup_table(); _bm_t2.verts.ensure_lookup_table()
                _bm_t2.faces.ensure_lookup_table()
                for _ft2 in _bm_t2.faces:
                    try: bm_lbl2.faces.new([_nv2[v.index] for v in _ft2.verts])
                    except: pass
                _bm_t2.free(); bpy.data.meshes.remove(_me_t2)
                _fc2_d = _fo2.data; bpy.data.objects.remove(_fo2); bpy.data.curves.remove(_fc2_d)
            parts.append(_sw_mesh_obj(f"{name}_bay_labels", bm_lbl2, col, 'M_White'))

        # ── Honeycomb vent panel (right side) ─────────────────────────────
        bm_vent2 = bmesh.new()
        VX0_2 = VENT_X0_2+0.002; VX1_2 = VENT_X1_2-0.002
        _sw_box(bm_vent2, VX0_2, VX1_2, FRONT_Y+0.001, FRONT_Y+0.004, BAY_Z0_2, BAY_Z1_2)
        _VNT_NC = 5; _VNT_NR = 11; _VNT_BH = 0.0014
        _vcw2 = (VX1_2-VX0_2) / _VNT_NC
        _vrs2 = (BAY_H_2-0.004) / _VNT_NR
        for _vc2 in range(_VNT_NC):
            _vx0c2 = VX0_2+_vc2*_vcw2+0.0005; _vx1c2 = _vx0c2+_vcw2-0.001
            for _vr2 in range(_VNT_NR):
                _vzb2 = BAY_Z0_2+0.002+_vr2*_vrs2
                _sw_box(bm_vent2, _vx0c2, _vx1c2,
                        FRONT_Y-0.0005, FRONT_Y+0.0005, _vzb2, _vzb2+_VNT_BH)
        parts.append(_sw_mesh_obj(f"{name}_vent_panel", bm_vent2, col, 'M_DarkGrayMet'))

        # ── Left control panel (5-strip USB tiling) ───────────────────────
        bm_ctrl2 = bmesh.new()
        _P1_2CZ = -HH*0.05; _P1_2Z0 = _P1_2CZ-0.003; _P1_2Z1 = _P1_2CZ+0.003
        _P2_2CZ = -HH*0.35; _P2_2Z0 = _P2_2CZ-0.003; _P2_2Z1 = _P2_2CZ+0.003
        _USB2_OW = 0.0130; _USB2_OH = 0.0060
        _USB2_CX = CTRL_CX_2 + 0.004
        _USB2_X0 = _USB2_CX-_USB2_OW/2; _USB2_X1 = _USB2_CX+_USB2_OW/2
        _CB2_X0 = CTRL_X0_2+0.001; _CB2_X1 = CTRL_X1_2-0.001
        _CB2_Y0 = FRONT_Y-0.0005;  _CB2_Y1 = FRONT_Y+0.0020
        _CB2_Z0 = BAY_Z0_2+0.001;  _CB2_Z1 = BAY_Z1_2-0.001
        _sw_box(bm_ctrl2, _CB2_X0,  _USB2_X0, _CB2_Y0, _CB2_Y1, _CB2_Z0, _CB2_Z1)
        _sw_box(bm_ctrl2, _USB2_X1, _CB2_X1,  _CB2_Y0, _CB2_Y1, _CB2_Z0, _CB2_Z1)
        _sw_box(bm_ctrl2, _USB2_X0, _USB2_X1, _CB2_Y0, _CB2_Y1, _CB2_Z0, _P2_2Z0)
        _sw_box(bm_ctrl2, _USB2_X0, _USB2_X1, _CB2_Y0, _CB2_Y1, _P2_2Z1, _P1_2Z0)
        _sw_box(bm_ctrl2, _USB2_X0, _USB2_X1, _CB2_Y0, _CB2_Y1, _P1_2Z1, _CB2_Z1)
        parts.append(_sw_mesh_obj(f"{name}_ctrl_bg", bm_ctrl2, col, 'M_DarkGrayMet'))

        # Power button (8-sided cap head)
        PWR2_CX = CTRL_CX_2+0.012; PWR2_CZ = HH*0.52
        PWR2_R = 0.0038; PWR2_T = 0.0030; PWR2_SEG = 8; PWR2_Y = FRONT_Y-0.0030
        bm_pwr2 = bmesh.new(); fv2p = []; bv2p = []
        for _i2p in range(PWR2_SEG):
            _a2p = math.pi/PWR2_SEG + 2*math.pi*_i2p/PWR2_SEG
            fv2p.append(bm_pwr2.verts.new((PWR2_CX+PWR2_R*math.cos(_a2p), PWR2_Y,         PWR2_CZ+PWR2_R*math.sin(_a2p))))
            bv2p.append(bm_pwr2.verts.new((PWR2_CX+PWR2_R*math.cos(_a2p), PWR2_Y+PWR2_T,  PWR2_CZ+PWR2_R*math.sin(_a2p))))
        cf2p = bm_pwr2.verts.new((PWR2_CX, PWR2_Y,         PWR2_CZ))
        cb2p = bm_pwr2.verts.new((PWR2_CX, PWR2_Y+PWR2_T,  PWR2_CZ))
        for _i2p in range(PWR2_SEG):
            _n2p = (_i2p+1)%PWR2_SEG
            _sw_F(bm_pwr2, [fv2p[_i2p], fv2p[_n2p], bv2p[_n2p], bv2p[_i2p]])
            try: bm_pwr2.faces.new([cf2p, fv2p[_n2p], fv2p[_i2p]])
            except: pass
            try: bm_pwr2.faces.new([cb2p, bv2p[_i2p], bv2p[_n2p]])
            except: pass
        parts.append(_sw_mesh_obj(f"{name}_pwr_btn", bm_pwr2, col, 'M_Black'))

        if qf["led_emissive"]:
            PWR2_RING_OR = PWR2_R+0.0018; PWR2_RING_IR = PWR2_R+0.0004; PWR2_RING_D = 0.0005
            bm_pwr2_led = bmesh.new()
            fr2_o=[]; fr2_i=[]; bk2_o=[]; bk2_i=[]
            for _i2r in range(16):
                _a2r = 2*math.pi*_i2r/16; _co2r = math.cos(_a2r); _si2r = math.sin(_a2r)
                fr2_o.append(bm_pwr2_led.verts.new((PWR2_CX+PWR2_RING_OR*_co2r, PWR2_Y,              PWR2_CZ+PWR2_RING_OR*_si2r)))
                fr2_i.append(bm_pwr2_led.verts.new((PWR2_CX+PWR2_RING_IR*_co2r, PWR2_Y,              PWR2_CZ+PWR2_RING_IR*_si2r)))
                bk2_o.append(bm_pwr2_led.verts.new((PWR2_CX+PWR2_RING_OR*_co2r, PWR2_Y+PWR2_RING_D,  PWR2_CZ+PWR2_RING_OR*_si2r)))
                bk2_i.append(bm_pwr2_led.verts.new((PWR2_CX+PWR2_RING_IR*_co2r, PWR2_Y+PWR2_RING_D,  PWR2_CZ+PWR2_RING_IR*_si2r)))
            for _i2r in range(16):
                _n2r = (_i2r+1)%16
                _sw_F(bm_pwr2_led, [fr2_o[_i2r], fr2_i[_i2r], fr2_i[_n2r], fr2_o[_n2r]])
                _sw_F(bm_pwr2_led, [bk2_o[_i2r], bk2_o[_n2r], bk2_i[_n2r], bk2_i[_i2r]])
                _sw_F(bm_pwr2_led, [fr2_o[_i2r], fr2_o[_n2r], bk2_o[_n2r], bk2_o[_i2r]])
                _sw_F(bm_pwr2_led, [fr2_i[_i2r], bk2_i[_i2r], bk2_i[_n2r], fr2_i[_n2r]])
            parts.append(_sw_mesh_obj(f"{name}_pwr_led", bm_pwr2_led, col, 'M_LED_Green'))

        # UID button + LED ring
        bm_uid2 = bmesh.new()
        UID2_CX = CTRL_CX_2+0.012; UID2_CZ = HH*0.18
        _sw_box(bm_uid2, UID2_CX-0.0025, UID2_CX+0.0025,
                FRONT_Y-0.0028, FRONT_Y-0.0005, UID2_CZ-0.0025, UID2_CZ+0.0025)
        parts.append(_sw_mesh_obj(f"{name}_uid_btn", bm_uid2, col, 'M_DarkGrayMet'))
        if qf["led_emissive"]:
            bm_uid2_led = bmesh.new()
            UID2_OR=0.0040; UID2_IR=0.0025; UID2_D=0.0004
            _sw_box(bm_uid2_led, UID2_CX-UID2_OR, UID2_CX+UID2_OR, FRONT_Y-0.0028, FRONT_Y-0.0028+UID2_D, UID2_CZ+UID2_IR, UID2_CZ+UID2_OR)
            _sw_box(bm_uid2_led, UID2_CX-UID2_OR, UID2_CX+UID2_OR, FRONT_Y-0.0028, FRONT_Y-0.0028+UID2_D, UID2_CZ-UID2_OR, UID2_CZ-UID2_IR)
            _sw_box(bm_uid2_led, UID2_CX-UID2_OR, UID2_CX-UID2_IR, FRONT_Y-0.0028, FRONT_Y-0.0028+UID2_D, UID2_CZ-UID2_OR, UID2_CZ+UID2_OR)
            _sw_box(bm_uid2_led, UID2_CX+UID2_IR, UID2_CX+UID2_OR, FRONT_Y-0.0028, FRONT_Y-0.0028+UID2_D, UID2_CZ-UID2_OR, UID2_CZ+UID2_OR)
            parts.append(_sw_mesh_obj(f"{name}_uid_led", bm_uid2_led, col, 'M_LED_Blue'))

        # Status LEDs (3 stacked)
        _sled2_defs = [(0.38,'M_LED_Green'),(0.12,'M_LED_Amber'),(-0.14,'M_LED_Green')]
        _sled2_bms: dict = {}
        SLED2_CX = CTRL_X0_2 + 0.010
        for _lzf2, _mat2 in _sled2_defs:
            _lz2 = HH*_lzf2
            if _mat2 not in _sled2_bms: _sled2_bms[_mat2] = bmesh.new()
            _sw_box(_sled2_bms[_mat2], SLED2_CX-0.0015, SLED2_CX+0.0015,
                    FRONT_Y-0.0025, FRONT_Y-0.0005, _lz2-0.0015, _lz2+0.0015)
        for _mat2, _bms2 in _sled2_bms.items():
            _sfx2 = _mat2.replace('M_LED_','').lower()
            parts.append(_sw_mesh_obj(f"{name}_sled_{_sfx2}", _bms2, col, _mat2))

        # Front USB-A ×2 (annular frame + tunnel + tongue)
        bm_usb2 = bmesh.new()
        USB2_OW=0.0130; USB2_OH=0.0060; USB2_IW=0.0100; USB2_IH=0.0035; USB2_D=0.0100
        for _ui2, _USBZ2 in enumerate([_P1_2CZ, _P2_2CZ]):
            _FY2 = FRONT_Y-0.0005; _BY2 = _FY2+USB2_D; _UX2 = _USB2_CX
            _sw_box(bm_usb2, _UX2-USB2_OW/2, _UX2+USB2_OW/2, _FY2-0.0008, _FY2, _USBZ2+USB2_IH/2, _USBZ2+USB2_OH/2)
            _sw_box(bm_usb2, _UX2-USB2_OW/2, _UX2+USB2_OW/2, _FY2-0.0008, _FY2, _USBZ2-USB2_OH/2, _USBZ2-USB2_IH/2)
            _sw_box(bm_usb2, _UX2-USB2_OW/2, _UX2-USB2_IW/2, _FY2-0.0008, _FY2, _USBZ2-USB2_OH/2, _USBZ2+USB2_OH/2)
            _sw_box(bm_usb2, _UX2+USB2_IW/2, _UX2+USB2_OW/2, _FY2-0.0008, _FY2, _USBZ2-USB2_OH/2, _USBZ2+USB2_OH/2)
            for _wp2 in [(_UX2-USB2_OW/2,_UX2+USB2_OW/2,_USBZ2+USB2_IH/2,_USBZ2+USB2_OH/2),
                         (_UX2-USB2_OW/2,_UX2+USB2_OW/2,_USBZ2-USB2_OH/2,_USBZ2-USB2_IH/2),
                         (_UX2-USB2_OW/2,_UX2-USB2_IW/2,_USBZ2-USB2_OH/2,_USBZ2+USB2_OH/2),
                         (_UX2+USB2_IW/2,_UX2+USB2_OW/2,_USBZ2-USB2_OH/2,_USBZ2+USB2_OH/2)]:
                _sw_box(bm_usb2, _wp2[0], _wp2[1], _BY2, _FY2, _wp2[2], _wp2[3])
            _sw_box(bm_usb2, _UX2-USB2_OW/2, _UX2+USB2_OW/2, _BY2-0.0005, _BY2, _USBZ2-USB2_OH/2, _USBZ2+USB2_OH/2)
            _sw_box(bm_usb2, _UX2-USB2_IW/2+0.001, _UX2+USB2_IW/2-0.001, _FY2+0.002, _BY2-0.001, _USBZ2, _USBZ2+USB2_IH/2-0.0003)
        parts.append(_sw_mesh_obj(f"{name}_usb_front", bm_usb2, col, 'M_PlasticDark'))

        # VGA port
        if qf["bezel"]:
            bm_vga2 = bmesh.new()
            _sw_box(bm_vga2, CTRL_CX_2-0.009, CTRL_CX_2+0.009,
                    FRONT_Y-0.004, FRONT_Y, -HH*0.65-0.006, -HH*0.65+0.006)
            parts.append(_sw_mesh_obj(f"{name}_vga_front", bm_vga2, col, 'M_DarkGrayMet'))

        # ── Rear: IEC C14 helper ─────────────────────────────────────────
        bm_iec2_all=bmesh.new(); bm_flg2_all=bmesh.new()
        bm_iec2_scr=bmesh.new(); bm_iec2_con=bmesh.new()

        def _build_iec_at_2u(psu_cx_iec, psu_cz_iec):
            CX_iec=psu_cx_iec; CZ_iec=psu_cz_iec
            ox0=CX_iec-IEC_FLG_W_2/2; ox1=CX_iec+IEC_FLG_W_2/2
            oz0=CZ_iec-IEC_FLG_H_2/2; oz1=CZ_iec+IEC_FLG_H_2/2
            cx0=CX_iec-IEC_CUT_W_2/2; cx1=CX_iec+IEC_CUT_W_2/2
            cz0=CZ_iec-IEC_CUT_H_2/2; cz1=CZ_iec+IEC_CUT_H_2/2
            ix0=cx0+S_WALL_2; ix1=cx1-S_WALL_2; iz0=cz0+S_WALL_2; iz1=cz1-S_WALL_2
            FLG_Y0=BACK_Y; FLG_Y1=BACK_Y+IEC_FLG_T_2; SOCK_Y1=BACK_Y-IEC_SOCK_D_2
            of_v=[bm_iec2_all.verts.new((ox0,FLG_Y0,oz0)),bm_iec2_all.verts.new((ox1,FLG_Y0,oz0)),
                  bm_iec2_all.verts.new((ox1,FLG_Y0,oz1)),bm_iec2_all.verts.new((ox0,FLG_Y0,oz1))]
            ob_v=[bm_iec2_all.verts.new((ox0,SOCK_Y1,oz0)),bm_iec2_all.verts.new((ox1,SOCK_Y1,oz0)),
                  bm_iec2_all.verts.new((ox1,SOCK_Y1,oz1)),bm_iec2_all.verts.new((ox0,SOCK_Y1,oz1))]
            cf_v=[bm_iec2_all.verts.new((cx0,FLG_Y0,cz0)),bm_iec2_all.verts.new((cx1,FLG_Y0,cz0)),
                  bm_iec2_all.verts.new((cx1,FLG_Y0,cz1)),bm_iec2_all.verts.new((cx0,FLG_Y0,cz1))]
            it_v=[bm_iec2_all.verts.new((ix0,FLG_Y0,iz0)),bm_iec2_all.verts.new((ix1,FLG_Y0,iz0)),
                  bm_iec2_all.verts.new((ix1,FLG_Y0,iz1)),bm_iec2_all.verts.new((ix0,FLG_Y0,iz1))]
            ib_v=[bm_iec2_all.verts.new((ix0,SOCK_Y1,iz0)),bm_iec2_all.verts.new((ix1,SOCK_Y1,iz0)),
                  bm_iec2_all.verts.new((ix1,SOCK_Y1,iz1)),bm_iec2_all.verts.new((ix0,SOCK_Y1,iz1))]
            _sw_F(bm_iec2_all,[of_v[0],of_v[1],cf_v[1],cf_v[0]]); _sw_F(bm_iec2_all,[of_v[3],cf_v[3],cf_v[2],of_v[2]])
            _sw_F(bm_iec2_all,[of_v[0],cf_v[0],cf_v[3],of_v[3]]); _sw_F(bm_iec2_all,[of_v[1],of_v[2],cf_v[2],cf_v[1]])
            _sw_F(bm_iec2_all,[of_v[0],ob_v[0],ob_v[1],of_v[1]]); _sw_F(bm_iec2_all,[of_v[3],of_v[2],ob_v[2],ob_v[3]])
            _sw_F(bm_iec2_all,[of_v[0],of_v[3],ob_v[3],ob_v[0]]); _sw_F(bm_iec2_all,[of_v[1],ob_v[1],ob_v[2],of_v[2]])
            _sw_F(bm_iec2_all,[ob_v[0],ob_v[3],ob_v[2],ob_v[1]])
            _sw_F(bm_iec2_all,[cf_v[0],cf_v[1],it_v[1],it_v[0]]); _sw_F(bm_iec2_all,[cf_v[3],it_v[3],it_v[2],cf_v[2]])
            _sw_F(bm_iec2_all,[cf_v[0],it_v[0],it_v[3],cf_v[3]]); _sw_F(bm_iec2_all,[cf_v[1],cf_v[2],it_v[2],it_v[1]])
            _sw_F(bm_iec2_all,[it_v[0],it_v[1],ib_v[1],ib_v[0]]); _sw_F(bm_iec2_all,[it_v[3],ib_v[3],ib_v[2],it_v[2]])
            _sw_F(bm_iec2_all,[it_v[0],ib_v[0],ib_v[3],it_v[3]]); _sw_F(bm_iec2_all,[it_v[1],it_v[2],ib_v[2],ib_v[1]])
            _sw_F(bm_iec2_all,[ib_v[0],ib_v[1],ib_v[2],ib_v[3]])
            f0_v=[bm_flg2_all.verts.new((ox0,FLG_Y0,oz0)),bm_flg2_all.verts.new((ox1,FLG_Y0,oz0)),
                  bm_flg2_all.verts.new((ox1,FLG_Y0,oz1)),bm_flg2_all.verts.new((ox0,FLG_Y0,oz1))]
            f1_v=[bm_flg2_all.verts.new((ox0,FLG_Y1,oz0)),bm_flg2_all.verts.new((ox1,FLG_Y1,oz0)),
                  bm_flg2_all.verts.new((ox1,FLG_Y1,oz1)),bm_flg2_all.verts.new((ox0,FLG_Y1,oz1))]
            c0_v=[bm_flg2_all.verts.new((cx0,FLG_Y0,cz0)),bm_flg2_all.verts.new((cx1,FLG_Y0,cz0)),
                  bm_flg2_all.verts.new((cx1,FLG_Y0,cz1)),bm_flg2_all.verts.new((cx0,FLG_Y0,cz1))]
            c1_v=[bm_flg2_all.verts.new((cx0,FLG_Y1,cz0)),bm_flg2_all.verts.new((cx1,FLG_Y1,cz0)),
                  bm_flg2_all.verts.new((cx1,FLG_Y1,cz1)),bm_flg2_all.verts.new((cx0,FLG_Y1,cz1))]
            _sw_F(bm_flg2_all,[f1_v[0],f1_v[1],c1_v[1],c1_v[0]]); _sw_F(bm_flg2_all,[f1_v[3],c1_v[3],c1_v[2],f1_v[2]])
            _sw_F(bm_flg2_all,[f1_v[0],c1_v[0],c1_v[3],f1_v[3]]); _sw_F(bm_flg2_all,[f1_v[1],f1_v[2],c1_v[2],c1_v[1]])
            _sw_F(bm_flg2_all,[f0_v[0],c0_v[0],c0_v[1],f0_v[1]]); _sw_F(bm_flg2_all,[f0_v[3],f0_v[2],c0_v[2],c0_v[3]])
            _sw_F(bm_flg2_all,[f0_v[0],f0_v[3],c0_v[3],c0_v[0]]); _sw_F(bm_flg2_all,[f0_v[1],c0_v[1],c0_v[2],f0_v[2]])
            for _i2f in range(4):
                _sw_F(bm_flg2_all,[f0_v[_i2f],f1_v[_i2f],f1_v[(_i2f+1)%4],f0_v[(_i2f+1)%4]])
            SR2=0.002; ST2=0.001; NS2=12
            for scx2 in [CX_iec-(IEC_CUT_W_2/2+(IEC_FLG_W_2/2-IEC_CUT_W_2/2)/2),
                         CX_iec+(IEC_CUT_W_2/2+(IEC_FLG_W_2/2-IEC_CUT_W_2/2)/2)]:
                _rb_v2=[]; _rf_v2=[]
                for _si2 in range(NS2):
                    _a2s=2*math.pi*_si2/NS2
                    _rb_v2.append(bm_iec2_scr.verts.new((scx2+SR2*math.cos(_a2s),FLG_Y1,       CZ_iec+SR2*math.sin(_a2s))))
                    _rf_v2.append(bm_iec2_scr.verts.new((scx2+SR2*math.cos(_a2s),FLG_Y1+ST2,   CZ_iec+SR2*math.sin(_a2s))))
                _cf2s=bm_iec2_scr.verts.new((scx2,FLG_Y1+ST2,CZ_iec))
                for _si2 in range(NS2):
                    _sw_F(bm_iec2_scr,[_rb_v2[_si2],_rf_v2[_si2],_rf_v2[(_si2+1)%NS2],_rb_v2[(_si2+1)%NS2]])
                    try: bm_iec2_scr.faces.new([_cf2s,_rf_v2[_si2],_rf_v2[(_si2+1)%NS2]])
                    except: pass
            PY0_2=SOCK_Y1+0.0005; PY1_2=PY0_2+0.001
            def _b2c(cx_b,cz_b,bw,bh):
                _sw_box(bm_iec2_con,cx_b-bw/2,cx_b+bw/2,PY0_2,PY1_2,cz_b-bh/2,cz_b+bh/2)
            _b2c(CX_iec,CZ_iec+0.0055,0.007,0.005)
            _b2c(CX_iec+0.0075,CZ_iec-0.0045,0.0038,0.009)
            _b2c(CX_iec-0.0075,CZ_iec-0.0045,0.0038,0.009)

        # ── Rear: RJ45 helper ────────────────────────────────────────────
        bm_io2_rj=bmesh.new(); bm_io2_con=bmesh.new()
        RJ2_OW=0.0160; RJ2_OH=0.0130; RJ2_WALL=0.0014
        RJ2_IW=RJ2_OW-2*RJ2_WALL; RJ2_IH=RJ2_OH-2*RJ2_WALL
        RJ2_CHAM=0.00048; RJ2_PROT=0.00150; RJ2_DPT=0.0160

        def _build_rear_rj45_2u(px_r,pz_r):
            py_m=BACK_Y+RJ2_PROT; py_d=py_m-RJ2_DPT; py_ib=py_d+RJ2_WALL
            om_r=[bm_io2_rj.verts.new((px_r-RJ2_OW/2,py_m,pz_r-RJ2_OH/2)),
                  bm_io2_rj.verts.new((px_r+RJ2_OW/2,py_m,pz_r-RJ2_OH/2)),
                  bm_io2_rj.verts.new((px_r+RJ2_OW/2,py_m,pz_r+RJ2_OH/2)),
                  bm_io2_rj.verts.new((px_r-RJ2_OW/2,py_m,pz_r+RJ2_OH/2))]
            im_r=[bm_io2_rj.verts.new((px_r-RJ2_IW/2+RJ2_CHAM,py_m,pz_r-RJ2_IH/2+RJ2_CHAM)),
                  bm_io2_rj.verts.new((px_r+RJ2_IW/2-RJ2_CHAM,py_m,pz_r-RJ2_IH/2+RJ2_CHAM)),
                  bm_io2_rj.verts.new((px_r+RJ2_IW/2-RJ2_CHAM,py_m,pz_r+RJ2_IH/2-RJ2_CHAM)),
                  bm_io2_rj.verts.new((px_r-RJ2_IW/2+RJ2_CHAM,py_m,pz_r+RJ2_IH/2-RJ2_CHAM))]
            od_r=[bm_io2_rj.verts.new((px_r-RJ2_OW/2,py_d,pz_r-RJ2_OH/2)),
                  bm_io2_rj.verts.new((px_r+RJ2_OW/2,py_d,pz_r-RJ2_OH/2)),
                  bm_io2_rj.verts.new((px_r+RJ2_OW/2,py_d,pz_r+RJ2_OH/2)),
                  bm_io2_rj.verts.new((px_r-RJ2_OW/2,py_d,pz_r+RJ2_OH/2))]
            ib_r=[bm_io2_rj.verts.new((px_r-RJ2_IW/2,py_ib,pz_r-RJ2_IH/2)),
                  bm_io2_rj.verts.new((px_r+RJ2_IW/2,py_ib,pz_r-RJ2_IH/2)),
                  bm_io2_rj.verts.new((px_r+RJ2_IW/2,py_ib,pz_r+RJ2_IH/2)),
                  bm_io2_rj.verts.new((px_r-RJ2_IW/2,py_ib,pz_r+RJ2_IH/2))]
            _sw_F(bm_io2_rj,[om_r[0],om_r[1],im_r[1],im_r[0]]); _sw_F(bm_io2_rj,[om_r[2],om_r[3],im_r[3],im_r[2]])
            _sw_F(bm_io2_rj,[om_r[3],om_r[0],im_r[0],im_r[3]]); _sw_F(bm_io2_rj,[om_r[1],om_r[2],im_r[2],im_r[1]])
            _sw_F(bm_io2_rj,[om_r[0],od_r[0],od_r[1],om_r[1]]); _sw_F(bm_io2_rj,[om_r[3],od_r[3],od_r[2],om_r[2]])
            _sw_F(bm_io2_rj,[om_r[3],om_r[0],od_r[0],od_r[3]]); _sw_F(bm_io2_rj,[om_r[1],od_r[1],od_r[2],om_r[2]])
            _sw_F(bm_io2_rj,[od_r[0],od_r[3],od_r[2],od_r[1]])
            _sw_F(bm_io2_rj,[im_r[0],im_r[1],ib_r[1],ib_r[0]]); _sw_F(bm_io2_rj,[im_r[2],im_r[3],ib_r[3],ib_r[2]])
            _sw_F(bm_io2_rj,[im_r[3],im_r[0],ib_r[0],ib_r[3]]); _sw_F(bm_io2_rj,[im_r[1],im_r[2],ib_r[2],ib_r[1]])
            _sw_F(bm_io2_rj,[ib_r[0],ib_r[1],ib_r[2],ib_r[3]])
            pin_y0_r=py_ib+0.0002; pin_y1_r=pin_y0_r+0.0003
            pin_z0_r=pz_r-RJ2_IH/2+0.001; sp_r2=RJ2_IW/9
            for pi_r2 in range(8):
                ppx_r2=(px_r-RJ2_IW/2)+(pi_r2+1)*sp_r2
                _sw_box(bm_io2_con,ppx_r2-0.0003,ppx_r2+0.0003,pin_y0_r,pin_y1_r,pin_z0_r,pin_z0_r+0.0011)

        # Build IEC for both PSUs
        _psu_cx_2L=(_psu2_x0L+_psu2_x1L)/2; _psu_cx_2R=(_psu2_x0R+_psu2_x1R)/2
        _build_iec_at_2u(_psu_cx_2L, _IEC2_CZ)
        _build_iec_at_2u(_psu_cx_2R, _IEC2_CZ)

        bm_psu2=bmesh.new(); bm_psu2_hdl=bmesh.new()
        bm_psu2_exh=bmesh.new(); bm_psu2_led=bmesh.new()

        for _p2x0,_p2x1,_p2cx in [(_psu2_x0L,_psu2_x1L,_psu_cx_2L),(_psu2_x0R,_psu2_x1R,_psu_cx_2R)]:
            _fp2_iz0=_IEC2_CZ-IEC_CUT_H_2/2; _fp2_iz1=_IEC2_CZ+IEC_CUT_H_2/2
            _fp2_ix0=_p2cx-IEC_CUT_W_2/2;   _fp2_ix1=_p2cx+IEC_CUT_W_2/2
            _fp2_x0=_p2x0+0.002; _fp2_x1=_p2x1-0.002
            _fp2_z0=-HH+0.003;   _fp2_z1=HH-0.003
            _sw_box(bm_psu2,_fp2_x0, _fp2_ix0,BACK_Y,BACK_Y+0.002,_fp2_z0,_fp2_z1)
            _sw_box(bm_psu2,_fp2_ix1,_fp2_x1, BACK_Y,BACK_Y+0.002,_fp2_z0,_fp2_z1)
            _sw_box(bm_psu2,_fp2_ix0,_fp2_ix1,BACK_Y,BACK_Y+0.002,_fp2_z0,_fp2_iz0)
            _sw_box(bm_psu2,_fp2_ix0,_fp2_ix1,BACK_Y,BACK_Y+0.002,_fp2_iz1,_fp2_z1)
            _sw_box(bm_psu2_hdl,_p2x0+0.005,_p2x1-0.005,BACK_Y+0.001,BACK_Y+0.006,HH-0.006,HH-0.002)
            _EX2_Z0=_IEC2_CZ+IEC_FLG_H_2/2+0.002; _EX2_Z1=HH-0.008
            _N_EX2=5; _SL2_H=0.0011
            _gap2=max(0.0006,(_EX2_Z1-_EX2_Z0-_N_EX2*_SL2_H)/(_N_EX2-1))
            for _ei2 in range(_N_EX2):
                _ez2=_EX2_Z0+_ei2*(_SL2_H+_gap2)
                _sw_box(bm_psu2_exh,_p2x0+0.006,_p2x1-0.006,BACK_Y+0.0025,BACK_Y+0.0030,_ez2,_ez2+_SL2_H)
            _FLK2_FX0=_p2cx-IEC_FLG_W_2/2; _FLK2_FX1=_p2cx+IEC_FLG_W_2/2
            _FLK2_Z0=_IEC2_CZ-IEC_FLG_H_2/2+0.002; _FLK2_Z1=_IEC2_CZ+IEC_FLG_H_2/2-0.002
            _N_FLK2=10
            _flk2_gap=max(0.0006,(_FLK2_Z1-_FLK2_Z0-_N_FLK2*_SL2_H)/(_N_FLK2-1))
            for _fi2 in range(_N_FLK2):
                _fz2=_FLK2_Z0+_fi2*(_SL2_H+_flk2_gap)
                _sw_box(bm_psu2_exh,_p2x0+0.004,_FLK2_FX0-0.002,BACK_Y+0.0025,BACK_Y+0.0030,_fz2,_fz2+_SL2_H)
                _sw_box(bm_psu2_exh,_FLK2_FX1+0.002,_p2x1-0.004,BACK_Y+0.0025,BACK_Y+0.0030,_fz2,_fz2+_SL2_H)
            _sw_box(bm_psu2_led,_p2cx+_psu2_ea*0.30,_p2cx+_psu2_ea*0.30+0.004,
                    BACK_Y+0.001,BACK_Y+0.003,HH*0.72,HH*0.72+0.004)

        parts.append(_sw_mesh_obj(f"{name}_psu_faces",        bm_psu2,      col, 'M_Aluminum'))
        parts.append(_sw_mesh_obj(f"{name}_psu_handles",      bm_psu2_hdl,  col, 'M_Black'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_body",     bm_iec2_all,  col, 'M_BlackMatte'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_flange",   bm_flg2_all,  col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_screws",   bm_iec2_scr,  col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_contacts", bm_iec2_con,  col, 'M_Gold'))
        parts.append(_sw_mesh_obj(f"{name}_psu_exhaust",      bm_psu2_exh,  col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_psu_leds",         bm_psu2_led,  col, 'M_LED_Green'))

        # PCIe brackets (2 slots)
        bm_pcie2=bmesh.new(); bm_pcie2_scr=bmesh.new()
        _pcie2_sw=(_pcie2_w-0.004)/2
        for _pi2 in range(2):
            _px2_0=_pcie2_x0+_pi2*(_pcie2_sw+0.004); _px2_1=_px2_0+_pcie2_sw; _scx2_p=(_px2_0+_px2_1)/2
            _sw_box(bm_pcie2,_px2_0+0.001,_px2_1-0.001,BACK_Y,BACK_Y+0.0015,-HH+0.002,HH-0.003)
            for _vi2_p in range(10):
                _vz2_p=-HH*0.65+_vi2_p*(h*0.78/10)
                _sw_box(bm_pcie2,_px2_0+0.003,_px2_1-0.003,BACK_Y+0.0002,BACK_Y+0.0015,_vz2_p,_vz2_p+0.0015)
            SCR2R=0.0022; SCR2T=0.0018; SCR2Y=BACK_Y+0.003; SCR2CZ=HH-0.006
            _fvp2=[]; _bvp2=[]
            for _si2_p in range(8):
                _a2_p=math.pi/8+2*math.pi*_si2_p/8
                _fvp2.append(bm_pcie2_scr.verts.new((_scx2_p+SCR2R*math.cos(_a2_p),SCR2Y,        SCR2CZ+SCR2R*math.sin(_a2_p))))
                _bvp2.append(bm_pcie2_scr.verts.new((_scx2_p+SCR2R*math.cos(_a2_p),SCR2Y+SCR2T,  SCR2CZ+SCR2R*math.sin(_a2_p))))
            _cfp2=bm_pcie2_scr.verts.new((_scx2_p,SCR2Y,       SCR2CZ))
            _cbp2=bm_pcie2_scr.verts.new((_scx2_p,SCR2Y+SCR2T, SCR2CZ))
            for _si2_p in range(8):
                _n2_p=(_si2_p+1)%8
                _sw_F(bm_pcie2_scr,[_fvp2[_si2_p],_fvp2[_n2_p],_bvp2[_n2_p],_bvp2[_si2_p]])
                try: bm_pcie2_scr.faces.new([_cfp2,_fvp2[_n2_p],_fvp2[_si2_p]])
                except: pass
                try: bm_pcie2_scr.faces.new([_cbp2,_bvp2[_si2_p],_bvp2[_n2_p]])
                except: pass
        parts.append(_sw_mesh_obj(f"{name}_pcie_brackets", bm_pcie2,     col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_pcie_screws",   bm_pcie2_scr, col, 'M_DarkGrayMet'))

        # Fan zone (2 modules)
        bm_fan2=bmesh.new()
        _fan2_mw=(_fan2_w-0.004)/2
        for _fi2 in range(2):
            _fx2_0=_fan2_x0+_fi2*(_fan2_mw+0.004); _fx2_1=_fx2_0+_fan2_mw
            _sw_box(bm_fan2,_fx2_0+0.001,_fx2_1-0.001,BACK_Y,BACK_Y+0.002,-HH+0.002,HH-0.003)
            _fh2_span=HH*1.60; _fh2_z0=-HH*0.80
            for _bi2 in range(4):
                _fbz2=_fh2_z0+_bi2*(_fh2_span/4)
                _sw_box(bm_fan2,_fx2_0+0.004,_fx2_1-0.004,BACK_Y+0.004,BACK_Y+0.008,_fbz2,_fbz2+0.003)
        parts.append(_sw_mesh_obj(f"{name}_fan_zone", bm_fan2, col, 'M_DarkGrayMet'))

        # IO cluster: 3 RJ45, 2 USB rear, VGA, DB9
        _build_rear_rj45_2u(_io2_x0+0.012, HH*0.40)
        _build_rear_rj45_2u(_io2_x0+0.030, HH*0.40)
        _build_rear_rj45_2u(_io2_x0+0.052, HH*0.40)
        bm_io2_con.verts.ensure_lookup_table()
        _n_io2_con=len(bm_io2_con.verts)//8
        for _ci2 in range(_n_io2_con):
            _b2c_=_ci2*8; _vs2_rc=bm_io2_con.verts[_b2c_:_b2c_+8]
            for _f_idx2 in [(0,1,2,3),(4,7,6,5),(0,4,5,1),(3,2,6,7),(0,3,7,4),(1,5,6,2)]:
                try: bm_io2_con.faces.new([_vs2_rc[_j2] for _j2 in _f_idx2])
                except: pass
        parts.append(_sw_mesh_obj(f"{name}_rear_rj45_housings", bm_io2_rj,  col, 'M_PlasticDark'))
        parts.append(_sw_mesh_obj(f"{name}_rear_rj45_contacts", bm_io2_con, col, 'M_Gold'))

        bm_io2_usb_r=bmesh.new()
        USB2_OW_R=0.0130; USB2_OH_R=0.0060; USB2_IW_R=0.0100; USB2_IH_R=0.0035; USB2_D_R=0.0100
        USB2_CX_R=_io2_x0+0.074
        for _ui_r2,_USB2CZR in enumerate([-HH*0.15,-HH*0.42]):
            _FYR2=BACK_Y+0.0005; _BYR2=_FYR2-USB2_D_R
            _sw_box(bm_io2_usb_r,USB2_CX_R-USB2_OW_R/2,USB2_CX_R+USB2_OW_R/2,_FYR2,_FYR2+0.0008,_USB2CZR+USB2_IH_R/2,_USB2CZR+USB2_OH_R/2)
            _sw_box(bm_io2_usb_r,USB2_CX_R-USB2_OW_R/2,USB2_CX_R+USB2_OW_R/2,_FYR2,_FYR2+0.0008,_USB2CZR-USB2_OH_R/2,_USB2CZR-USB2_IH_R/2)
            _sw_box(bm_io2_usb_r,USB2_CX_R-USB2_OW_R/2,USB2_CX_R-USB2_IW_R/2,_FYR2,_FYR2+0.0008,_USB2CZR-USB2_OH_R/2,_USB2CZR+USB2_OH_R/2)
            _sw_box(bm_io2_usb_r,USB2_CX_R+USB2_IW_R/2,USB2_CX_R+USB2_OW_R/2,_FYR2,_FYR2+0.0008,_USB2CZR-USB2_OH_R/2,_USB2CZR+USB2_OH_R/2)
            for _wpr2 in [(USB2_CX_R-USB2_OW_R/2,USB2_CX_R+USB2_OW_R/2,_USB2CZR+USB2_IH_R/2,_USB2CZR+USB2_OH_R/2),
                          (USB2_CX_R-USB2_OW_R/2,USB2_CX_R+USB2_OW_R/2,_USB2CZR-USB2_OH_R/2,_USB2CZR-USB2_IH_R/2),
                          (USB2_CX_R-USB2_OW_R/2,USB2_CX_R-USB2_IW_R/2,_USB2CZR-USB2_OH_R/2,_USB2CZR+USB2_OH_R/2),
                          (USB2_CX_R+USB2_IW_R/2,USB2_CX_R+USB2_OW_R/2,_USB2CZR-USB2_OH_R/2,_USB2CZR+USB2_OH_R/2)]:
                _sw_box(bm_io2_usb_r,_wpr2[0],_wpr2[1],_BYR2,_FYR2,_wpr2[2],_wpr2[3])
            _sw_box(bm_io2_usb_r,USB2_CX_R-USB2_OW_R/2,USB2_CX_R+USB2_OW_R/2,_BYR2,_BYR2+0.0005,_USB2CZR-USB2_OH_R/2,_USB2CZR+USB2_OH_R/2)
            _sw_box(bm_io2_usb_r,USB2_CX_R-USB2_IW_R/2+0.001,USB2_CX_R+USB2_IW_R/2-0.001,_BYR2+0.001,_FYR2-0.002,_USB2CZR,_USB2CZR+USB2_IH_R/2-0.0003)
        bm_io2_misc=bmesh.new()
        _sw_box(bm_io2_misc,_io2_x0+0.090,_io2_x0+0.108,BACK_Y+0.001,BACK_Y+0.004,HH*0.25-0.0075,HH*0.25+0.0075)
        _sw_box(bm_io2_misc,_io2_x0+0.090,_io2_x0+0.108,BACK_Y+0.001,BACK_Y+0.004,-HH*0.58-0.005,-HH*0.58+0.005)
        parts.append(_sw_mesh_obj(f"{name}_usb_rear",    bm_io2_usb_r, col, 'M_PlasticDark'))
        parts.append(_sw_mesh_obj(f"{name}_rear_io_misc",bm_io2_misc,  col, 'M_DarkGrayMet'))

        # Rear background panel
        bm_r2bg=bmesh.new()
        def _rbg2(x0,x1,z0,z1): _sw_box(bm_r2bg,x0,x1,BACK_Y-0.002,BACK_Y,z0,z1)
        # PCIe + fan zone: solid
        _rbg2(_pcie2_x0, _psu2_x0L, -HH, HH)
        # IO zone left margin + gap strip
        _rbg2(-HW, _io2_x0+0.004, -HH, HH)
        _rbg2(_io2_x0+_io2_w, _pcie2_x0, -HH, HH)
        # IO zone: tile around RJ45 openings (3 ports, same Z band)
        _RJ2_IHW = RJ2_IW / 2;  _RJ2_IHH = RJ2_IH / 2
        _RJ2_CZ  = HH * 0.40
        _RJ2_Z0  = _RJ2_CZ - _RJ2_IHH;  _RJ2_Z1 = _RJ2_CZ + _RJ2_IHH
        for _rjcx in [_io2_x0+0.012, _io2_x0+0.030, _io2_x0+0.052]:
            _rx0 = _rjcx - _RJ2_IHW;  _rx1 = _rjcx + _RJ2_IHW
            _rbg2(_rx0, _rx1, -HH, _RJ2_Z0)   # below RJ45 hole
            _rbg2(_rx0, _rx1, _RJ2_Z1, HH)    # above RJ45 hole
        # Solid columns between and around RJ45 ports
        _rbg2(_io2_x0+0.004,        _io2_x0+0.012-_RJ2_IHW, -HH, HH)
        _rbg2(_io2_x0+0.012+_RJ2_IHW, _io2_x0+0.030-_RJ2_IHW, -HH, HH)
        _rbg2(_io2_x0+0.030+_RJ2_IHW, _io2_x0+0.052-_RJ2_IHW, -HH, HH)
        _rbg2(_io2_x0+0.052+_RJ2_IHW, _io2_x0+0.074-USB2_IW_R/2, -HH, HH)
        # USB column: tile around two holes at different Z
        _USBCX_R = _io2_x0 + 0.074
        _USBRX0 = _USBCX_R - USB2_IW_R/2;  _USBRX1 = _USBCX_R + USB2_IW_R/2
        _USB1_Z0 = -HH*0.15 - USB2_IH_R/2;  _USB1_Z1 = -HH*0.15 + USB2_IH_R/2
        _USB2_Z0 = -HH*0.42 - USB2_IH_R/2;  _USB2_Z1 = -HH*0.42 + USB2_IH_R/2
        _rbg2(_USBRX0, _USBRX1, -HH,      _USB2_Z0)
        _rbg2(_USBRX0, _USBRX1, _USB2_Z1, _USB1_Z0)
        _rbg2(_USBRX0, _USBRX1, _USB1_Z1, HH)
        # Solid remainder: VGA+DB9 zone → right edge of IO zone
        _rbg2(_USBRX1, _io2_x0+_io2_w, -HH, HH)
        # PSU separators and right edge
        _rbg2(_psu2_x1L,_psu2_x0R,-HH,HH)                  # between PSUs
        _rbg2(_psu2_x1R,HW,-HH,HH)                          # right edge
        for _pbx0,_pbx1 in [(_psu2_x0L,_psu2_x1L),(_psu2_x0R,_psu2_x1R)]:
            _pcx2=(_pbx0+_pbx1)/2
            _bgiz0=_IEC2_CZ-IEC_CUT_H_2/2; _bgiz1=_IEC2_CZ+IEC_CUT_H_2/2
            _bgix0=_pcx2-IEC_CUT_W_2/2;    _bgix1=_pcx2+IEC_CUT_W_2/2
            _rbg2(_pbx0,_bgix0,-HH,HH); _rbg2(_bgix1,_pbx1,-HH,HH)
            _rbg2(_bgix0,_bgix1,-HH,_bgiz0); _rbg2(_bgix0,_bgix1,_bgiz1,HH)
        parts.append(_sw_mesh_obj(f"{name}_rear_panel_bg",bm_r2bg,col,'M_Aluminum'))

        # ── Translation: centred → equipment-origin ───────────────────────
        tx, ty, tz = 0.0, d / 2, h / 2
        for obj in parts[1:]:
            me = obj.data
            for v in me.vertices:
                v.co.x += tx; v.co.y += ty; v.co.z += tz
            me.update()
            obj.hide_render = False
        parts[0].hide_render = False

        # ── Mounting ears + M6 screws (equipment-origin space) ────────────
        ear_w_2u = (EIA_RAIL_SPAN_M - EIA_EQUIPMENT_BODY_M) / 2
        ear_d_2u = 0.002; ear_h_2u = h * 0.68
        for side_sign in (-1, 1):
            side_label = 'L' if side_sign < 0 else 'R'
            ear_cx_2u = side_sign * (w / 2 + ear_w_2u / 2)
            bm_ear2 = bmesh.new()
            _sw_box(bm_ear2,
                    ear_cx_2u - ear_w_2u/2, ear_cx_2u + ear_w_2u/2,
                    -ear_d_2u, 0.0,
                    (h - ear_h_2u)/2, (h + ear_h_2u)/2)
            parts.append(_sw_mesh_obj(f"{name}_ear_{side_label}", bm_ear2, col, 'M_Aluminum'))
            SCR2_R_E=0.0038; SCR2_T_E=0.0028; SCR2_Y_E=-(ear_d_2u+0.001); SCR2_Z_E=h/2
            bm_scr2e=bmesh.new(); fv2e=[]; bv2e=[]
            for _i2e in range(8):
                _a2e=math.pi/8+2*math.pi*_i2e/8
                fv2e.append(bm_scr2e.verts.new((ear_cx_2u+SCR2_R_E*math.cos(_a2e),SCR2_Y_E,           SCR2_Z_E+SCR2_R_E*math.sin(_a2e))))
                bv2e.append(bm_scr2e.verts.new((ear_cx_2u+SCR2_R_E*math.cos(_a2e),SCR2_Y_E+SCR2_T_E,  SCR2_Z_E+SCR2_R_E*math.sin(_a2e))))
            cf2e=bm_scr2e.verts.new((ear_cx_2u,SCR2_Y_E,           SCR2_Z_E))
            cb2e=bm_scr2e.verts.new((ear_cx_2u,SCR2_Y_E+SCR2_T_E,  SCR2_Z_E))
            for _i2e in range(8):
                _n2e=(_i2e+1)%8
                _sw_F(bm_scr2e,[fv2e[_i2e],fv2e[_n2e],bv2e[_n2e],bv2e[_i2e]])
                try: bm_scr2e.faces.new([cf2e,fv2e[_n2e],fv2e[_i2e]])
                except: pass
                try: bm_scr2e.faces.new([cb2e,bv2e[_i2e],bv2e[_n2e]])
                except: pass
            GRV2=0.0006; GRL2=SCR2_R_E*1.6
            _sw_box(bm_scr2e,ear_cx_2u-GRL2/2,ear_cx_2u+GRL2/2,SCR2_Y_E-0.0003,SCR2_Y_E,SCR2_Z_E-GRV2/2,SCR2_Z_E+GRV2/2)
            _sw_box(bm_scr2e,ear_cx_2u-GRV2/2,ear_cx_2u+GRV2/2,SCR2_Y_E-0.0003,SCR2_Y_E,SCR2_Z_E-GRL2/2,SCR2_Z_E+GRL2/2)
            parts.append(_sw_mesh_obj(f"{name}_ear_screw_{side_label}", bm_scr2e, col, 'M_DarkGrayMet'))

    elif u_size == 3:
        # ── Hero 3U server front + rear — centred coordinate system ──────
        _sw_ensure_materials()
        HW_3 = w / 2;  HH_3 = h / 2
        FRONT_Y_3 = -(d / 2);  BACK_Y_3 = d / 2

        # Delete chassis rear face
        _bm_r3 = bmesh.new()
        _bm_r3.from_mesh(parts[0].data)
        bmesh.ops.delete(_bm_r3,
            geom=[f for f in _bm_r3.faces if f.calc_center_median().y > d/2*0.97],
            context='FACES_ONLY')
        _bm_r3.to_mesh(parts[0].data); _bm_r3.free(); parts[0].data.update()

        # ── Front layout ──────────────────────────────────────────────────
        L_M3 = 0.010; R_M3 = 0.008
        CTRL_W3 = 0.060; VENT_W3 = 0.090
        CTRL_X0_3 = -HW_3 + L_M3;  CTRL_X1_3 = CTRL_X0_3 + CTRL_W3
        CTRL_CX_3 = (CTRL_X0_3 + CTRL_X1_3) / 2
        VENT_X1_3 = HW_3 - R_M3;   VENT_X0_3 = VENT_X1_3 - VENT_W3
        BAY_X0_3  = CTRL_X1_3 + 0.004;  BAY_X1_3 = VENT_X0_3 - 0.004
        BAY_ZW_3  = BAY_X1_3 - BAY_X0_3
        BAY_H_3   = h * 0.84;  BAY_Z0_3 = -BAY_H_3 / 2;  BAY_Z1_3 = BAY_H_3 / 2
        BAY_RD_3  = 0.012

        # Carrier grid (8 cols × 2 rows)
        NCOLS_3 = 8;  NROWS_3 = 2;  GX_3 = 0.0015;  GZ_3 = 0.004
        cw_3 = (BAY_ZW_3 - GX_3*(NCOLS_3-1)) / NCOLS_3
        ch_3 = (BAY_H_3  - GZ_3*(NROWS_3-1)) / NROWS_3

        # ── IEC C14 constants ─────────────────────────────────────────────
        IEC_CUT_W_3 = 0.0280; IEC_CUT_H_3 = 0.0220
        IEC_FLG_W_3 = 0.0390; IEC_FLG_H_3 = 0.0310
        IEC_SOCK_D_3 = 0.0200; IEC_FLG_T_3 = 0.0025; S_WALL_3 = 0.002

        # ── Rear layout ───────────────────────────────────────────────────
        _io3_w = 0.110; _pcie3_w = 0.060; _fan3_w = 0.080
        _psu3_ea = 0.085; _psu3_gap = 0.006; _rg3 = 0.005
        _io3_x0   = -HW_3
        _pcie3_x0 = _io3_x0   + _io3_w    + _rg3
        _fan3_x0  = _pcie3_x0 + _pcie3_w  + _rg3
        _psu3_x0L = _fan3_x0  + _fan3_w   + _rg3
        _psu3_x1L = _psu3_x0L + _psu3_ea
        _psu3_x0R = _psu3_x1L + _psu3_gap
        _psu3_x1R = _psu3_x0R + _psu3_ea
        _IEC3_CZ  = -HH_3 * 0.20

        # ── Front plate ───────────────────────────────────────────────────
        parts.append(_sw_holey_plate(
            f"{name}_front_plate", FRONT_Y_3,
            [(BAY_X0_3,        BAY_X1_3,        BAY_Z0_3,        BAY_Z1_3),
             (VENT_X0_3+0.001, VENT_X1_3-0.001, BAY_Z0_3+0.001,  BAY_Z1_3-0.001),
             (CTRL_X0_3+0.001, CTRL_X1_3-0.001, BAY_Z0_3+0.001,  BAY_Z1_3-0.001)],
            [], col, 'M_DarkGrayMet',
            x_min=-HW_3, x_max=HW_3, z_min=-HH_3, z_max=HH_3,
            outward_plus_y=False))

        # Top louver strip
        bm_louv3 = bmesh.new()
        for _li3 in range(5):
            _zt3 = HH_3 - 0.0003 - _li3 * 0.0022
            _sw_box(bm_louv3, -HW_3+0.005, HW_3-0.005,
                    FRONT_Y_3+h*0.3, FRONT_Y_3+h*0.8, _zt3-0.0004, _zt3+0.00005)
        parts.append(_sw_mesh_obj(f"{name}_top_louvers", bm_louv3, col, 'M_DarkGrayMet'))

        # Service tag
        bm_tag3 = bmesh.new()
        _TX3 = -HW_3 + 0.0065
        _sw_box(bm_tag3, _TX3-0.0055, _TX3+0.0055, FRONT_Y_3-0.0008, FRONT_Y_3, -h*0.26, h*0.26)
        _sw_box(bm_tag3, _TX3-0.003,  _TX3+0.003,  FRONT_Y_3-0.004,  FRONT_Y_3, -h*0.26-0.004, -h*0.26)
        parts.append(_sw_mesh_obj(f"{name}_svc_tag", bm_tag3, col, 'M_PlasticDark'))

        # Bay background (recessed)
        bm_bybg3 = bmesh.new()
        _sw_box(bm_bybg3, BAY_X0_3, BAY_X1_3, FRONT_Y_3+0.002, FRONT_Y_3+BAY_RD_3, BAY_Z0_3, BAY_Z1_3)
        parts.append(_sw_mesh_obj(f"{name}_bay_bg", bm_bybg3, col, 'M_PlasticDark'))

        # ── 8×2 carrier grid ─────────────────────────────────────────────
        bm_cf3 = bmesh.new(); bm_cv3 = bmesh.new()
        bm_ch3 = bmesh.new(); bm_cl3 = bmesh.new(); bm_clw3 = bmesh.new()
        _lbl3_objs = []
        LBL_Y3 = FRONT_Y_3 - 0.0020

        def _add_lbl3(txt, lx, lz):
            _fc3 = bpy.data.curves.new("_s3lbl", type='FONT')
            _fc3.body = txt; _fc3.size = 0.0028; _fc3.extrude = 0.00028
            _fc3.align_x = 'CENTER'; _fc3.align_y = 'CENTER'
            _o3 = bpy.data.objects.new("_s3lbl", _fc3)
            bpy.context.scene.collection.objects.link(_o3)
            _o3.rotation_euler = (math.pi/2, 0, 0)
            _o3.location = (lx, LBL_Y3, lz)
            _lbl3_objs.append(_o3)

        for _r3 in range(NROWS_3):
            for _c3 in range(NCOLS_3):
                _idx3  = _r3*NCOLS_3 + _c3
                _cx3   = BAY_X0_3 + (_c3+0.5)*cw_3 + _c3*GX_3
                _cz3   = BAY_Z0_3 + (_r3+0.5)*ch_3 + _r3*GZ_3
                _CY3_0 = FRONT_Y_3 + 0.0002;  _CY3_1 = FRONT_Y_3 - 0.0018
                _sw_box(bm_cf3, _cx3-cw_3/2+0.001, _cx3+cw_3/2-0.001,
                        _CY3_1, _CY3_0, _cz3-ch_3/2+0.001, _cz3+ch_3/2-0.001)
                _VW3 = cw_3*0.58; _vx3_0 = _cx3-_VW3/2+cw_3*0.08; _vx3_1 = _cx3+_VW3/2+cw_3*0.08
                for _vi3 in range(4):
                    _vz3 = _cz3 + (_vi3-1.5)*0.0060
                    _sw_box(bm_cv3, _vx3_0, _vx3_1, _CY3_1-0.0003, _CY3_1,
                            _vz3-0.00035, _vz3+0.00035)
                if qf["detailed_handles"]:
                    _HX3 = _cx3 - cw_3/2 + 0.0045
                    _sw_box(bm_ch3, _HX3-0.00275, _HX3+0.00275,
                            FRONT_Y_3-0.0038, FRONT_Y_3-0.0002, _cz3-ch_3*0.36, _cz3+ch_3*0.36)
                    _sw_box(bm_ch3, _HX3-0.00275, _HX3-0.00275+cw_3*0.22,
                            FRONT_Y_3-0.0038, FRONT_Y_3-0.0002, _cz3-ch_3*0.36, _cz3-ch_3*0.36+0.0042)
                if qf["led_emissive"]:
                    _LX3 = _cx3+cw_3/2-0.006
                    _LRZ3 = _cz3+ch_3/2-0.0035;  _LWZ3 = _LRZ3-0.0035
                    _sw_box(bm_cl3, _LX3-0.0012, _LX3+0.0012,
                            _CY3_1-0.0008, _CY3_1, _LRZ3-0.0012, _LRZ3+0.0012)
                    _sw_box(bm_clw3, _LX3-0.0012, _LX3+0.0012,
                            _CY3_1-0.0008, _CY3_1, _LWZ3-0.0012, _LWZ3+0.0012)
                if qf["bezel"]:
                    _add_lbl3(str(_idx3+1), _cx3, _cz3+ch_3/2-0.0025)

        parts.append(_sw_mesh_obj(f"{name}_carrier_faces",      bm_cf3,  col, 'M_PlasticDark'))
        parts.append(_sw_mesh_obj(f"{name}_carrier_vents",      bm_cv3,  col, 'M_Black'))
        parts.append(_sw_mesh_obj(f"{name}_carrier_handles",    bm_ch3,  col, 'M_Black'))
        parts.append(_sw_mesh_obj(f"{name}_carrier_leds",       bm_cl3,  col, 'M_LED_Green'))
        parts.append(_sw_mesh_obj(f"{name}_carrier_leds_write", bm_clw3, col, 'M_LED_Amber'))

        if qf["bezel"] and _lbl3_objs:
            bpy.context.view_layer.update()
            _dep3 = bpy.context.evaluated_depsgraph_get()
            bm_lbl3 = bmesh.new()
            for _fo3 in _lbl3_objs:
                _me_t3 = bpy.data.meshes.new_from_object(_fo3.evaluated_get(_dep3))
                _bm_t3 = bmesh.new(); _bm_t3.from_mesh(_me_t3)
                bmesh.ops.transform(_bm_t3, matrix=_fo3.matrix_world, verts=_bm_t3.verts[:])
                _nv3 = [bm_lbl3.verts.new(v.co) for v in _bm_t3.verts]
                bm_lbl3.verts.ensure_lookup_table(); _bm_t3.verts.ensure_lookup_table()
                _bm_t3.faces.ensure_lookup_table()
                for _ft3 in _bm_t3.faces:
                    try: bm_lbl3.faces.new([_nv3[v.index] for v in _ft3.verts])
                    except: pass
                _bm_t3.free(); bpy.data.meshes.remove(_me_t3)
                _fc3_d = _fo3.data; bpy.data.objects.remove(_fo3); bpy.data.curves.remove(_fc3_d)
            parts.append(_sw_mesh_obj(f"{name}_bay_labels", bm_lbl3, col, 'M_White'))

        # ── Honeycomb vent panel (right side) ─────────────────────────────
        bm_vent3 = bmesh.new()
        VX0_3 = VENT_X0_3+0.002; VX1_3 = VENT_X1_3-0.002
        _sw_box(bm_vent3, VX0_3, VX1_3, FRONT_Y_3+0.001, FRONT_Y_3+0.004, BAY_Z0_3, BAY_Z1_3)
        _VNT3_NC = 5; _VNT3_NR = 14; _VNT3_BH = 0.0014
        _vcw3 = (VX1_3-VX0_3) / _VNT3_NC
        _vrs3 = (BAY_H_3-0.004) / _VNT3_NR
        for _vc3 in range(_VNT3_NC):
            _vx0c3 = VX0_3+_vc3*_vcw3+0.0005; _vx1c3 = _vx0c3+_vcw3-0.001
            for _vr3 in range(_VNT3_NR):
                _vzb3 = BAY_Z0_3+0.002+_vr3*_vrs3
                _sw_box(bm_vent3, _vx0c3, _vx1c3,
                        FRONT_Y_3-0.0005, FRONT_Y_3+0.0005, _vzb3, _vzb3+_VNT3_BH)
        parts.append(_sw_mesh_obj(f"{name}_vent_panel", bm_vent3, col, 'M_DarkGrayMet'))

        # ── Left control panel (7-strip USB tiling for 3 USB ports) ──────
        bm_ctrl3 = bmesh.new()
        _P1_3CZ = -HH_3*0.03; _P1_3Z0 = _P1_3CZ-0.003; _P1_3Z1 = _P1_3CZ+0.003
        _P2_3CZ = -HH_3*0.28; _P2_3Z0 = _P2_3CZ-0.003; _P2_3Z1 = _P2_3CZ+0.003
        _P3_3CZ = -HH_3*0.52; _P3_3Z0 = _P3_3CZ-0.003; _P3_3Z1 = _P3_3CZ+0.003
        _USB3_OW = 0.0130; _USB3_OH = 0.0060
        _USB3_CX = CTRL_CX_3 + 0.006
        _USB3_X0 = _USB3_CX-_USB3_OW/2; _USB3_X1 = _USB3_CX+_USB3_OW/2
        _CB3_X0 = CTRL_X0_3+0.001; _CB3_X1 = CTRL_X1_3-0.001
        _CB3_Y0 = FRONT_Y_3-0.0005;  _CB3_Y1 = FRONT_Y_3+0.0020
        _CB3_Z0 = BAY_Z0_3+0.001;    _CB3_Z1 = BAY_Z1_3-0.001
        # 7 strips tiling around all 3 USB openings
        _sw_box(bm_ctrl3, _CB3_X0,  _USB3_X0, _CB3_Y0, _CB3_Y1, _CB3_Z0, _CB3_Z1)
        _sw_box(bm_ctrl3, _USB3_X1, _CB3_X1,  _CB3_Y0, _CB3_Y1, _CB3_Z0, _CB3_Z1)
        _sw_box(bm_ctrl3, _USB3_X0, _USB3_X1, _CB3_Y0, _CB3_Y1, _CB3_Z0, _P3_3Z0)
        _sw_box(bm_ctrl3, _USB3_X0, _USB3_X1, _CB3_Y0, _CB3_Y1, _P3_3Z1, _P2_3Z0)
        _sw_box(bm_ctrl3, _USB3_X0, _USB3_X1, _CB3_Y0, _CB3_Y1, _P2_3Z1, _P1_3Z0)
        _sw_box(bm_ctrl3, _USB3_X0, _USB3_X1, _CB3_Y0, _CB3_Y1, _P1_3Z1, _CB3_Z1)
        parts.append(_sw_mesh_obj(f"{name}_ctrl_bg", bm_ctrl3, col, 'M_DarkGrayMet'))

        # Power button (8-sided cap head)
        PWR3_CX = CTRL_CX_3+0.015; PWR3_CZ = HH_3*0.55
        PWR3_R = 0.0042; PWR3_T = 0.0030; PWR3_SEG = 8; PWR3_Y = FRONT_Y_3-0.0030
        bm_pwr3 = bmesh.new(); fv3p = []; bv3p = []
        for _i3p in range(PWR3_SEG):
            _a3p = math.pi/PWR3_SEG + 2*math.pi*_i3p/PWR3_SEG
            fv3p.append(bm_pwr3.verts.new((PWR3_CX+PWR3_R*math.cos(_a3p), PWR3_Y,         PWR3_CZ+PWR3_R*math.sin(_a3p))))
            bv3p.append(bm_pwr3.verts.new((PWR3_CX+PWR3_R*math.cos(_a3p), PWR3_Y+PWR3_T,  PWR3_CZ+PWR3_R*math.sin(_a3p))))
        cf3p = bm_pwr3.verts.new((PWR3_CX, PWR3_Y,         PWR3_CZ))
        cb3p = bm_pwr3.verts.new((PWR3_CX, PWR3_Y+PWR3_T,  PWR3_CZ))
        for _i3p in range(PWR3_SEG):
            _n3p = (_i3p+1)%PWR3_SEG
            _sw_F(bm_pwr3, [fv3p[_i3p], fv3p[_n3p], bv3p[_n3p], bv3p[_i3p]])
            try: bm_pwr3.faces.new([cf3p, fv3p[_n3p], fv3p[_i3p]])
            except: pass
            try: bm_pwr3.faces.new([cb3p, bv3p[_i3p], bv3p[_n3p]])
            except: pass
        parts.append(_sw_mesh_obj(f"{name}_pwr_btn", bm_pwr3, col, 'M_Black'))

        if qf["led_emissive"]:
            PWR3_RING_OR = PWR3_R+0.0018; PWR3_RING_IR = PWR3_R+0.0004; PWR3_RING_D = 0.0005
            bm_pwr3_led = bmesh.new()
            fr3_o=[]; fr3_i=[]; bk3_o=[]; bk3_i=[]
            for _i3r in range(16):
                _a3r = 2*math.pi*_i3r/16; _co3r = math.cos(_a3r); _si3r = math.sin(_a3r)
                fr3_o.append(bm_pwr3_led.verts.new((PWR3_CX+PWR3_RING_OR*_co3r, PWR3_Y,              PWR3_CZ+PWR3_RING_OR*_si3r)))
                fr3_i.append(bm_pwr3_led.verts.new((PWR3_CX+PWR3_RING_IR*_co3r, PWR3_Y,              PWR3_CZ+PWR3_RING_IR*_si3r)))
                bk3_o.append(bm_pwr3_led.verts.new((PWR3_CX+PWR3_RING_OR*_co3r, PWR3_Y+PWR3_RING_D,  PWR3_CZ+PWR3_RING_OR*_si3r)))
                bk3_i.append(bm_pwr3_led.verts.new((PWR3_CX+PWR3_RING_IR*_co3r, PWR3_Y+PWR3_RING_D,  PWR3_CZ+PWR3_RING_IR*_si3r)))
            for _i3r in range(16):
                _n3r = (_i3r+1)%16
                _sw_F(bm_pwr3_led, [fr3_o[_i3r], fr3_i[_i3r], fr3_i[_n3r], fr3_o[_n3r]])
                _sw_F(bm_pwr3_led, [bk3_o[_i3r], bk3_o[_n3r], bk3_i[_n3r], bk3_i[_i3r]])
                _sw_F(bm_pwr3_led, [fr3_o[_i3r], fr3_o[_n3r], bk3_o[_n3r], bk3_o[_i3r]])
                _sw_F(bm_pwr3_led, [fr3_i[_i3r], bk3_i[_i3r], bk3_i[_n3r], fr3_i[_n3r]])
            parts.append(_sw_mesh_obj(f"{name}_pwr_led", bm_pwr3_led, col, 'M_LED_Green'))

        # UID button + LED ring
        bm_uid3 = bmesh.new()
        UID3_CX = CTRL_CX_3+0.005; UID3_CZ = HH_3*0.42
        _sw_box(bm_uid3, UID3_CX-0.0025, UID3_CX+0.0025,
                FRONT_Y_3-0.0028, FRONT_Y_3-0.0005, UID3_CZ-0.0025, UID3_CZ+0.0025)
        parts.append(_sw_mesh_obj(f"{name}_uid_btn", bm_uid3, col, 'M_DarkGrayMet'))
        if qf["led_emissive"]:
            bm_uid3_led = bmesh.new()
            UID3_OR=0.0040; UID3_IR=0.0025; UID3_D=0.0004
            _sw_box(bm_uid3_led, UID3_CX-UID3_OR, UID3_CX+UID3_OR, FRONT_Y_3-0.0028, FRONT_Y_3-0.0028+UID3_D, UID3_CZ+UID3_IR, UID3_CZ+UID3_OR)
            _sw_box(bm_uid3_led, UID3_CX-UID3_OR, UID3_CX+UID3_OR, FRONT_Y_3-0.0028, FRONT_Y_3-0.0028+UID3_D, UID3_CZ-UID3_OR, UID3_CZ-UID3_IR)
            _sw_box(bm_uid3_led, UID3_CX-UID3_OR, UID3_CX-UID3_IR, FRONT_Y_3-0.0028, FRONT_Y_3-0.0028+UID3_D, UID3_CZ-UID3_OR, UID3_CZ+UID3_OR)
            _sw_box(bm_uid3_led, UID3_CX+UID3_IR, UID3_CX+UID3_OR, FRONT_Y_3-0.0028, FRONT_Y_3-0.0028+UID3_D, UID3_CZ-UID3_OR, UID3_CZ+UID3_OR)
            parts.append(_sw_mesh_obj(f"{name}_uid_led", bm_uid3_led, col, 'M_LED_Blue'))

        # Status LEDs (4 green + 1 amber)
        SLED3_CX = CTRL_X0_3 + 0.010
        _sled3_green_bm = bmesh.new(); _sled3_amber_bm = bmesh.new()
        for _sli3 in range(4):
            _slz3 = HH_3*0.20 - _sli3*0.010
            _sw_box(_sled3_green_bm, SLED3_CX-0.0015, SLED3_CX+0.0015,
                    FRONT_Y_3-0.0025, FRONT_Y_3-0.0005, _slz3-0.0015, _slz3+0.0015)
        _slz3_amb = HH_3*0.20 - 4*0.010
        _sw_box(_sled3_amber_bm, SLED3_CX-0.0015, SLED3_CX+0.0015,
                FRONT_Y_3-0.0025, FRONT_Y_3-0.0005, _slz3_amb-0.0015, _slz3_amb+0.0015)
        parts.append(_sw_mesh_obj(f"{name}_sled_green", _sled3_green_bm, col, 'M_LED_Green'))
        parts.append(_sw_mesh_obj(f"{name}_sled_amber", _sled3_amber_bm, col, 'M_LED_Amber'))

        # Front USB-A ×3 (4-strip tiling × 3 positions)
        bm_usb3 = bmesh.new()
        USB3_OW=0.0130; USB3_OH=0.0060; USB3_IW=0.0100; USB3_IH=0.0035; USB3_D=0.0100
        _USB3_CX_PORT = CTRL_CX_3 + 0.006
        for _ui3, _USBZ3 in enumerate([_P1_3CZ, _P2_3CZ, _P3_3CZ]):
            _FY3 = FRONT_Y_3-0.0005; _BY3 = _FY3+USB3_D; _UX3 = _USB3_CX_PORT
            _sw_box(bm_usb3, _UX3-USB3_OW/2, _UX3+USB3_OW/2, _FY3-0.0008, _FY3, _USBZ3+USB3_IH/2, _USBZ3+USB3_OH/2)
            _sw_box(bm_usb3, _UX3-USB3_OW/2, _UX3+USB3_OW/2, _FY3-0.0008, _FY3, _USBZ3-USB3_OH/2, _USBZ3-USB3_IH/2)
            _sw_box(bm_usb3, _UX3-USB3_OW/2, _UX3-USB3_IW/2, _FY3-0.0008, _FY3, _USBZ3-USB3_OH/2, _USBZ3+USB3_OH/2)
            _sw_box(bm_usb3, _UX3+USB3_IW/2, _UX3+USB3_OW/2, _FY3-0.0008, _FY3, _USBZ3-USB3_OH/2, _USBZ3+USB3_OH/2)
            for _wp3 in [(_UX3-USB3_OW/2,_UX3+USB3_OW/2,_USBZ3+USB3_IH/2,_USBZ3+USB3_OH/2),
                         (_UX3-USB3_OW/2,_UX3+USB3_OW/2,_USBZ3-USB3_OH/2,_USBZ3-USB3_IH/2),
                         (_UX3-USB3_OW/2,_UX3-USB3_IW/2,_USBZ3-USB3_OH/2,_USBZ3+USB3_OH/2),
                         (_UX3+USB3_IW/2,_UX3+USB3_OW/2,_USBZ3-USB3_OH/2,_USBZ3+USB3_OH/2)]:
                _sw_box(bm_usb3, _wp3[0], _wp3[1], _BY3, _FY3, _wp3[2], _wp3[3])
            _sw_box(bm_usb3, _UX3-USB3_OW/2, _UX3+USB3_OW/2, _BY3-0.0005, _BY3, _USBZ3-USB3_OH/2, _USBZ3+USB3_OH/2)
            _sw_box(bm_usb3, _UX3-USB3_IW/2+0.001, _UX3+USB3_IW/2-0.001, _FY3+0.002, _BY3-0.001, _USBZ3, _USBZ3+USB3_IH/2-0.0003)
        parts.append(_sw_mesh_obj(f"{name}_usb_front", bm_usb3, col, 'M_PlasticDark'))

        # VGA DE-15 front port — centred on USB column, D-shell frame + pins
        if qf["bezel"]:
            VGA3_CX = _USB3_CX_PORT          # align with USB ports above
            VGA3_CZ = -HH_3 * 0.72
            VGA3_OW = 0.0310; VGA3_OH = 0.0125
            VGA3_IW = 0.0255; VGA3_IH = 0.0092
            VGA3_Y0 = FRONT_Y_3 - 0.0042; VGA3_Y1 = FRONT_Y_3
            # D-shell outer frame (4 strips)
            bm_vga3 = bmesh.new()
            _sw_box(bm_vga3, VGA3_CX-VGA3_OW/2, VGA3_CX+VGA3_OW/2, VGA3_Y0, VGA3_Y1, VGA3_CZ-VGA3_OH/2, VGA3_CZ-VGA3_IH/2)
            _sw_box(bm_vga3, VGA3_CX-VGA3_OW/2, VGA3_CX+VGA3_OW/2, VGA3_Y0, VGA3_Y1, VGA3_CZ+VGA3_IH/2, VGA3_CZ+VGA3_OH/2)
            _sw_box(bm_vga3, VGA3_CX-VGA3_OW/2, VGA3_CX-VGA3_IW/2, VGA3_Y0, VGA3_Y1, VGA3_CZ-VGA3_OH/2, VGA3_CZ+VGA3_OH/2)
            _sw_box(bm_vga3, VGA3_CX+VGA3_IW/2, VGA3_CX+VGA3_OW/2, VGA3_Y0, VGA3_Y1, VGA3_CZ-VGA3_OH/2, VGA3_CZ+VGA3_OH/2)
            # Jack screw posts (6-sided, one each side)
            for _js3x in [VGA3_CX - VGA3_OW/2 - 0.0032, VGA3_CX + VGA3_OW/2 + 0.0032]:
                _js3r = 0.0026
                _js3vf = []; _js3vb = []
                for _i3j in range(6):
                    _a3j = math.pi/6 + 2*math.pi*_i3j/6
                    _js3vf.append(bm_vga3.verts.new((_js3x+_js3r*math.cos(_a3j), VGA3_Y0,        VGA3_CZ+_js3r*math.sin(_a3j))))
                    _js3vb.append(bm_vga3.verts.new((_js3x+_js3r*math.cos(_a3j), VGA3_Y0+0.0014, VGA3_CZ+_js3r*math.sin(_a3j))))
                _js3c = bm_vga3.verts.new((_js3x, VGA3_Y0+0.0014, VGA3_CZ))
                for _i3j in range(6):
                    _n3j = (_i3j+1)%6
                    _sw_F(bm_vga3, [_js3vf[_i3j], _js3vf[_n3j], _js3vb[_n3j], _js3vb[_i3j]])
                    try: bm_vga3.faces.new([_js3c, _js3vb[_i3j], _js3vb[_n3j]])
                    except: pass
            parts.append(_sw_mesh_obj(f"{name}_vga_front", bm_vga3, col, 'M_DarkGrayMet'))
            # Inner recess
            bm_vga3_in = bmesh.new()
            _sw_box(bm_vga3_in, VGA3_CX-VGA3_IW/2, VGA3_CX+VGA3_IW/2, VGA3_Y1, VGA3_Y1+0.0010, VGA3_CZ-VGA3_IH/2, VGA3_CZ+VGA3_IH/2)
            parts.append(_sw_mesh_obj(f"{name}_vga_inner", bm_vga3_in, col, 'M_PlasticDark'))
            # 15 pins: 3 rows × 5 cols
            bm_vga3_pins = bmesh.new()
            _vga3_pr = 0.00055
            for _vr3 in range(3):
                _vpz3 = VGA3_CZ + (_vr3 - 1) * (VGA3_IH * 0.30)
                for _vc3 in range(5):
                    _vpx3 = VGA3_CX + (_vc3 - 2) * (VGA3_IW * 0.175)
                    _sw_box(bm_vga3_pins, _vpx3-_vga3_pr, _vpx3+_vga3_pr, VGA3_Y1-0.0008, VGA3_Y1, _vpz3-_vga3_pr, _vpz3+_vga3_pr)
            parts.append(_sw_mesh_obj(f"{name}_vga_pins", bm_vga3_pins, col, 'M_Gold'))

        # ── Rear: IEC C14 helper ─────────────────────────────────────────
        bm_psu3_iec_body=bmesh.new(); bm_psu3_iec_flange=bmesh.new()
        bm_psu3_iec_screws=bmesh.new(); bm_psu3_iec_contacts=bmesh.new()

        def _build_iec_at_3u(psu_cx_iec, psu_cz_iec):
            CX_iec=psu_cx_iec; CZ_iec=psu_cz_iec
            ox0=CX_iec-IEC_FLG_W_3/2; ox1=CX_iec+IEC_FLG_W_3/2
            oz0=CZ_iec-IEC_FLG_H_3/2; oz1=CZ_iec+IEC_FLG_H_3/2
            cx0=CX_iec-IEC_CUT_W_3/2; cx1=CX_iec+IEC_CUT_W_3/2
            cz0=CZ_iec-IEC_CUT_H_3/2; cz1=CZ_iec+IEC_CUT_H_3/2
            ix0=cx0+S_WALL_3; ix1=cx1-S_WALL_3; iz0=cz0+S_WALL_3; iz1=cz1-S_WALL_3
            FLG_Y0=BACK_Y_3; FLG_Y1=BACK_Y_3+IEC_FLG_T_3; SOCK_Y1=BACK_Y_3-IEC_SOCK_D_3
            of_v=[bm_psu3_iec_body.verts.new((ox0,FLG_Y0,oz0)),bm_psu3_iec_body.verts.new((ox1,FLG_Y0,oz0)),
                  bm_psu3_iec_body.verts.new((ox1,FLG_Y0,oz1)),bm_psu3_iec_body.verts.new((ox0,FLG_Y0,oz1))]
            ob_v=[bm_psu3_iec_body.verts.new((ox0,SOCK_Y1,oz0)),bm_psu3_iec_body.verts.new((ox1,SOCK_Y1,oz0)),
                  bm_psu3_iec_body.verts.new((ox1,SOCK_Y1,oz1)),bm_psu3_iec_body.verts.new((ox0,SOCK_Y1,oz1))]
            cf_v=[bm_psu3_iec_body.verts.new((cx0,FLG_Y0,cz0)),bm_psu3_iec_body.verts.new((cx1,FLG_Y0,cz0)),
                  bm_psu3_iec_body.verts.new((cx1,FLG_Y0,cz1)),bm_psu3_iec_body.verts.new((cx0,FLG_Y0,cz1))]
            it_v=[bm_psu3_iec_body.verts.new((ix0,FLG_Y0,iz0)),bm_psu3_iec_body.verts.new((ix1,FLG_Y0,iz0)),
                  bm_psu3_iec_body.verts.new((ix1,FLG_Y0,iz1)),bm_psu3_iec_body.verts.new((ix0,FLG_Y0,iz1))]
            ib_v=[bm_psu3_iec_body.verts.new((ix0,SOCK_Y1,iz0)),bm_psu3_iec_body.verts.new((ix1,SOCK_Y1,iz0)),
                  bm_psu3_iec_body.verts.new((ix1,SOCK_Y1,iz1)),bm_psu3_iec_body.verts.new((ix0,SOCK_Y1,iz1))]
            _sw_F(bm_psu3_iec_body,[of_v[0],of_v[1],cf_v[1],cf_v[0]]); _sw_F(bm_psu3_iec_body,[of_v[3],cf_v[3],cf_v[2],of_v[2]])
            _sw_F(bm_psu3_iec_body,[of_v[0],cf_v[0],cf_v[3],of_v[3]]); _sw_F(bm_psu3_iec_body,[of_v[1],of_v[2],cf_v[2],cf_v[1]])
            _sw_F(bm_psu3_iec_body,[of_v[0],ob_v[0],ob_v[1],of_v[1]]); _sw_F(bm_psu3_iec_body,[of_v[3],of_v[2],ob_v[2],ob_v[3]])
            _sw_F(bm_psu3_iec_body,[of_v[0],of_v[3],ob_v[3],ob_v[0]]); _sw_F(bm_psu3_iec_body,[of_v[1],ob_v[1],ob_v[2],of_v[2]])
            _sw_F(bm_psu3_iec_body,[ob_v[0],ob_v[3],ob_v[2],ob_v[1]])
            _sw_F(bm_psu3_iec_body,[cf_v[0],cf_v[1],it_v[1],it_v[0]]); _sw_F(bm_psu3_iec_body,[cf_v[3],it_v[3],it_v[2],cf_v[2]])
            _sw_F(bm_psu3_iec_body,[cf_v[0],it_v[0],it_v[3],cf_v[3]]); _sw_F(bm_psu3_iec_body,[cf_v[1],cf_v[2],it_v[2],it_v[1]])
            _sw_F(bm_psu3_iec_body,[it_v[0],it_v[1],ib_v[1],ib_v[0]]); _sw_F(bm_psu3_iec_body,[it_v[3],ib_v[3],ib_v[2],it_v[2]])
            _sw_F(bm_psu3_iec_body,[it_v[0],ib_v[0],ib_v[3],it_v[3]]); _sw_F(bm_psu3_iec_body,[it_v[1],it_v[2],ib_v[2],ib_v[1]])
            _sw_F(bm_psu3_iec_body,[ib_v[0],ib_v[1],ib_v[2],ib_v[3]])
            f0_v=[bm_psu3_iec_flange.verts.new((ox0,FLG_Y0,oz0)),bm_psu3_iec_flange.verts.new((ox1,FLG_Y0,oz0)),
                  bm_psu3_iec_flange.verts.new((ox1,FLG_Y0,oz1)),bm_psu3_iec_flange.verts.new((ox0,FLG_Y0,oz1))]
            f1_v=[bm_psu3_iec_flange.verts.new((ox0,FLG_Y1,oz0)),bm_psu3_iec_flange.verts.new((ox1,FLG_Y1,oz0)),
                  bm_psu3_iec_flange.verts.new((ox1,FLG_Y1,oz1)),bm_psu3_iec_flange.verts.new((ox0,FLG_Y1,oz1))]
            c0_v=[bm_psu3_iec_flange.verts.new((cx0,FLG_Y0,cz0)),bm_psu3_iec_flange.verts.new((cx1,FLG_Y0,cz0)),
                  bm_psu3_iec_flange.verts.new((cx1,FLG_Y0,cz1)),bm_psu3_iec_flange.verts.new((cx0,FLG_Y0,cz1))]
            c1_v=[bm_psu3_iec_flange.verts.new((cx0,FLG_Y1,cz0)),bm_psu3_iec_flange.verts.new((cx1,FLG_Y1,cz0)),
                  bm_psu3_iec_flange.verts.new((cx1,FLG_Y1,cz1)),bm_psu3_iec_flange.verts.new((cx0,FLG_Y1,cz1))]
            _sw_F(bm_psu3_iec_flange,[f1_v[0],f1_v[1],c1_v[1],c1_v[0]]); _sw_F(bm_psu3_iec_flange,[f1_v[3],c1_v[3],c1_v[2],f1_v[2]])
            _sw_F(bm_psu3_iec_flange,[f1_v[0],c1_v[0],c1_v[3],f1_v[3]]); _sw_F(bm_psu3_iec_flange,[f1_v[1],f1_v[2],c1_v[2],c1_v[1]])
            _sw_F(bm_psu3_iec_flange,[f0_v[0],c0_v[0],c0_v[1],f0_v[1]]); _sw_F(bm_psu3_iec_flange,[f0_v[3],f0_v[2],c0_v[2],c0_v[3]])
            _sw_F(bm_psu3_iec_flange,[f0_v[0],f0_v[3],c0_v[3],c0_v[0]]); _sw_F(bm_psu3_iec_flange,[f0_v[1],c0_v[1],c0_v[2],f0_v[2]])
            for _i3f in range(4):
                _sw_F(bm_psu3_iec_flange,[f0_v[_i3f],f1_v[_i3f],f1_v[(_i3f+1)%4],f0_v[(_i3f+1)%4]])
            SR3=0.002; ST3=0.001; NS3=12
            for scx3 in [CX_iec-(IEC_CUT_W_3/2+(IEC_FLG_W_3/2-IEC_CUT_W_3/2)/2),
                         CX_iec+(IEC_CUT_W_3/2+(IEC_FLG_W_3/2-IEC_CUT_W_3/2)/2)]:
                _rb_v3=[]; _rf_v3=[]
                for _si3 in range(NS3):
                    _a3s=2*math.pi*_si3/NS3
                    _rb_v3.append(bm_psu3_iec_screws.verts.new((scx3+SR3*math.cos(_a3s),FLG_Y1,       CZ_iec+SR3*math.sin(_a3s))))
                    _rf_v3.append(bm_psu3_iec_screws.verts.new((scx3+SR3*math.cos(_a3s),FLG_Y1+ST3,   CZ_iec+SR3*math.sin(_a3s))))
                _cf3s=bm_psu3_iec_screws.verts.new((scx3,FLG_Y1+ST3,CZ_iec))
                for _si3 in range(NS3):
                    _sw_F(bm_psu3_iec_screws,[_rb_v3[_si3],_rf_v3[_si3],_rf_v3[(_si3+1)%NS3],_rb_v3[(_si3+1)%NS3]])
                    try: bm_psu3_iec_screws.faces.new([_cf3s,_rf_v3[_si3],_rf_v3[(_si3+1)%NS3]])
                    except: pass
            PY0_3=SOCK_Y1+0.0005; PY1_3=PY0_3+0.001
            def _b3c(cx_b,cz_b,bw,bh):
                _sw_box(bm_psu3_iec_contacts,cx_b-bw/2,cx_b+bw/2,PY0_3,PY1_3,cz_b-bh/2,cz_b+bh/2)
            _b3c(CX_iec,CZ_iec+0.0055,0.007,0.005)
            _b3c(CX_iec+0.0075,CZ_iec-0.0045,0.0038,0.009)
            _b3c(CX_iec-0.0075,CZ_iec-0.0045,0.0038,0.009)

        # ── Rear: RJ45 helper ────────────────────────────────────────────
        bm_io3_rj=bmesh.new(); bm_io3_con=bmesh.new()
        RJ3_OW=0.0160; RJ3_OH=0.0130; RJ3_WALL=0.0014
        RJ3_IW=RJ3_OW-2*RJ3_WALL; RJ3_IH=RJ3_OH-2*RJ3_WALL
        RJ3_CHAM=0.00048; RJ3_PROT=0.00150; RJ3_DPT=0.0160

        def _build_rear_rj45_3u(px_r,pz_r):
            py_m=BACK_Y_3+RJ3_PROT; py_d=py_m-RJ3_DPT; py_ib=py_d+RJ3_WALL
            om_r=[bm_io3_rj.verts.new((px_r-RJ3_OW/2,py_m,pz_r-RJ3_OH/2)),
                  bm_io3_rj.verts.new((px_r+RJ3_OW/2,py_m,pz_r-RJ3_OH/2)),
                  bm_io3_rj.verts.new((px_r+RJ3_OW/2,py_m,pz_r+RJ3_OH/2)),
                  bm_io3_rj.verts.new((px_r-RJ3_OW/2,py_m,pz_r+RJ3_OH/2))]
            im_r=[bm_io3_rj.verts.new((px_r-RJ3_IW/2+RJ3_CHAM,py_m,pz_r-RJ3_IH/2+RJ3_CHAM)),
                  bm_io3_rj.verts.new((px_r+RJ3_IW/2-RJ3_CHAM,py_m,pz_r-RJ3_IH/2+RJ3_CHAM)),
                  bm_io3_rj.verts.new((px_r+RJ3_IW/2-RJ3_CHAM,py_m,pz_r+RJ3_IH/2-RJ3_CHAM)),
                  bm_io3_rj.verts.new((px_r-RJ3_IW/2+RJ3_CHAM,py_m,pz_r+RJ3_IH/2-RJ3_CHAM))]
            od_r=[bm_io3_rj.verts.new((px_r-RJ3_OW/2,py_d,pz_r-RJ3_OH/2)),
                  bm_io3_rj.verts.new((px_r+RJ3_OW/2,py_d,pz_r-RJ3_OH/2)),
                  bm_io3_rj.verts.new((px_r+RJ3_OW/2,py_d,pz_r+RJ3_OH/2)),
                  bm_io3_rj.verts.new((px_r-RJ3_OW/2,py_d,pz_r+RJ3_OH/2))]
            ib_r=[bm_io3_rj.verts.new((px_r-RJ3_IW/2,py_ib,pz_r-RJ3_IH/2)),
                  bm_io3_rj.verts.new((px_r+RJ3_IW/2,py_ib,pz_r-RJ3_IH/2)),
                  bm_io3_rj.verts.new((px_r+RJ3_IW/2,py_ib,pz_r+RJ3_IH/2)),
                  bm_io3_rj.verts.new((px_r-RJ3_IW/2,py_ib,pz_r+RJ3_IH/2))]
            _sw_F(bm_io3_rj,[om_r[0],om_r[1],im_r[1],im_r[0]]); _sw_F(bm_io3_rj,[om_r[2],om_r[3],im_r[3],im_r[2]])
            _sw_F(bm_io3_rj,[om_r[3],om_r[0],im_r[0],im_r[3]]); _sw_F(bm_io3_rj,[om_r[1],om_r[2],im_r[2],im_r[1]])
            _sw_F(bm_io3_rj,[om_r[0],od_r[0],od_r[1],om_r[1]]); _sw_F(bm_io3_rj,[om_r[3],od_r[3],od_r[2],om_r[2]])
            _sw_F(bm_io3_rj,[om_r[3],om_r[0],od_r[0],od_r[3]]); _sw_F(bm_io3_rj,[om_r[1],od_r[1],od_r[2],om_r[2]])
            _sw_F(bm_io3_rj,[od_r[0],od_r[3],od_r[2],od_r[1]])
            _sw_F(bm_io3_rj,[im_r[0],im_r[1],ib_r[1],ib_r[0]]); _sw_F(bm_io3_rj,[im_r[2],im_r[3],ib_r[3],ib_r[2]])
            _sw_F(bm_io3_rj,[im_r[3],im_r[0],ib_r[0],ib_r[3]]); _sw_F(bm_io3_rj,[im_r[1],im_r[2],ib_r[2],ib_r[1]])
            _sw_F(bm_io3_rj,[ib_r[0],ib_r[1],ib_r[2],ib_r[3]])
            pin_y0_r=py_ib+0.0002; pin_y1_r=pin_y0_r+0.0003
            pin_z0_r=pz_r-RJ3_IH/2+0.001; sp_r3=RJ3_IW/9
            for pi_r3 in range(8):
                ppx_r3=(px_r-RJ3_IW/2)+(pi_r3+1)*sp_r3
                _sw_box(bm_io3_con,ppx_r3-0.0003,ppx_r3+0.0003,pin_y0_r,pin_y1_r,pin_z0_r,pin_z0_r+0.0011)

        # Build IEC for both PSUs
        _psu_cx_3L=(_psu3_x0L+_psu3_x1L)/2; _psu_cx_3R=(_psu3_x0R+_psu3_x1R)/2
        _build_iec_at_3u(_psu_cx_3L, _IEC3_CZ)
        _build_iec_at_3u(_psu_cx_3R, _IEC3_CZ)

        bm_psu3=bmesh.new(); bm_psu3_hdl=bmesh.new()
        bm_psu3_exh=bmesh.new(); bm_psu3_led=bmesh.new()

        for _p3x0,_p3x1,_p3cx in [(_psu3_x0L,_psu3_x1L,_psu_cx_3L),(_psu3_x0R,_psu3_x1R,_psu_cx_3R)]:
            _fp3_iz0=_IEC3_CZ-IEC_CUT_H_3/2; _fp3_iz1=_IEC3_CZ+IEC_CUT_H_3/2
            _fp3_ix0=_p3cx-IEC_CUT_W_3/2;   _fp3_ix1=_p3cx+IEC_CUT_W_3/2
            _fp3_x0=_p3x0+0.002; _fp3_x1=_p3x1-0.002
            _fp3_z0=-HH_3+0.003;   _fp3_z1=HH_3-0.003
            _sw_box(bm_psu3,_fp3_x0, _fp3_ix0,BACK_Y_3,BACK_Y_3+0.002,_fp3_z0,_fp3_z1)
            _sw_box(bm_psu3,_fp3_ix1,_fp3_x1, BACK_Y_3,BACK_Y_3+0.002,_fp3_z0,_fp3_z1)
            _sw_box(bm_psu3,_fp3_ix0,_fp3_ix1,BACK_Y_3,BACK_Y_3+0.002,_fp3_z0,_fp3_iz0)
            _sw_box(bm_psu3,_fp3_ix0,_fp3_ix1,BACK_Y_3,BACK_Y_3+0.002,_fp3_iz1,_fp3_z1)
            _sw_box(bm_psu3_hdl,_p3x0+0.005,_p3x1-0.005,BACK_Y_3+0.001,BACK_Y_3+0.006,HH_3-0.006,HH_3-0.002)
            _EX3_Z0=_IEC3_CZ+IEC_FLG_H_3/2+0.002; _EX3_Z1=HH_3-0.008
            _N_EX3=5; _SL3_H=0.0011
            _gap3=max(0.0006,(_EX3_Z1-_EX3_Z0-_N_EX3*_SL3_H)/(_N_EX3-1))
            for _ei3 in range(_N_EX3):
                _ez3=_EX3_Z0+_ei3*(_SL3_H+_gap3)
                _sw_box(bm_psu3_exh,_p3x0+0.006,_p3x1-0.006,BACK_Y_3+0.0025,BACK_Y_3+0.0030,_ez3,_ez3+_SL3_H)
            _FLK3_FX0=_p3cx-IEC_FLG_W_3/2; _FLK3_FX1=_p3cx+IEC_FLG_W_3/2
            _FLK3_Z0=_IEC3_CZ-IEC_FLG_H_3/2+0.002; _FLK3_Z1=_IEC3_CZ+IEC_FLG_H_3/2-0.002
            _N_FLK3=12
            _flk3_gap=max(0.0006,(_FLK3_Z1-_FLK3_Z0-_N_FLK3*_SL3_H)/(_N_FLK3-1))
            for _fi3 in range(_N_FLK3):
                _fz3=_FLK3_Z0+_fi3*(_SL3_H+_flk3_gap)
                _sw_box(bm_psu3_exh,_p3x0+0.004,_FLK3_FX0-0.002,BACK_Y_3+0.0025,BACK_Y_3+0.0030,_fz3,_fz3+_SL3_H)
                _sw_box(bm_psu3_exh,_FLK3_FX1+0.002,_p3x1-0.004,BACK_Y_3+0.0025,BACK_Y_3+0.0030,_fz3,_fz3+_SL3_H)
            _sw_box(bm_psu3_led,_p3cx+_psu3_ea*0.30,_p3cx+_psu3_ea*0.30+0.004,
                    BACK_Y_3+0.001,BACK_Y_3+0.003,HH_3*0.72,HH_3*0.72+0.004)

        parts.append(_sw_mesh_obj(f"{name}_psu_faces",        bm_psu3,              col, 'M_Aluminum'))
        parts.append(_sw_mesh_obj(f"{name}_psu_handles",      bm_psu3_hdl,          col, 'M_Black'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_body",     bm_psu3_iec_body,     col, 'M_BlackMatte'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_flange",   bm_psu3_iec_flange,   col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_screws",   bm_psu3_iec_screws,   col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_psu_iec_contacts", bm_psu3_iec_contacts, col, 'M_Gold'))
        parts.append(_sw_mesh_obj(f"{name}_psu_exhaust",      bm_psu3_exh,          col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_psu_leds",         bm_psu3_led,          col, 'M_LED_Green'))

        # PCIe brackets (3 slots)
        bm_pcie3=bmesh.new(); bm_pcie3_scr=bmesh.new()
        _pcie3_sw=(_pcie3_w-2*0.004)/3
        for _pi3 in range(3):
            _px3_0=_pcie3_x0+0.002+_pi3*(_pcie3_sw+0.002); _px3_1=_px3_0+_pcie3_sw; _scx3_p=(_px3_0+_px3_1)/2
            _sw_box(bm_pcie3,_px3_0+0.001,_px3_1-0.001,BACK_Y_3,BACK_Y_3+0.0015,-HH_3+0.002,HH_3-0.003)
            if qf["bay_3d"]:
                for _vi3_p in range(10):
                    _vz3_p=-HH_3*0.65+_vi3_p*(h*0.78/10)
                    _sw_box(bm_pcie3,_px3_0+0.003,_px3_1-0.003,BACK_Y_3+0.0002,BACK_Y_3+0.0015,_vz3_p,_vz3_p+0.0015)
            SCR3R=0.0022; SCR3T=0.0018; SCR3Y=BACK_Y_3+0.003; SCR3CZ=HH_3-0.006
            _fvp3=[]; _bvp3=[]
            for _si3_p in range(8):
                _a3_p=math.pi/8+2*math.pi*_si3_p/8
                _fvp3.append(bm_pcie3_scr.verts.new((_scx3_p+SCR3R*math.cos(_a3_p),SCR3Y,        SCR3CZ+SCR3R*math.sin(_a3_p))))
                _bvp3.append(bm_pcie3_scr.verts.new((_scx3_p+SCR3R*math.cos(_a3_p),SCR3Y+SCR3T,  SCR3CZ+SCR3R*math.sin(_a3_p))))
            _cfp3=bm_pcie3_scr.verts.new((_scx3_p,SCR3Y,       SCR3CZ))
            _cbp3=bm_pcie3_scr.verts.new((_scx3_p,SCR3Y+SCR3T, SCR3CZ))
            for _si3_p in range(8):
                _n3_p=(_si3_p+1)%8
                _sw_F(bm_pcie3_scr,[_fvp3[_si3_p],_fvp3[_n3_p],_bvp3[_n3_p],_bvp3[_si3_p]])
                try: bm_pcie3_scr.faces.new([_cfp3,_fvp3[_n3_p],_fvp3[_si3_p]])
                except: pass
                try: bm_pcie3_scr.faces.new([_cbp3,_bvp3[_si3_p],_bvp3[_n3_p]])
                except: pass
        parts.append(_sw_mesh_obj(f"{name}_pcie_brackets", bm_pcie3,     col, 'M_DarkGrayMet'))
        parts.append(_sw_mesh_obj(f"{name}_pcie_screws",   bm_pcie3_scr, col, 'M_DarkGrayMet'))

        # Fan zone (3 modules)
        bm_fan3=bmesh.new()
        _fan3_mw=(_fan3_w-2*0.004)/3
        for _fi3 in range(3):
            _fx3_0=_fan3_x0+0.002+_fi3*(_fan3_mw+0.002); _fx3_1=_fx3_0+_fan3_mw
            _sw_box(bm_fan3,_fx3_0+0.001,_fx3_1-0.001,BACK_Y_3,BACK_Y_3+0.002,-HH_3+0.002,HH_3-0.003)
            _fh3_span=HH_3*1.60; _fh3_z0=-HH_3*0.80
            for _bi3 in range(4):
                _fbz3=_fh3_z0+_bi3*(_fh3_span/4)
                _sw_box(bm_fan3,_fx3_0+0.004,_fx3_1-0.004,BACK_Y_3+0.004,BACK_Y_3+0.008,_fbz3,_fbz3+0.003)
        parts.append(_sw_mesh_obj(f"{name}_fan_zone", bm_fan3, col, 'M_DarkGrayMet'))

        # IO cluster: 3 RJ45, 2 USB rear, VGA, DB9
        _build_rear_rj45_3u(_io3_x0+0.012, HH_3*0.40)
        _build_rear_rj45_3u(_io3_x0+0.030, HH_3*0.40)
        _build_rear_rj45_3u(_io3_x0+0.052, HH_3*0.40)
        bm_io3_con.verts.ensure_lookup_table()
        _n_io3_con=len(bm_io3_con.verts)//8
        for _ci3 in range(_n_io3_con):
            _b3c_=_ci3*8; _vs3_rc=bm_io3_con.verts[_b3c_:_b3c_+8]
            for _f_idx3 in [(0,1,2,3),(4,7,6,5),(0,4,5,1),(3,2,6,7),(0,3,7,4),(1,5,6,2)]:
                try: bm_io3_con.faces.new([_vs3_rc[_j3] for _j3 in _f_idx3])
                except: pass
        parts.append(_sw_mesh_obj(f"{name}_rear_rj45_housings", bm_io3_rj,  col, 'M_PlasticDark'))
        parts.append(_sw_mesh_obj(f"{name}_rear_rj45_contacts", bm_io3_con, col, 'M_Gold'))

        bm_io3_usb_r=bmesh.new()
        USB3_OW_R=0.0130; USB3_OH_R=0.0060; USB3_IW_R=0.0100; USB3_IH_R=0.0035; USB3_D_R=0.0100
        USB3_CX_R=_io3_x0+0.074
        for _ui_r3,_USB3CZR in enumerate([-HH_3*0.15,-HH_3*0.42]):
            _FYR3=BACK_Y_3+0.0005; _BYR3=_FYR3-USB3_D_R
            _sw_box(bm_io3_usb_r,USB3_CX_R-USB3_OW_R/2,USB3_CX_R+USB3_OW_R/2,_FYR3,_FYR3+0.0008,_USB3CZR+USB3_IH_R/2,_USB3CZR+USB3_OH_R/2)
            _sw_box(bm_io3_usb_r,USB3_CX_R-USB3_OW_R/2,USB3_CX_R+USB3_OW_R/2,_FYR3,_FYR3+0.0008,_USB3CZR-USB3_OH_R/2,_USB3CZR-USB3_IH_R/2)
            _sw_box(bm_io3_usb_r,USB3_CX_R-USB3_OW_R/2,USB3_CX_R-USB3_IW_R/2,_FYR3,_FYR3+0.0008,_USB3CZR-USB3_OH_R/2,_USB3CZR+USB3_OH_R/2)
            _sw_box(bm_io3_usb_r,USB3_CX_R+USB3_IW_R/2,USB3_CX_R+USB3_OW_R/2,_FYR3,_FYR3+0.0008,_USB3CZR-USB3_OH_R/2,_USB3CZR+USB3_OH_R/2)
            for _wpr3 in [(USB3_CX_R-USB3_OW_R/2,USB3_CX_R+USB3_OW_R/2,_USB3CZR+USB3_IH_R/2,_USB3CZR+USB3_OH_R/2),
                          (USB3_CX_R-USB3_OW_R/2,USB3_CX_R+USB3_OW_R/2,_USB3CZR-USB3_OH_R/2,_USB3CZR-USB3_IH_R/2),
                          (USB3_CX_R-USB3_OW_R/2,USB3_CX_R-USB3_IW_R/2,_USB3CZR-USB3_OH_R/2,_USB3CZR+USB3_OH_R/2),
                          (USB3_CX_R+USB3_IW_R/2,USB3_CX_R+USB3_OW_R/2,_USB3CZR-USB3_OH_R/2,_USB3CZR+USB3_OH_R/2)]:
                _sw_box(bm_io3_usb_r,_wpr3[0],_wpr3[1],_BYR3,_FYR3,_wpr3[2],_wpr3[3])
            _sw_box(bm_io3_usb_r,USB3_CX_R-USB3_OW_R/2,USB3_CX_R+USB3_OW_R/2,_BYR3,_BYR3+0.0005,_USB3CZR-USB3_OH_R/2,_USB3CZR+USB3_OH_R/2)
            _sw_box(bm_io3_usb_r,USB3_CX_R-USB3_IW_R/2+0.001,USB3_CX_R+USB3_IW_R/2-0.001,_BYR3+0.001,_FYR3-0.002,_USB3CZR,_USB3CZR+USB3_IH_R/2-0.0003)
        bm_io3_misc=bmesh.new()
        _sw_box(bm_io3_misc,_io3_x0+0.090,_io3_x0+0.108,BACK_Y_3+0.001,BACK_Y_3+0.004,HH_3*0.25-0.0075,HH_3*0.25+0.0075)
        _sw_box(bm_io3_misc,_io3_x0+0.090,_io3_x0+0.108,BACK_Y_3+0.001,BACK_Y_3+0.004,-HH_3*0.58-0.005,-HH_3*0.58+0.005)
        parts.append(_sw_mesh_obj(f"{name}_usb_rear",     bm_io3_usb_r, col, 'M_PlasticDark'))
        parts.append(_sw_mesh_obj(f"{name}_rear_io_misc", bm_io3_misc,  col, 'M_DarkGrayMet'))

        # Rear background panel
        bm_r3bg=bmesh.new()
        def _rbg3(x0,x1,z0,z1): _sw_box(bm_r3bg,x0,x1,BACK_Y_3-0.002,BACK_Y_3,z0,z1)
        # PCIe + fan zone: solid strip
        _rbg3(_pcie3_x0, _psu3_x0L, -HH_3, HH_3)
        # IO zone left margin + right gap strip
        _rbg3(-HW_3, _io3_x0+0.004, -HH_3, HH_3)
        _rbg3(_io3_x0+_io3_w, _pcie3_x0, -HH_3, HH_3)
        # IO zone: tile around RJ45 openings (column-based to avoid Z overlap)
        _RJ3_IHW = RJ3_IW / 2;  _RJ3_IHH = RJ3_IH / 2
        _RJ3_CZ  = HH_3 * 0.40
        _RJ3_Z0  = _RJ3_CZ - _RJ3_IHH;  _RJ3_Z1 = _RJ3_CZ + _RJ3_IHH
        for _rjcx in [_io3_x0+0.012, _io3_x0+0.030, _io3_x0+0.052]:
            _rx0 = _rjcx - _RJ3_IHW;  _rx1 = _rjcx + _RJ3_IHW
            _rbg3(_rx0, _rx1, -HH_3, _RJ3_Z0)   # below RJ45 hole
            _rbg3(_rx0, _rx1, _RJ3_Z1, HH_3)    # above RJ45 hole
        # Solid columns between and around RJ45 ports
        _rbg3(_io3_x0+0.004,                  _io3_x0+0.012-_RJ3_IHW, -HH_3, HH_3)
        _rbg3(_io3_x0+0.012+_RJ3_IHW, _io3_x0+0.030-_RJ3_IHW, -HH_3, HH_3)
        _rbg3(_io3_x0+0.030+_RJ3_IHW, _io3_x0+0.052-_RJ3_IHW, -HH_3, HH_3)
        _rbg3(_io3_x0+0.052+_RJ3_IHW, USB3_CX_R-USB3_IW_R/2,  -HH_3, HH_3)
        # USB column: tile around two holes at different Z
        _USBRX3_X0 = USB3_CX_R - USB3_IW_R/2;  _USBRX3_X1 = USB3_CX_R + USB3_IW_R/2
        _USB3R_1Z0 = -HH_3*0.15 - USB3_IH_R/2;  _USB3R_1Z1 = -HH_3*0.15 + USB3_IH_R/2
        _USB3R_2Z0 = -HH_3*0.42 - USB3_IH_R/2;  _USB3R_2Z1 = -HH_3*0.42 + USB3_IH_R/2
        _rbg3(_USBRX3_X0, _USBRX3_X1, -HH_3,          _USB3R_2Z0)
        _rbg3(_USBRX3_X0, _USBRX3_X1, _USB3R_2Z1, _USB3R_1Z0)
        _rbg3(_USBRX3_X0, _USBRX3_X1, _USB3R_1Z1, HH_3)
        # VGA+DB9 zone → right edge of IO zone
        _rbg3(_USBRX3_X1, _io3_x0+_io3_w, -HH_3, HH_3)
        # PSU separators and right edge
        _rbg3(_psu3_x1L,_psu3_x0R,-HH_3,HH_3)
        _rbg3(_psu3_x1R,HW_3,-HH_3,HH_3)
        for _pbx0_3,_pbx1_3 in [(_psu3_x0L,_psu3_x1L),(_psu3_x0R,_psu3_x1R)]:
            _pcx3=(_pbx0_3+_pbx1_3)/2
            _bgiz0_3=_IEC3_CZ-IEC_CUT_H_3/2; _bgiz1_3=_IEC3_CZ+IEC_CUT_H_3/2
            _bgix0_3=_pcx3-IEC_CUT_W_3/2;    _bgix1_3=_pcx3+IEC_CUT_W_3/2
            _rbg3(_pbx0_3,_bgix0_3,-HH_3,HH_3); _rbg3(_bgix1_3,_pbx1_3,-HH_3,HH_3)
            _rbg3(_bgix0_3,_bgix1_3,-HH_3,_bgiz0_3); _rbg3(_bgix0_3,_bgix1_3,_bgiz1_3,HH_3)
        parts.append(_sw_mesh_obj(f"{name}_rear_panel_bg",bm_r3bg,col,'M_Aluminum'))

        # ── Translation: centred → equipment-origin ───────────────────────
        tx, ty, tz = 0.0, d / 2, h / 2
        for obj in parts[1:]:
            me = obj.data
            for v in me.vertices:
                v.co.x += tx; v.co.y += ty; v.co.z += tz
            me.update()
            obj.hide_render = False
        parts[0].hide_render = False

        # ── Mounting ears + M6 screws (equipment-origin space) ────────────
        ear_w_3u = (EIA_RAIL_SPAN_M - EIA_EQUIPMENT_BODY_M) / 2
        ear_d_3u = 0.002; ear_h_3u = h * 0.68
        for side_sign in (-1, 1):
            side_label = 'L' if side_sign < 0 else 'R'
            ear_cx_3u = side_sign * (w / 2 + ear_w_3u / 2)
            bm_ear3 = bmesh.new()
            _sw_box(bm_ear3,
                    ear_cx_3u - ear_w_3u/2, ear_cx_3u + ear_w_3u/2,
                    -ear_d_3u, 0.0,
                    (h - ear_h_3u)/2, (h + ear_h_3u)/2)
            parts.append(_sw_mesh_obj(f"{name}_ear_{side_label}", bm_ear3, col, 'M_Aluminum'))
            SCR3_R_E=0.0038; SCR3_T_E=0.0028; SCR3_Y_E=-(ear_d_3u+0.001); SCR3_Z_E=h/2
            bm_scr3e=bmesh.new(); fv3e=[]; bv3e=[]
            for _i3e in range(8):
                _a3e=math.pi/8+2*math.pi*_i3e/8
                fv3e.append(bm_scr3e.verts.new((ear_cx_3u+SCR3_R_E*math.cos(_a3e),SCR3_Y_E,           SCR3_Z_E+SCR3_R_E*math.sin(_a3e))))
                bv3e.append(bm_scr3e.verts.new((ear_cx_3u+SCR3_R_E*math.cos(_a3e),SCR3_Y_E+SCR3_T_E,  SCR3_Z_E+SCR3_R_E*math.sin(_a3e))))
            cf3e=bm_scr3e.verts.new((ear_cx_3u,SCR3_Y_E,           SCR3_Z_E))
            cb3e=bm_scr3e.verts.new((ear_cx_3u,SCR3_Y_E+SCR3_T_E,  SCR3_Z_E))
            for _i3e in range(8):
                _n3e=(_i3e+1)%8
                _sw_F(bm_scr3e,[fv3e[_i3e],fv3e[_n3e],bv3e[_n3e],bv3e[_i3e]])
                try: bm_scr3e.faces.new([cf3e,fv3e[_n3e],fv3e[_i3e]])
                except: pass
                try: bm_scr3e.faces.new([cb3e,bv3e[_i3e],bv3e[_n3e]])
                except: pass
            GRV3=0.0006; GRL3=SCR3_R_E*1.6
            _sw_box(bm_scr3e,ear_cx_3u-GRL3/2,ear_cx_3u+GRL3/2,SCR3_Y_E-0.0003,SCR3_Y_E,SCR3_Z_E-GRV3/2,SCR3_Z_E+GRV3/2)
            _sw_box(bm_scr3e,ear_cx_3u-GRV3/2,ear_cx_3u+GRV3/2,SCR3_Y_E-0.0003,SCR3_Y_E,SCR3_Z_E-GRL3/2,SCR3_Z_E+GRL3/2)
            parts.append(_sw_mesh_obj(f"{name}_ear_screw_{side_label}", bm_scr3e, col, 'M_DarkGrayMet'))

    elif u_size <= 3:
        # ── 3U front face ─────────────────────────────────────────────────
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

    # ── Mounting ears — 2U+ only (1U builds its own hero ears above) ─────────
    # Total panel = 482.6 mm; body = 446 mm; each ear = (482.6 - 446) / 2 = 18.3 mm
    ear_w = (EIA_RAIL_SPAN_M - EIA_EQUIPMENT_BODY_M) / 2   # 18.3 mm
    ear_d = 0.002   # 2 mm deep
    ear_h = h * 0.68

    for side_sign in (-1, 1) if u_size != 1 else ():
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

    if qf["vents"] and u_size != 1:
        # ── Side ventilation slots (horizontal louvre strips) — 2U+ only ──
        # 1U hero model builds its own louvers in the centred-coord block above.
        _base_vents = 6
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

    if u_size > 3 and qf["bezel"]:
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

    # ── Hero: chamfer strips at key chassis edges (proper_bevels) — 2U+ ─────
    if qf.get("proper_bevels") and u_size != 1:
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

    # ── Join + origin ─────────────────────────────────────────────────────
    # 1U handles join inside its own block (join already done when join_mesh=True).
    # 2U+ respect join_mesh here: True → merge + fix normals, False → keep parts.
    if join_mesh:
        if u_size == 1:
            joined = parts[0] if parts else None   # already joined above
        else:
            joined = _join_parts(parts, name)
            bpy.ops.object.select_all(action='DESELECT')
            joined.select_set(True)
            bpy.context.view_layer.objects.active = joined
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.normals_make_consistent(inside=False)
            bpy.ops.object.mode_set(mode='OBJECT')
    else:
        joined = parts[0] if parts else None

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
    PW      = 0.01200               # port pitch width (layout spacing)
    OW, OH  = 0.01200, 0.01180      # outer shell W / H = PW (fits within pitch)
    OD      = 0.01600
    WALL    = 0.00140
    IW      = OW - 2 * WALL        # 0.00920
    IH      = OH - 2 * WALL        # 0.00900
    CHAM    = 0.00048
    PORT_PROTRUDE = 0.00150
    PORT_FRONT_Y  = FRONT_Y - PORT_PROTRUDE   # -0.14150

    GAP_X   = 0.00115
    GRP_GAP = 0.00460
    G_SIZE  = 6
    N_GROUPS = 4 if port_count >= 48 else 2
    PORT_ZONE_CX = 0.0115
    single_grp_w  = G_SIZE * PW + (G_SIZE - 1) * GAP_X   # uses PW not OW
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
    FAN_CZ       = -0.00040              # shifted 2mm down to clear chassis top
    FAN_DUCT_D   = 0.02800
    FAN_BACK_Y   = BACK_Y + 0.002        # 2mm proud of back plate

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
    _quad((-HW, FRONT_Y, HH), ( HW, FRONT_Y,  HH), ( HW, BACK_Y,  HH), (-HW, BACK_Y,  HH))  # top
    _quad((-HW, FRONT_Y,-HH), (-HW, BACK_Y,  -HH), ( HW, BACK_Y, -HH), ( HW, FRONT_Y, -HH)) # bottom
    _quad((-HW, FRONT_Y,-HH), (-HW, FRONT_Y,  HH), (-HW, BACK_Y,  HH), (-HW, BACK_Y,  -HH)) # left
    _quad(( HW, FRONT_Y,-HH), ( HW, BACK_Y,  -HH), ( HW, BACK_Y,  HH), ( HW, FRONT_Y,  HH)) # right
    parts.append(_sw_mesh_obj(f"{name}_chassis", bm_ch, col, 'M_Aluminum'))

    # ── BACK PLATE: aluminium with cutouts for fans, IEC C14, rear RJ45 ports ──
    bp_rect = [
        # IEC C14 cutout
        (IEC_CX - IEC_CUT_W/2, IEC_CX + IEC_CUT_W/2,
         IEC_CZ - IEC_CUT_H/2, IEC_CZ + IEC_CUT_H/2),
        # Rear RJ45 — console
        (REAR_PORTS[0]['cx'] - REAR_OW/2, REAR_PORTS[0]['cx'] + REAR_OW/2,
         REAR_PORTS[0]['cz'] - REAR_OH/2, REAR_PORTS[0]['cz'] + REAR_OH/2),
        # Rear RJ45 — management
        (REAR_PORTS[1]['cx'] - REAR_OW/2, REAR_PORTS[1]['cx'] + REAR_OW/2,
         REAR_PORTS[1]['cz'] - REAR_OH/2, REAR_PORTS[1]['cz'] + REAR_OH/2),
    ]
    bp_circ = [
        (FAN1_CX, FAN_CZ, FAN_HOLE_R),
        (FAN2_CX, FAN_CZ, FAN_HOLE_R),
    ]
    parts.append(_sw_holey_plate(
        f"{name}_back_plate", BACK_Y,
        bp_rect, bp_circ,
        col, 'M_Aluminum',
        x_min=-HW, x_max=HW, z_min=-HH, z_max=HH,
        outward_plus_y=True,
    ))

    # ─────────────────────────────────────────────────────────────────────
    # FRONT PLATE: aluminium plate with 48 RJ45 + 4 SFP+ holes
    # ─────────────────────────────────────────────────────────────────────
    fp_holes = []
    for g in range(N_GROUPS):
        gx = port_left_edge + g * (single_grp_w + GRP_GAP)
        for p in range(G_SIZE):
            cx = gx + p * (PW + GAP_X) + PW / 2   # centre using PW pitch
            x0 = cx - OW / 2                        # hole sized to OW
            x1 = cx + OW / 2
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
            px = gx + p * (PW + GAP_X) + PW / 2   # centre using PW pitch
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
                # 8 gold contact pins — inside cavity, in front of ib face
                N_PINS = 8
                pin_y0 = py1 - WALL - 0.0012   # 1.2mm inside cavity
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
    LED_W, LED_H_dim, LED_D_dim = 0.00110, 0.00200, 0.00100  # narrower for 2-per-port
    LED_GAP   = 0.00060                                        # gap between the two LEDs
    LED_Z_OFFSET = OH / 2 + 0.00250
    # offsets so the pair is centred on the port: -(LED_W/2 + LED_GAP/2) and +(...)
    LED_X_OFFSETS = [-(LED_W / 2 + LED_GAP / 2), (LED_W / 2 + LED_GAP / 2)]
    for g in range(N_GROUPS):
        gx = port_left_edge + g * (single_grp_w + GRP_GAP)
        for p in range(G_SIZE):
            px = gx + p * (PW + GAP_X) + PW / 2   # PW pitch
            for pz in [Z_UPPER, Z_LOWER]:
                lz = pz + LED_Z_OFFSET
                ly = PORT_FRONT_Y - 0.0002
                # left LED: link/activity (green or off)
                r0 = _rng.random()
                mat_left = 'M_LED_Green' if r0 < 0.70 else 'M_LED_Off'
                led_groups[mat_left].append((px + LED_X_OFFSETS[0], ly, lz))
                # right LED: speed/POE indicator (amber or off)
                r1 = _rng.random()
                mat_right = 'M_LED_Amber' if r1 < 0.45 else 'M_LED_Off'
                led_groups[mat_right].append((px + LED_X_OFFSETS[1], ly, lz))
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

    # SFP bails — small retention clips on front lip of each cage
    BAIL_T = 0.00120; BAIL_H = 0.00400; BAIL_D = 0.00300
    bm_bail = bmesh.new()
    for x0, x1, z0, z1 in SFP_CAGES_DEF:
        cx_sfp = (x0 + x1) / 2
        cz_top = z1
        # horizontal tab across top of cage mouth
        _sw_box(bm_bail, x0 + 0.001, x1 - 0.001,
                SFP_MOUTH_Y - BAIL_D, SFP_MOUTH_Y,
                cz_top, cz_top + BAIL_T)
        # small pull-tab finger loop
        _sw_box(bm_bail, cx_sfp - 0.003, cx_sfp + 0.003,
                SFP_MOUTH_Y - BAIL_D, SFP_MOUTH_Y - BAIL_D + BAIL_T,
                cz_top + BAIL_T, cz_top + BAIL_T + BAIL_H)
    parts.append(_sw_mesh_obj(f"{name}_sfp_bails", bm_bail, col, 'M_Black'))

    # ─────────────────────────────────────────────────────────────────────
    # FANS: shroud ring + 9 swept blades + hub cylinder + interior duct box
    # ─────────────────────────────────────────────────────────────────────
    for fi, fcx in enumerate([FAN1_CX, FAN2_CX], 1):
        suffix = f"_{fi}"
        N_BLADES = 9
        # ── Shroud ring ──
        bm_fan = bmesh.new()
        N_RING = 48; SR = FAN_SHROUD_R; ST = 0.0025
        outer_f_r = []; outer_b_r = []
        for i in range(N_RING):
            a = 2 * math.pi * i / N_RING
            outer_f_r.append(bm_fan.verts.new((fcx + SR*math.cos(a), FAN_BACK_Y,        FAN_CZ + SR*math.sin(a))))
            outer_b_r.append(bm_fan.verts.new((fcx + SR*math.cos(a), FAN_BACK_Y - ST*3, FAN_CZ + SR*math.sin(a))))
        IR = SR - 0.0035
        inner_f_r = []; inner_b_r = []
        for i in range(N_RING):
            a = 2 * math.pi * i / N_RING
            inner_f_r.append(bm_fan.verts.new((fcx + IR*math.cos(a), FAN_BACK_Y,        FAN_CZ + IR*math.sin(a))))
            inner_b_r.append(bm_fan.verts.new((fcx + IR*math.cos(a), FAN_BACK_Y - ST*3, FAN_CZ + IR*math.sin(a))))
        for i in range(N_RING):
            n = (i + 1) % N_RING
            _sw_F(bm_fan, [outer_f_r[i], outer_f_r[n], outer_b_r[n], outer_b_r[i]])
            _sw_F(bm_fan, [inner_f_r[i], inner_b_r[i], inner_b_r[n], inner_f_r[n]])
            _sw_F(bm_fan, [outer_f_r[i], inner_f_r[i], inner_f_r[n], outer_f_r[n]])
            _sw_F(bm_fan, [outer_b_r[i], outer_b_r[n], inner_b_r[n], inner_b_r[i]])
        parts.append(_sw_mesh_obj(f"{name}_fan_shroud{suffix}", bm_fan, col, 'M_Black'))

        # ── Blades: 9 thin swept blades ──
        bm_bl = bmesh.new()
        BLADE_R_IN = 0.005; BLADE_R_OUT = IR - 0.001; PITCH = 0.005
        for b_i in range(N_BLADES):
            angle_base = 2 * math.pi * b_i / N_BLADES
            angle_tip  = angle_base + 0.35          # moderate sweep
            y_in_f  = FAN_BACK_Y - 0.002
            y_out_f = FAN_BACK_Y - 0.002 + PITCH
            y_in_b  = y_in_f  - 0.0015
            y_out_b = y_out_f - 0.0015
            bl_w = 0.0015                            # thin blades
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
        parts.append(_sw_mesh_obj(f"{name}_fan_blades{suffix}", bm_bl, col, 'M_DarkGrayMet'))

        # ── Hub ──
        bm_hub = bmesh.new()
        HR = 0.0055; HY0 = FAN_BACK_Y - 0.002; HY1 = FAN_BACK_Y + 0.003
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

        # ── Duct box: pushed inside chassis — blocks line-of-sight to front ──
        bm_duct = bmesh.new()
        DZ0 = max(FAN_CZ - FAN_SHROUD_R, -HH + 0.001)
        DZ1 = min(FAN_CZ + FAN_SHROUD_R,  HH - 0.001)
        DX0 = fcx - FAN_SHROUD_R; DX1 = fcx + FAN_SHROUD_R
        DY0 = BACK_Y - 0.001                         # just inside back plate
        DY1 = DY0 - FAN_DUCT_D
        oo_d = [bm_duct.verts.new((DX0, DY0, DZ0)), bm_duct.verts.new((DX1, DY0, DZ0)),
                bm_duct.verts.new((DX1, DY0, DZ1)), bm_duct.verts.new((DX0, DY0, DZ1))]
        ii_d = [bm_duct.verts.new((DX0, DY1, DZ0)), bm_duct.verts.new((DX1, DY1, DZ0)),
                bm_duct.verts.new((DX1, DY1, DZ1)), bm_duct.verts.new((DX0, DY1, DZ1))]
        _sw_F(bm_duct, [ii_d[0], ii_d[1], ii_d[2], ii_d[3]])  # back face (solid)
        _sw_F(bm_duct, [oo_d[0], ii_d[0], ii_d[3], oo_d[3]])  # left wall
        _sw_F(bm_duct, [oo_d[1], oo_d[2], ii_d[2], ii_d[1]])  # right wall
        _sw_F(bm_duct, [oo_d[0], oo_d[1], ii_d[1], ii_d[0]])  # bottom wall
        _sw_F(bm_duct, [oo_d[3], ii_d[3], ii_d[2], oo_d[2]])  # top wall
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
    DX0 = -0.2060; DX1 = -0.1784; DZ0 = -0.0100; DZ1 = 0.0118
    PY_disp = FRONT_Y - 0.0015
    bm_disp_bz = bmesh.new()
    _sw_box(bm_disp_bz, DX0 - 0.003, DX1 + 0.003,
            PY_disp + 0.0005, PY_disp + 0.004,
            DZ0 - 0.003, DZ1 + 0.003)
    parts.append(_sw_mesh_obj(f"{name}_display_bezel", bm_disp_bz, col, 'M_Black'))
    bm_disp_sc = bmesh.new()
    _sw_box(bm_disp_sc, DX0, DX1, PY_disp, PY_disp + 0.0015, DZ0, DZ1)
    parts.append(_sw_mesh_obj(f"{name}_display_screen", bm_disp_sc, col, 'M_Display'))

    # Status indicator LEDs (SYS / PWR / POE / ALT) — 4 small squares right of display
    # Status LEDs at X≈-0.210 (far-left front panel, near logo area per manifest)
    SLED_Y = FRONT_Y - 0.0003
    SLED_W = 0.0023; SLED_H = 0.0016; SLED_D = 0.0008
    SLED_X = -0.2100
    sled_defs = [
        ('pwr', SLED_X,  0.0121, 'M_LED_Green'),
        ('sys', SLED_X,  0.0046, 'M_LED_White'),
        ('alt', SLED_X, -0.0029, 'M_LED_Off'),
        ('poe', SLED_X, -0.0104, 'M_LED_Amber'),
    ]
    for sled_label, sx, sz, smat in sled_defs:
        bm_sl = bmesh.new()
        _sw_box(bm_sl, sx - SLED_W/2, sx + SLED_W/2, SLED_Y - SLED_D, SLED_Y,
                sz - SLED_H/2, sz + SLED_H/2)
        parts.append(_sw_mesh_obj(f"{name}_sled_{sled_label}", bm_sl, col, smat))

    # ─────────────────────────────────────────────────────────────────────
    # TOP LOUVERS — horizontal slats spanning full width (L→R), toward rear of chassis
    # 0.2 mm proud of chassis top (barely peeking through), 2 mm deep, 4 mm pitch
    # ─────────────────────────────────────────────────────────────────────
    LOUVER_D = 0.0020; LOUVER_GAP = 0.0040; LOUVER_H = 0.0002
    N_LOUVERS = 20; START_Y_L = 0.010
    bm_louv = bmesh.new()
    for i in range(N_LOUVERS):
        ly = START_Y_L + i * (LOUVER_D + LOUVER_GAP)
        _sw_box(bm_louv, -HW + 0.005, HW - 0.005, ly, ly + LOUVER_D, HH - 0.0005, HH + LOUVER_H)
    parts.append(_sw_mesh_obj(f"{name}_top_louvers", bm_louv, col, 'M_DarkGrayMet'))

    # ─────────────────────────────────────────────────────────────────────
    # SIDE VENTS — thin horizontal slits on chassis side walls
    # Each slit: 1 mm tall in Z, runs Y depth, stacked and centred on mid-height
    # ─────────────────────────────────────────────────────────────────────
    VENT_SLOT_H = 0.0010; VENT_SLOT_GAP = 0.0020; N_VENTS = 10
    VENT_Y0 = -0.040; VENT_Y1 = 0.040
    total_vent_span = N_VENTS * VENT_SLOT_H + (N_VENTS - 1) * VENT_SLOT_GAP
    start_z_v = -total_vent_span / 2  # centred on Z=0
    for x_pos in [HW, -HW]:
        bm_vent = bmesh.new()
        for i in range(N_VENTS):
            vz = start_z_v + i * (VENT_SLOT_H + VENT_SLOT_GAP)
            x0_v = x_pos - 0.001  if x_pos > 0 else x_pos - 0.0002
            x1_v = x_pos + 0.0002 if x_pos > 0 else x_pos + 0.001
            _sw_box(bm_vent, x0_v, x1_v, VENT_Y0, VENT_Y1, vz, vz + VENT_SLOT_H)
        side_label = 'R' if x_pos > 0 else 'L'
        parts.append(_sw_mesh_obj(f"{name}_side_vents_{side_label}", bm_vent, col, 'M_Black'))

    # ─────────────────────────────────────────────────────────────────────
    # PORT LABELS — numbers printed in the top wall of each port housing face
    # Sits in the WALL-height strip at the very top of each housing opening
    # SFP+ labelled 1–4, placed on the bail pull-tab (top of bail)
    # ─────────────────────────────────────────────────────────────────────
    LABEL_Y    = PORT_FRONT_Y - 0.0002    # just proud of the housing face
    LABEL_SIZE = 0.0009                    # 0.9mm — fits inside WALL=1.4mm
    LABEL_EXT  = 0.00012                   # 0.12mm extrusion
    # Top wall strip center of each housing row
    LBL_Z_UP   = Z_UPPER + OH / 2 - WALL / 2   # inside top wall of upper housing
    LBL_Z_LO   = Z_LOWER + OH / 2 - WALL / 2   # inside top wall of lower housing

    _lbl_objs = []

    def _add_lbl(text_str: str, lx: float, lz: float, ly: float = None) -> None:
        fc = bpy.data.curves.new("_sw_lbl_fc", type='FONT')
        fc.body = text_str
        fc.size = LABEL_SIZE
        fc.extrude = LABEL_EXT
        fc.align_x = 'CENTER'
        fc.align_y = 'CENTER'
        o = bpy.data.objects.new("_sw_lbl_obj", fc)
        bpy.context.scene.collection.objects.link(o)
        o.rotation_euler = (math.pi / 2, 0, 0)   # face toward -Y (front viewer)
        o.location = (lx, LABEL_Y if ly is None else ly, lz)
        _lbl_objs.append(o)

    port_num = 1
    for g in range(N_GROUPS):
        gx = port_left_edge + g * (single_grp_w + GRP_GAP)
        for p in range(G_SIZE):
            cx = gx + p * (PW + GAP_X) + PW / 2
            _add_lbl(str(port_num),     cx, LBL_Z_UP)
            _add_lbl(str(port_num + 1), cx, LBL_Z_LO)
            port_num += 2

    SFP_LBL_Y = SFP_MOUTH_Y - BAIL_D - 0.0002   # just proud of pull-tab front face
    for ci_sfp, (sx0, sx1, sz0, sz1) in enumerate(SFP_CAGES_DEF, 1):
        sfp_lbl_z = sz1 + BAIL_T + BAIL_H / 2   # centre of pull-tab vertically
        _add_lbl(str(ci_sfp), (sx0 + sx1) / 2, sfp_lbl_z, ly=SFP_LBL_Y)

    if _lbl_objs:
        bpy.context.view_layer.update()
        dep = bpy.context.evaluated_depsgraph_get()
        bm_lbl = bmesh.new()
        for fo in _lbl_objs:
            me_tmp = bpy.data.meshes.new_from_object(fo.evaluated_get(dep))
            bm_t = bmesh.new()
            bm_t.from_mesh(me_tmp)
            bmesh.ops.transform(bm_t, matrix=fo.matrix_world, verts=bm_t.verts[:])
            nv = [bm_lbl.verts.new(v.co) for v in bm_t.verts]
            bm_lbl.verts.ensure_lookup_table()
            bm_t.verts.ensure_lookup_table()
            bm_t.faces.ensure_lookup_table()
            for f in bm_t.faces:
                try: bm_lbl.faces.new([nv[v.index] for v in f.verts])
                except: pass
            bm_t.free()
            bpy.data.meshes.remove(me_tmp)
            fc_data = fo.data
            bpy.data.objects.remove(fo)
            bpy.data.curves.remove(fc_data)
        parts.append(_sw_mesh_obj(f"{name}_port_labels", bm_lbl, col, 'M_White'))

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

        # M6 rack screw — 8-sided cap head + Phillips cross, centered on ear
        SCR_R   = 0.0038              # 7.6mm diam head (M6 pan head)
        SCR_T   = 0.0028              # 2.8mm head thickness
        SCR_Y   = -(ear_d + 0.0010)  # 1mm proud of outer ear face (Y = -ear_d)
        SCR_Z   = h / 2              # vertically centred on chassis
        SCR_SEG = 8
        bm_scr  = bmesh.new()
        fv = []; bv = []
        for i in range(SCR_SEG):
            a = math.pi / SCR_SEG + 2 * math.pi * i / SCR_SEG  # flat-top orientation
            fv.append(bm_scr.verts.new((ear_cx + SCR_R*math.cos(a), SCR_Y,         SCR_Z + SCR_R*math.sin(a))))
            bv.append(bm_scr.verts.new((ear_cx + SCR_R*math.cos(a), SCR_Y + SCR_T, SCR_Z + SCR_R*math.sin(a))))
        cf = bm_scr.verts.new((ear_cx, SCR_Y,         SCR_Z))
        cb = bm_scr.verts.new((ear_cx, SCR_Y + SCR_T, SCR_Z))
        for i in range(SCR_SEG):
            n = (i + 1) % SCR_SEG
            _sw_F(bm_scr, [fv[i], fv[n], bv[n], bv[i]])
            try: bm_scr.faces.new([cf, fv[n], fv[i]])
            except: pass
            try: bm_scr.faces.new([cb, bv[i], bv[n]])
            except: pass
        # Phillips cross grooves (two thin raised bars on outer face)
        GRV = 0.0006; GRL = SCR_R * 1.6
        _sw_box(bm_scr, ear_cx - GRL/2, ear_cx + GRL/2, SCR_Y - 0.0003, SCR_Y, SCR_Z - GRV/2, SCR_Z + GRV/2)
        _sw_box(bm_scr, ear_cx - GRV/2, ear_cx + GRV/2, SCR_Y - 0.0003, SCR_Y, SCR_Z - GRL/2, SCR_Z + GRL/2)
        parts.append(_sw_mesh_obj(f"{name}_ear_screw_{side_label}", bm_scr, col, 'M_DarkGrayMet'))

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
    join_mesh: bool = False,
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
    join_mesh:        when True join all parts into one mesh; False keeps parts separate (default)
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
        C13 outlet: open-frame bezel + dark socket face + optional 3-pin stubs.
        portrait=True  → taller than wide  (0U column layout)
        portrait=False → wider than tall   (1U row layout)
        The bezel is 4 border bars (not a solid box) so the socket face is visible.
        """
        hw, hh = (0.034, 0.038) if portrait else (0.038, 0.030)
        fr  = 0.002   # frame rail width (2 mm)
        fd  = 0.0020  # frame depth (2 mm proud)
        # 4-bar open frame — leaves the socket opening clear
        _bm_fr = bmesh.new()
        _sw_box(_bm_fr, cx-hw/2-fr, cx+hw/2+fr, fy-fd, fy, cz+hh/2,     cz+hh/2+fr)  # top
        _sw_box(_bm_fr, cx-hw/2-fr, cx+hw/2+fr, fy-fd, fy, cz-hh/2-fr,  cz-hh/2)     # bottom
        _sw_box(_bm_fr, cx-hw/2-fr, cx-hw/2,    fy-fd, fy, cz-hh/2,     cz+hh/2)     # left
        _sw_box(_bm_fr, cx+hw/2,    cx+hw/2+fr, fy-fd, fy, cz-hh/2,     cz+hh/2)     # right
        parts.append(_sw_mesh_obj(f"{tag}_frame", _bm_fr, col, 'M_DarkGrayMet'))
        # Dark socket face — slightly proud of body, visible through frame opening
        parts.append(_create_box_object(f"{tag}_face",
            cx=cx, cy=fy + 0.0005, cz=cz,
            w=hw, d=0.0015, h=hh, collection=col,
            material='M_PlasticDark'))
        if qf["bay_3d"]:   # IEC 3-pin aperture stubs (gold)
            gz_off = hh * 0.28 if portrait else 0.0
            parts.append(_create_box_object(f"{tag}_gnd",
                cx=cx, cy=fy - 0.0005, cz=cz + gz_off,
                w=0.005, d=0.003, h=0.010 if portrait else 0.005,
                collection=col, material='M_Gold'))
            for sx, lbl in [(-1, "L"), (1, "N")]:
                pz_off = -hh * 0.18 if portrait else 0.0
                parts.append(_create_box_object(f"{tag}_pin{lbl}",
                    cx=cx + sx * hw * 0.30, cy=fy - 0.0005, cz=cz + pz_off,
                    w=0.004, d=0.003, h=0.010 if portrait else 0.005,
                    collection=col, material='M_Gold'))

    def _c19(tag, cx, cz, fy):
        """C19 heavy-duty outlet (portrait, 0U only)."""
        hw, hh = 0.048, 0.044
        hd     = 0.0100   # 10 mm recess
        # Outer bezel
        parts.append(_create_box_object(f"{tag}_hsg",
            cx=cx, cy=fy - 0.0010, cz=cz,
            w=hw + 0.004, d=0.0020, h=hh + 0.004, collection=col,
            material='M_DarkGrayMet'))
        # Recessed back face
        parts.append(_create_box_object(f"{tag}_face",
            cx=cx, cy=fy + hd, cz=cz,
            w=hw - 0.004, d=0.0025, h=hh - 0.004, collection=col,
            material='M_PlasticDark'))

    def _c14(tag, cx, cz, fy):
        """C14 IEC inlet (1U right end)."""
        # Outer bezel
        parts.append(_create_box_object(f"{tag}_c14_hsg",
            cx=cx, cy=fy - 0.0010, cz=cz,
            w=0.034, d=0.0020, h=0.022, collection=col,
            material='M_DarkGrayMet'))
        # Recessed back face (8 mm)
        parts.append(_create_box_object(f"{tag}_c14_face",
            cx=cx, cy=fy + 0.0080, cz=cz,
            w=0.026, d=0.0025, h=0.014, collection=col,
            material='M_PlasticDark'))

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

        # ── Hero: side-channel grooves (extruded aluminium profile) ─────────
        if qf["bezel"]:
            for _sx, _slbl in [(-1, 'L'), (1, 'R')]:
                _ch_cx = _sx * (w_pdu / 2 - 0.0045)
                parts.append(_create_box_object(f"{name}_ch_{_slbl}",
                    cx=_ch_cx, cy=d_pdu / 2, cz=(h_pdu - HEAD_H - FOOT_H) / 2 + FOOT_H,
                    w=0.003, d=d_pdu - 0.010, h=h_pdu - HEAD_H - FOOT_H - 0.008,
                    collection=col))

        # ── Hero: C20 inlet 3-pin geometry ───────────────────────────────────
        if qf["bezel"] and qf["bay_3d"]:
            _C20_CZ = h_pdu - HEAD_H / 2
            _C20_H  = 0.026
            # Ground pin (top-center, oblong vertical)
            parts.append(_create_box_object(f"{name}_c20_gnd",
                cx=0.0, cy=-0.0010, cz=_C20_CZ + _C20_H * 0.22,
                w=0.0060, d=0.0040, h=0.0130, collection=col))
            # L and N pins (bottom left/right)
            for _px, _pl in [(-0.012, 'L'), (0.012, 'N')]:
                parts.append(_create_box_object(f"{name}_c20_{_pl}",
                    cx=_px, cy=-0.0010, cz=_C20_CZ - _C20_H * 0.20,
                    w=0.0060, d=0.0040, h=0.0130, collection=col))

        # ── Hero: control section — RJ45 network port + USB console ─────────
        if qf["bezel"]:
            _RJ0_CX  =  0.008
            _RJ0_CZ  = CTRL_Z - 0.028
            _RJ0_OW  = 0.018;  _RJ0_OH = 0.016
            _RJ0_IW  = 0.0138; _RJ0_IH = 0.0092
            # RJ45 outer bezel box
            parts.append(_create_box_object(f"{name}_rj_bezel",
                cx=_RJ0_CX, cy=-0.0040, cz=_RJ0_CZ,
                w=_RJ0_OW, d=0.0080, h=_RJ0_OH, collection=col))
            # RJ45 dark inner recess + 8 gold contact pins
            _bm_rj0 = bmesh.new()
            _sw_box(_bm_rj0,
                    _RJ0_CX - _RJ0_IW / 2, _RJ0_CX + _RJ0_IW / 2,
                    -0.0015, 0.0030,
                    _RJ0_CZ - _RJ0_IH / 2, _RJ0_CZ + _RJ0_IH / 2)
            _rj0_pin_w = _RJ0_IW / 10
            for _pi0 in range(8):
                _rpx0_0 = _RJ0_CX - _RJ0_IW / 2 + _pi0 * (_RJ0_IW / 8) + _rj0_pin_w * 0.15
                _rpx0_1 = _rpx0_0 + _rj0_pin_w * 0.70
                _sw_box(_bm_rj0, _rpx0_0, _rpx0_1, -0.0008, 0.0020,
                        _RJ0_CZ - _RJ0_IH / 2 + 0.0008,
                        _RJ0_CZ - _RJ0_IH / 2 + 0.0038)
            parts.append(_sw_mesh_obj(f"{name}_rj_port", _bm_rj0, col, 'M_Gold'))
            # RJ45 activity LED
            if qf["led_emissive"]:
                parts.append(_create_box_object(f"{name}_rj_led",
                    cx=_RJ0_CX - _RJ0_OW / 2 - 0.0040, cy=-0.0048, cz=_RJ0_CZ,
                    w=0.0040, d=0.0030, h=0.0040, collection=col))
            # USB-A console port (below RJ45)
            _USB0_CX = -0.006
            _USB0_CZ = CTRL_Z - 0.044
            _USB0_IW = 0.0126; _USB0_IH = 0.0046
            parts.append(_create_box_object(f"{name}_usb_hsg",
                cx=_USB0_CX, cy=-0.0040, cz=_USB0_CZ,
                w=_USB0_IW + 0.006, d=0.0080, h=_USB0_IH * 2.2, collection=col))
            _bm_usb0 = bmesh.new()
            _sw_box(_bm_usb0,
                    _USB0_CX - _USB0_IW / 2, _USB0_CX + _USB0_IW / 2,
                    -0.0010, 0.0040,
                    _USB0_CZ - _USB0_IH / 2, _USB0_CZ + _USB0_IH / 2)
            # USB tongue divider
            _sw_box(_bm_usb0,
                    _USB0_CX - _USB0_IW / 2 + 0.001, _USB0_CX + _USB0_IW / 2 - 0.001,
                    -0.0005, 0.0030,
                    _USB0_CZ - 0.0003, _USB0_CZ + 0.0003)
            parts.append(_sw_mesh_obj(f"{name}_usb_port", _bm_usb0, col, 'M_PlasticDark'))

        # ── Hero: per-outlet status LED domes ────────────────────────────────
        if qf["led_emissive"]:
            _bm_oleds0 = bmesh.new()
            for _oz0 in outlet_positions:
                _led_cx0 = w_pdu * 0.34
                _sw_box(_bm_oleds0,
                        _led_cx0 - 0.0022, _led_cx0 + 0.0022,
                        fy_0u - 0.0022, fy_0u,
                        _oz0 - 0.0022, _oz0 + 0.0022)
            parts.append(_sw_mesh_obj(f"{name}_outlet_leds", _bm_oleds0, col, 'M_LED_Green'))

        # ── Hero: rear keyhole mounting bracket with slots ───────────────────
        if qf.get("detailed_rear", False):
            for _ti0, _tab_z0 in enumerate([h_pdu * 0.10, h_pdu * 0.50, h_pdu * 0.90]):
                # Wide keyhole head
                parts.append(_create_box_object(f"{name}_kh_head_{_ti0}",
                    cx=0.0, cy=d_pdu + 0.007, cz=_tab_z0 + 0.007,
                    w=0.020, d=0.014, h=0.014, collection=col))
                # Narrow slot
                parts.append(_create_box_object(f"{name}_kh_slot_{_ti0}",
                    cx=0.0, cy=d_pdu + 0.007, cz=_tab_z0 - 0.006,
                    w=0.007, d=0.014, h=0.014, collection=col))

        if join_mesh:
            joined = _join_parts(parts, name)
        else:
            joined = parts[0]

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

        # C13 outlets — build first so outlet_xs is populated before tiling bg
        outlet_xs: List[float] = []
        for i in range(n_outlets):
            ox = out_x0 + i * out_step + out_step / 2
            ox = _jitter(ox, 0.001, rv)
            outlet_xs.append(ox)
            if qf["server_bays"]:
                _c13(f"{name}_c13_{i:02d}", cx=ox, cz=out_z,
                     fy=fy_1u, portrait=False)

        # Recessed outlet zone background — tiled AROUND each outlet aperture
        # (solid plate would block the recessed socket faces)
        # Landscape C13 inner aperture: 0.034 wide × 0.026 tall → half = 0.017 / 0.013
        if qf["server_bays"]:
            _AP_HW = 0.021   # housing outer half-width  (hw=0.038 + 2×fr=0.004)/2
            _AP_HH = 0.017   # housing outer half-height (hh=0.030 + 2×fr=0.004)/2
            _BG_Z0 = out_z - h_pdu * 0.40
            _BG_Z1 = out_z + h_pdu * 0.40
            _BG_X0 = out_x0
            _BG_X1 = out_x0 + OUT_ZONE
            _AP_Z0 = out_z - _AP_HH
            _AP_Z1 = out_z + _AP_HH
            _BG_CY = 0.0010;  _BG_D = 0.002

            def _rbg1u(x0, x1, z0, z1, idx):
                if x1 - x0 < 0.0001 or z1 - z0 < 0.0001:
                    return
                parts.append(_create_box_object(f"{name}_out_bg_{idx}",
                    cx=(x0+x1)/2, cy=_BG_CY, cz=(z0+z1)/2,
                    w=x1-x0, d=_BG_D, h=z1-z0, collection=col))

            _idx_bg = [0]
            def _bg(x0, x1, z0, z1):
                _rbg1u(x0, x1, z0, z1, _idx_bg[0]); _idx_bg[0] += 1

            # Top and bottom horizontal strips (full outlet zone width)
            _bg(_BG_X0, _BG_X1, _AP_Z1, _BG_Z1)
            _bg(_BG_X0, _BG_X1, _BG_Z0, _AP_Z0)
            # Left margin (before first outlet aperture)
            _bg(_BG_X0, outlet_xs[0] - _AP_HW, _AP_Z0, _AP_Z1)
            # Right margin (after last outlet aperture)
            _bg(outlet_xs[-1] + _AP_HW, _BG_X1, _AP_Z0, _AP_Z1)
            # Inter-outlet columns
            for _ii in range(len(outlet_xs) - 1):
                _bg(outlet_xs[_ii] + _AP_HW, outlet_xs[_ii + 1] - _AP_HW, _AP_Z0, _AP_Z1)

        # C14 inlet on right end
        inlet_cx = w_pdu / 2 - INLET_W / 2 - R_MARGIN
        if qf["server_bays"]:
            _c14(f"{name}", cx=inlet_cx, cz=out_z, fy=fy_1u)

        # Metered zone (left side): ammeter display + circuit breaker + LED
        meter_cx = -w_pdu / 2 + METER_W / 2
        if qf["bezel"]:
            # Display bezel surround (dark border)
            parts.append(_create_box_object(f"{name}_meter_bg",
                cx=meter_cx, cy=fy_1u + 0.0008, cz=out_z + h_pdu * 0.12,
                w=METER_W - 0.014, d=0.003, h=h_pdu * 0.44, collection=col,
                material='M_Black'))
            # LCD display face (dark plastic, slightly proud of bezel)
            parts.append(_create_box_object(f"{name}_meter_disp",
                cx=meter_cx, cy=fy_1u - 0.0005, cz=out_z + h_pdu * 0.12,
                w=METER_W - 0.022, d=0.003, h=h_pdu * 0.28, collection=col,
                material='M_PlasticDark'))
            # Circuit breaker button — proud disc with side walls (not a flat polygon)
            import math as _math
            _BRK_R = 0.006;  _BRK_CX = meter_cx;  _BRK_CZ = out_z - h_pdu * 0.24
            _BRK_Y0 = fy_1u - 0.0008   # rear (flush with face)
            _BRK_Y1 = fy_1u - 0.0020   # front (1.2mm proud)
            _bm_brk = bmesh.new()
            _bvf = [_bm_brk.verts.new((_BRK_CX + _BRK_R * _math.cos(2*_math.pi*k/8),
                                        _BRK_Y1,
                                        _BRK_CZ + _BRK_R * _math.sin(2*_math.pi*k/8)))
                    for k in range(8)]
            _bvb = [_bm_brk.verts.new((_BRK_CX + _BRK_R * _math.cos(2*_math.pi*k/8),
                                        _BRK_Y0,
                                        _BRK_CZ + _BRK_R * _math.sin(2*_math.pi*k/8)))
                    for k in range(8)]
            _bm_brk.faces.new(_bvf)  # front face
            for _k in range(8):
                _n = (_k + 1) % 8
                _bm_brk.faces.new([_bvf[_k], _bvb[_k], _bvb[_n], _bvf[_n]])  # sides
            parts.append(_sw_mesh_obj(f"{name}_breaker", _bm_brk, col, 'M_DarkGrayMet'))
            # Power LED — only used at non-hero quality; hero adds status_leds instead
            if not qf.get("led_emissive", False):
                parts.append(_create_box_object(f"{name}_pwr_led",
                    cx=_jitter(meter_cx + 0.016, 0.002, rv),
                    cy=fy_1u - 0.0010,
                    cz=_jitter(out_z - h_pdu * 0.12, 0.001, rv),
                    w=0.005, d=0.003, h=0.005, collection=col,
                    material='M_LED_Green'))

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

        # ── Hero: M6 mounting screws on ears ─────────────────────────────────
        if qf["ear_screws"] and qf["bezel"]:
            import math as _math
            _SCR1_R  = 0.003
            _SCR1_Y  = -ear_d * 0.5
            for _side_s, _slbl_s in [(-1, 'L'), (1, 'R')]:
                _ear_cx_s = _side_s * (w_pdu / 2 + ear_w / 2)
                _bm_scr1  = bmesh.new()
                for _scr_z1 in [h_pdu * 0.25, h_pdu * 0.75]:
                    _cv1 = [_bm_scr1.verts.new((
                        _ear_cx_s + _SCR1_R * _math.cos(2 * _math.pi * _k / 8),
                        _SCR1_Y,
                        _scr_z1  + _SCR1_R * _math.sin(2 * _math.pi * _k / 8)))
                        for _k in range(8)]
                    _bm_scr1.faces.new(_cv1)
                    _sw_box(_bm_scr1,
                            _ear_cx_s - _SCR1_R * 0.80, _ear_cx_s + _SCR1_R * 0.80,
                            _SCR1_Y - 0.0004, _SCR1_Y,
                            _scr_z1  - _SCR1_R * 0.20, _scr_z1  + _SCR1_R * 0.20)
                    _sw_box(_bm_scr1,
                            _ear_cx_s - _SCR1_R * 0.20, _ear_cx_s + _SCR1_R * 0.20,
                            _SCR1_Y - 0.0004, _SCR1_Y,
                            _scr_z1  - _SCR1_R * 0.80, _scr_z1  + _SCR1_R * 0.80)
                parts.append(_sw_mesh_obj(f"{name}_ear_screw_{_slbl_s}", _bm_scr1, col, 'M_DarkGrayMet'))

        # ── Hero: management zone — RJ45 network port + status LEDs ──────────
        if qf["bezel"]:
            _MGT_CX  = meter_cx   # same X centre as display
            _RJ1_CZ  = out_z - h_pdu * 0.34   # lower quarter of PDU height
            _RJ1_OW  = 0.020;  _RJ1_OH = 0.016
            _RJ1_IW  = 0.0138; _RJ1_IH = 0.0092
            # RJ45 bezel — open 4-bar frame so port recess is visible
            _bm_rj1_bez = bmesh.new()
            _rj1_ow2 = _RJ1_OW / 2;  _rj1_oh2 = _RJ1_OH / 2
            _rj1_iw2 = _RJ1_IW / 2;  _rj1_ih2 = _RJ1_IH / 2
            _rj1_fy0 = fy_1u - 0.0018;  _rj1_fy1 = fy_1u   # 1.8mm proud
            _sw_box(_bm_rj1_bez, _MGT_CX - _rj1_ow2, _MGT_CX + _rj1_ow2,
                    _rj1_fy0, _rj1_fy1,
                    _RJ1_CZ + _rj1_ih2, _RJ1_CZ + _rj1_oh2)   # top
            _sw_box(_bm_rj1_bez, _MGT_CX - _rj1_ow2, _MGT_CX + _rj1_ow2,
                    _rj1_fy0, _rj1_fy1,
                    _RJ1_CZ - _rj1_oh2, _RJ1_CZ - _rj1_ih2)   # bottom
            _sw_box(_bm_rj1_bez, _MGT_CX - _rj1_ow2, _MGT_CX - _rj1_iw2,
                    _rj1_fy0, _rj1_fy1,
                    _RJ1_CZ - _rj1_ih2, _RJ1_CZ + _rj1_ih2)   # left
            _sw_box(_bm_rj1_bez, _MGT_CX + _rj1_iw2, _MGT_CX + _rj1_ow2,
                    _rj1_fy0, _rj1_fy1,
                    _RJ1_CZ - _rj1_ih2, _RJ1_CZ + _rj1_ih2)   # right
            parts.append(_sw_mesh_obj(f"{name}_rj_bezel", _bm_rj1_bez, col, 'M_DarkGrayMet'))
            # RJ45 dark port recess
            _bm_rj1_recess = bmesh.new()
            _sw_box(_bm_rj1_recess,
                    _MGT_CX - _RJ1_IW / 2, _MGT_CX + _RJ1_IW / 2,
                    fy_1u, fy_1u + 0.0060,
                    _RJ1_CZ - _RJ1_IH / 2, _RJ1_CZ + _RJ1_IH / 2)
            parts.append(_sw_mesh_obj(f"{name}_rj_recess", _bm_rj1_recess, col, 'M_PlasticDark'))
            # 8 gold contact pins
            _bm_rj1_pins = bmesh.new()
            _rj1_pw = _RJ1_IW / 10
            for _pi1 in range(8):
                _rpx1_0 = _MGT_CX - _RJ1_IW / 2 + _pi1 * (_RJ1_IW / 8) + _rj1_pw * 0.15
                _rpx1_1 = _rpx1_0 + _rj1_pw * 0.70
                _sw_box(_bm_rj1_pins, _rpx1_0, _rpx1_1,
                        fy_1u + 0.0020, fy_1u + 0.0045,
                        _RJ1_CZ - _RJ1_IH / 2 + 0.001, _RJ1_CZ - _RJ1_IH / 2 + 0.004)
            parts.append(_sw_mesh_obj(f"{name}_rj_pins", _bm_rj1_pins, col, 'M_Gold'))
            # Two status LEDs — neat horizontal pair just below the display
            if qf["led_emissive"]:
                _bm_sleds1 = bmesh.new()
                _sled_z = out_z + h_pdu * 0.38   # same row as outlet LEDs
                for _li1 in range(2):
                    _slx = _MGT_CX - 0.006 + _li1 * 0.010
                    _sw_box(_bm_sleds1,
                            _slx - 0.0022, _slx + 0.0022,
                            fy_1u - 0.0025, fy_1u,
                            _sled_z - 0.0022, _sled_z + 0.0022)
                parts.append(_sw_mesh_obj(f"{name}_status_leds", _bm_sleds1, col, 'M_LED_Green'))

        # ── Hero: outlet label strip + per-outlet LEDs ───────────────────────
        if qf["bezel"]:
            parts.append(_create_box_object(f"{name}_lbl_strip",
                cx=out_x0 + OUT_ZONE / 2, cy=fy_1u - 0.0005, cz=out_z + h_pdu * 0.38,
                w=OUT_ZONE - 0.004, d=0.0015, h=h_pdu * 0.10, collection=col))
            if qf["led_emissive"]:
                _bm_oleds1 = bmesh.new()
                for _ox1 in outlet_xs:
                    _sw_box(_bm_oleds1,
                            _ox1 - 0.0022, _ox1 + 0.0022,
                            fy_1u - 0.0028, fy_1u,
                            out_z + h_pdu * 0.35 - 0.0022, out_z + h_pdu * 0.35 + 0.0022)
                parts.append(_sw_mesh_obj(f"{name}_outlet_leds", _bm_oleds1, col, 'M_LED_Green'))

        if join_mesh:
            joined = _join_parts(parts, name)
        else:
            joined = parts[0]

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
        "join_mesh":    join_mesh,
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
