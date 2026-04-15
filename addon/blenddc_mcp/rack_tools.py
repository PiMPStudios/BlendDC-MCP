"""
Rack cabinet generation and management tools for the BlendDC asset pipeline.

Coordinate convention (origin at base-front-centre):
  X = rack centreline  (negative = left,  positive = right)
  Y = depth            (0 = front face,   positive = toward rear)
  Z = height           (0 = floor,        positive = up)

All geometry is created via bmesh (no interactive operators) so it is thread-safe
when called through the @thread_safe decorator.

EIA-310 geometry is defined in constants.py.
"""

import bpy
import bmesh
import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

import mathutils

from core import mcp, thread_safe, _log
from constants import (
    RACK_U_M, RACK_U_MM,
    EIA_RAIL_SPAN_M, EIA_RAIL_SPAN_MM,
    RACK_HOLE_OFFSETS_MM, RACK_HOLE_SIZE_M,
    RACK_DEFAULT_U_HEIGHT,
    RACK_DEFAULT_WIDTH_MM, RACK_DEFAULT_DEPTH_MM,
    RACK_BASE_HEIGHT_M, RACK_BASE_HEIGHT_MM,
    RACK_TOP_HEIGHT_M, RACK_TOP_HEIGHT_MM,
    RACK_INTERIOR_HEIGHT_MM, RACK_INTERIOR_HEIGHT_M,
    RACK_POST_SIZE_M, RACK_POST_SIZE_MM,
    RACK_SHEET_THICK_M, RACK_SHEET_THICK_MM,
    RACK_RAIL_THICK_M, RACK_RAIL_THICK_MM,
    RACK_RAIL_FLANGE_W_M, RACK_RAIL_FLANGE_W_MM,
    RACK_INNER_CLEAR_M,
    EIA_HOLE_INSET_M,
    RACK_SETBACK_FRONT_M, RACK_SETBACK_REAR_M,
    EAR_SETBACK_M,
    QUALITY_TIERS,
    HINGE_PIN_DIAM_M, HINGE_PIN_HEIGHT_M, HINGE_COUNT_PER_DOOR,
    LATCH_WIDTH_M, LATCH_HEIGHT_M, LATCH_DEPTH_M,
    ANCHOR_INSET_M,
    HINGE_POSITIONS,
    DOOR_SHEET_THICK_M,
    DOOR_VENT_SLOT_W_M, DOOR_VENT_SLOT_H_M,
    DOOR_VENT_GAP_X_M, DOOR_VENT_GAP_Y_M, DOOR_VENT_MARGIN_M,
    BRUSH_STRIP_HEIGHT_M, BRUSH_STRIP_DEPTH_M,
    CABLE_ENTRY_CUTOUT_W_M, CABLE_ENTRY_CUTOUT_H_M,
    CABLE_TRAY_DEPTH_M, CABLE_TRAY_WALL_THICK_M,
    VERT_CABLE_MGMT_WIDTH_M,
    FAN_TRAY_HEIGHT_M, FAN_TRAY_PANEL_H_M,
    FAN_SIZE_M, FAN_FRAME_WALL_M, FAN_GRID_COLS, FAN_GRID_ROWS,
    VENT_BAR_W_M, VENT_SLOT_GAP_M,
    FLOOR_BRACKET_VERT_H_M, FLOOR_BRACKET_VERT_W_M, FLOOR_BRACKET_VERT_T_M,
    FLOOR_BRACKET_FLANGE_L_M, FLOOR_BRACKET_FLANGE_T_M,
    RACK_CROSSBAR_H_M, RACK_CROSSBAR_T_M,
)


# ── Internal geometry helpers ──────────────────────────────────────────────

def _create_box_object(
    name: str,
    cx: float, cy: float, cz: float,
    w: float, d: float, h: float,
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    """
    Create a solid box mesh object centred at (cx, cy, cz) with dimensions w×d×h.
    Links to collection only (not the root scene collection).
    """
    mesh = bpy.data.meshes.new(name)
    obj  = bpy.data.objects.new(name, mesh)

    bm = bmesh.new()
    # create_cube(size=2.0) generates a 2×2×2 cube (vertices at ±1.0);
    # the diagonal matrix then scales each axis to the exact target dimension:
    #   X: ±1 × w*0.5 → ±w/2  (total width  = w)
    #   Y: ±1 × d*0.5 → ±d/2  (total depth  = d)
    #   Z: ±1 × h*0.5 → ±h/2  (total height = h)
    # NOTE: size=1.0 would give a 1×1×1 cube (±0.5) producing half-size geometry.
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
        # Always restore cursor — never strand it on failure
        with contextlib.suppress(Exception):
            bpy.context.scene.cursor.location = saved


def _bmesh_join(
    name: str,
    parts: List[bpy.types.Object],
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    """
    Combine a list of mesh objects into a single joined mesh object.

    For each part, the mesh data is copied and transformed by
    Matrix.Translation(part.location) to bake the world position into vertex
    coordinates.  The resulting object is placed at (0, 0, 0) so its origin
    is the world origin (base-front-centre for rack parts).

    All input part objects and their mesh data blocks are removed after joining.
    Returns the new joined object.
    """
    combined_bm = bmesh.new()
    tmp_meshes: List[bpy.types.Mesh] = []

    for part in parts:
        tmp = part.data.copy()
        tmp.transform(mathutils.Matrix.Translation(part.location))
        combined_bm.from_mesh(tmp)
        tmp_meshes.append(tmp)

    combined_mesh = bpy.data.meshes.new(name)
    combined_bm.to_mesh(combined_mesh)
    combined_bm.free()
    combined_mesh.update()

    joined = bpy.data.objects.new(name, combined_mesh)
    joined.location = (0.0, 0.0, 0.0)
    collection.objects.link(joined)

    part_meshes = [p.data for p in parts]
    try:
        for part in parts:
            bpy.data.objects.remove(part, do_unlink=True)
        for m in part_meshes + tmp_meshes:
            if m.users == 0:
                bpy.data.meshes.remove(m)
    except Exception:
        pass

    return joined


def _create_l_rail(
    name: str,
    sign_x: int,
    half_inner: float,
    rfw: float,
    rt: float,
    setback: float,
    cz: float,
    height: float,
    frame_height: float,
    web_depth: float,
    web_cy: float,
    collection: bpy.types.Collection,
    eia_holes: bool = True,
    u_height: int = 42,
) -> Dict[str, Any]:
    """
    Create one vertical L-channel post: structural web + EIA mounting face.

    Cross-section viewed from above (left rail, sign_x = -1):

        X = -(half_inner + rfw + rt)   -(half_inner + rfw)   -half_inner
                        |<-- web rt wide -->|<----- rfw ------>|
                        [    web plate      ][  mounting face   ]  (open interior →)
                                            ↑
                               inner edge at ±half_inner (225 mm from centre)

    Web:    thin sheet (rt × full_depth × frame_height) at the outermost X position.
            Spans the full cabinet depth and full structural height.
    Flange: thin mounting face (rfw × rt × height) at Y = setback, inner edge at
            ±half_inner.  EIA holes punched through via bmesh extrusion.

    EIA hole centres: sign_x × (EIA_RAIL_SPAN_M/2 − EIA_HOLE_INSET_M) = ±233.4 mm.
    Inner clear opening between flange inner faces: 2 × half_inner = 450 mm.
    Equipment body (446 mm) slides through with ~4 mm clearance each side.

    sign_x:      -1 = left rail, +1 = right rail
    half_inner:  half the inner clear opening (RACK_INNER_CLEAR_M / 2 = 225 mm)
    rfw:         flange width in X (RACK_RAIL_FLANGE_W_M = 57.15 mm)
    rt:          sheet metal thickness (RACK_RAIL_THICK_M = 2 mm)
    setback:     Y of the mounting face front surface (RACK_SETBACK_FRONT_M = 75 mm)
    cz:          Z-centre of the rail zone (base_h + rail_h / 2)
    height:      EIA rail zone height (u_height × RACK_U_M)
    frame_height: full cabinet structural height (web spans this)
    web_depth:   Y depth of the post column (post size, e.g. RACK_POST_SIZE_M = 60 mm)
    web_cy:      Y centre of the post column (ps/2 for front, d−ps/2 for rear)
    eia_holes:   True = punch EIA holes via bmesh; False = solid box flange
    u_height:    rack unit count (used when eia_holes=True)
    """
    # ── Web: L-channel post column — narrow vertical member at each corner ──
    # Depth = post cross-section only (not full rack depth) so the open frame
    # does not look like it has solid side panels.  Side crossbars provide the
    # front-to-rear structural connection.
    half_outer = half_inner + rfw
    web = _create_box_object(
        f"{name}_web",
        cx=sign_x * (half_outer + rt / 2),
        cy=web_cy,
        cz=frame_height / 2,
        w=rt, d=web_depth, h=frame_height,
        collection=collection,
    )

    # X extents of the mounting face (inner face at ±half_inner, outer at ±half_outer)
    x_inner = sign_x * half_inner
    x_outer = sign_x * half_outer
    x_l = min(x_inner, x_outer)   # smaller X value
    x_r = max(x_inner, x_outer)   # larger  X value

    # EIA hole centre X: 7.938 mm inset from equipment panel edge (482.6 mm / 2 = 241.3 mm)
    hole_cx = sign_x * (EIA_RAIL_SPAN_M / 2 - EIA_HOLE_INSET_M)  # ±233.362 mm
    hole_sz = RACK_HOLE_SIZE_M                                     # 9.525 mm
    x_hl = hole_cx - hole_sz / 2   # left edge of hole slot
    x_hr = hole_cx + hole_sz / 2   # right edge of hole slot

    if not eia_holes:
        flange = _create_box_object(
            f"{name}_flange",
            cx=(x_l + x_r) / 2,
            cy=setback + rt / 2,
            cz=cz,
            w=x_r - x_l, d=rt, h=height,
            collection=collection,
        )
        return {"web": web, "flange_parts": [flange]}

    # ── Flange with EIA-310 through-holes via bmesh face extrusion ──────────
    #
    # EIA-310 pattern per U (44.45 mm), measured from bottom of U:
    #   Hole 1:  0.000 –  9.525 mm   (open)
    #   Bar  1:  9.525 – 15.880 mm   6.355 mm solid
    #   Hole 2: 15.880 – 25.405 mm   (open)
    #   Bar  2: 25.405 – 28.570 mm   3.165 mm solid
    #   Hole 3: 28.570 – 38.095 mm   (open)
    #   Bar  3: 38.095 – 44.450 mm   6.355 mm solid (connects to next U)
    #
    # Topology in X (4 vertices per row): x_l … x_hl … x_hr … x_r
    #   Bar segment  → 3 faces: [x_l,x_hl], [x_hl,x_hr], [x_hr,x_r]  (all solid)
    #   Hole segment → 2 faces: [x_l,x_hl],              [x_hr,x_r]  (center open)
    # After remove_doubles all shared boundary vertices merge cleanly.
    # extrude_face_region then creates closed hole tunnels automatically.

    off1 = RACK_HOLE_OFFSETS_MM[1] / 1000.0   # 0.01588 m
    off2 = RACK_HOLE_OFFSETS_MM[2] / 1000.0   # 0.02857 m
    u_m  = RACK_U_M                            # 0.04445 m

    rail_base_z = cz - height / 2.0            # Z of rail zone bottom
    y_f = setback                              # front face of mounting flange

    bm = bmesh.new()

    def _quad(x0: float, x1: float, z0: float, z1: float) -> None:
        """Add one quad face on the front face (Y=y_f) of the flange."""
        v0 = bm.verts.new((x0, y_f, z0))
        v1 = bm.verts.new((x1, y_f, z0))
        v2 = bm.verts.new((x1, y_f, z1))
        v3 = bm.verts.new((x0, y_f, z1))
        bm.faces.new([v0, v1, v2, v3])

    for u in range(u_height):
        uz = rail_base_z + u * u_m

        # Six segments per U: (z_start, z_end, is_hole)
        segs = [
            (uz,                    uz + hole_sz,          True),
            (uz + hole_sz,          uz + off1,             False),
            (uz + off1,             uz + off1 + hole_sz,   True),
            (uz + off1 + hole_sz,   uz + off2,             False),
            (uz + off2,             uz + off2 + hole_sz,   True),
            (uz + off2 + hole_sz,   uz + u_m,              False),
        ]

        for z0, z1, is_hole in segs:
            if z1 - z0 < 1e-9:
                continue
            if is_hole:
                # Two margin quads; center is left open (the hole)
                _quad(x_l, x_hl, z0, z1)
                _quad(x_hr, x_r, z0, z1)
            else:
                # Three sub-quads — consistent topology so vertices merge cleanly
                _quad(x_l,  x_hl, z0, z1)
                _quad(x_hl, x_hr, z0, z1)
                _quad(x_hr, x_r,  z0, z1)

    # Merge coincident vertices at segment boundaries
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=1e-6)

    # Extrude in +Y by rt (2 mm) to give the mounting face its thickness.
    original_faces = list(bm.faces)
    ret = bmesh.ops.extrude_face_region(bm, geom=original_faces)
    new_verts = [v for v in ret["geom"] if isinstance(v, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, vec=(0.0, rt, 0.0), verts=new_verts)

    bm.normal_update()

    flange_mesh = bpy.data.meshes.new(f"{name}_flange")
    bm.to_mesh(flange_mesh)
    bm.free()
    flange_mesh.update()

    flange = bpy.data.objects.new(f"{name}_flange", flange_mesh)
    flange.location = (0.0, 0.0, 0.0)
    collection.objects.link(flange)

    return {"web": web, "flange_parts": [flange]}


def _add_door_hardware(
    name_prefix: str,
    w: float,
    base_h: float,
    rail_h: float,
    collection: bpy.types.Collection,
    face: str = "front",
    depth: float = 0.0,
) -> List[bpy.types.Object]:
    """
    Add hinge-pin stubs (left side) and latch receiver (right side) on the
    specified face. face='front' protrudes at Y=0; face='rear' protrudes at Y=depth.
    Returns the list of hardware objects created.
    """
    objs: List[bpy.types.Object] = []
    tag = "rear" if face == "rear" else "front"

    if face == "rear":
        hinge_cy = depth + HINGE_PIN_HEIGHT_M / 2   # protrudes past rear face
        latch_cy  = depth + LATCH_DEPTH_M / 2
    else:
        hinge_cy = -(HINGE_PIN_HEIGHT_M / 2)         # protrudes forward of Y=0
        latch_cy  = -(LATCH_DEPTH_M / 2)

    # ── Hinge pins (left side, 3 per door) ───────────────────────────────
    hinge_z_positions = [base_h + rail_h * pos for pos in HINGE_POSITIONS]
    for i, hz in enumerate(hinge_z_positions):
        hinge = _create_box_object(
            f"{name_prefix}_hinge_{tag}_{i}",
            cx=-(w / 2) + ANCHOR_INSET_M,
            cy=hinge_cy,
            cz=hz,
            w=HINGE_PIN_DIAM_M,
            d=HINGE_PIN_HEIGHT_M,
            h=HINGE_PIN_DIAM_M,
            collection=collection,
        )
        objs.append(hinge)

    # ── Latch receiver (right side, centre height) ────────────────────────
    latch = _create_box_object(
        f"{name_prefix}_latch_{tag}",
        cx=(w / 2) - ANCHOR_INSET_M,
        cy=latch_cy,
        cz=base_h + rail_h * 0.50,
        w=LATCH_WIDTH_M,
        d=LATCH_DEPTH_M,
        h=LATCH_HEIGHT_M,
        collection=collection,
    )
    objs.append(latch)

    return objs


# ── Floor bracket helper ──────────────────────────────────────────────────

def _create_floor_bracket(
    name_prefix: str,
    half_w: float,
    post_cy: float,
    corner_tag: str,
    collection: bpy.types.Collection,
) -> List[bpy.types.Object]:
    """
    Create a seismic floor-mounting L-bracket at one rack corner post.

    Represents a standard steel angle bracket: a vertical plate bolted flush
    against the cabinet outer face (at ±half_w), plus a horizontal floor flange
    that sits on the raised floor tile and accepts anchor bolts.

    corner_tag: "FL"|"FR"|"RL"|"RR" (Front-Left / Front-Right / Rear-Left / Rear-Right)
    half_w:     half the cabinet external width (w / 2) — bracket inner face is flush here
    post_cy:    Y centre of the bracket (aligned with the corner post)
    """
    objs: List[bpy.types.Object] = []

    sign_x = -1 if corner_tag.endswith("L") else +1

    # Vertical plate — inner face flush with cabinet outer face (±half_w),
    # runs 40 mm along Y (spanning post face width), 80 mm tall
    vert = _create_box_object(
        f"{name_prefix}_vert",
        cx=sign_x * (half_w + FLOOR_BRACKET_VERT_T_M / 2),
        cy=post_cy,
        cz=FLOOR_BRACKET_VERT_H_M / 2,
        w=FLOOR_BRACKET_VERT_T_M,
        d=FLOOR_BRACKET_VERT_W_M,
        h=FLOOR_BRACKET_VERT_H_M,
        collection=collection,
    )
    objs.append(vert)

    # Horizontal floor flange — extends outward in X from the cabinet face,
    # lies flat on the floor, accepts anchor bolts
    flange = _create_box_object(
        f"{name_prefix}_flange",
        cx=sign_x * (half_w + FLOOR_BRACKET_FLANGE_L_M / 2),
        cy=post_cy,
        cz=FLOOR_BRACKET_FLANGE_T_M / 2,
        w=FLOOR_BRACKET_FLANGE_L_M,
        d=FLOOR_BRACKET_VERT_W_M,
        h=FLOOR_BRACKET_FLANGE_T_M,
        collection=collection,
    )
    objs.append(flange)

    return objs


# ── Fan tray helper ────────────────────────────────────────────────────────

def _fan_zone_bounds(w: float, d: float):
    """Return (fz_w, fz_d, fz_x0, fz_x1, fz_y0, fz_y1) for the 2×2 fan zone."""
    fz_w = FAN_GRID_COLS * FAN_SIZE_M + (FAN_GRID_COLS - 1) * FAN_FRAME_WALL_M
    fz_d = FAN_GRID_ROWS * FAN_SIZE_M + (FAN_GRID_ROWS - 1) * FAN_FRAME_WALL_M
    fz_x0 = -fz_w / 2
    fz_x1 =  fz_w / 2
    fz_y0 = d / 2 - fz_d / 2
    fz_y1 = d / 2 + fz_d / 2
    return fz_w, fz_d, fz_x0, fz_x1, fz_y0, fz_y1


def _create_zoned_vent_plate(
    name_prefix: str,
    w: float,
    d: float,
    cz: float,
    h: float,
    collection: bpy.types.Collection,
) -> List[bpy.types.Object]:
    """
    Build a horizontal plate that is solid everywhere except the 2×2 fan zone,
    where it has ventilation slots (bars running full fan-zone width, gaps in Y).

    Solid sections: left strip, right strip, front centre piece, rear centre piece.
    Slotted section: bars spanning the fan zone width with open gaps between them.
    """
    objs: List[bpy.types.Object] = []
    fz_w, fz_d, fz_x0, fz_x1, fz_y0, fz_y1 = _fan_zone_bounds(w, d)

    # Left solid strip (full depth)
    left_w = w / 2 + fz_x0
    objs.append(_create_box_object(f"{name_prefix}_solid_L",
        cx=(fz_x0 - w / 2) / 2, cy=d / 2, cz=cz,
        w=left_w, d=d, h=h, collection=collection))

    # Right solid strip (full depth)
    right_w = w / 2 - fz_x1
    objs.append(_create_box_object(f"{name_prefix}_solid_R",
        cx=(fz_x1 + w / 2) / 2, cy=d / 2, cz=cz,
        w=right_w, d=d, h=h, collection=collection))

    # Front solid piece (fan zone X width, rack front to fan zone front)
    if fz_y0 > 0:
        objs.append(_create_box_object(f"{name_prefix}_solid_F",
            cx=0.0, cy=fz_y0 / 2, cz=cz,
            w=fz_w, d=fz_y0, h=h, collection=collection))

    # Rear solid piece (fan zone X width, fan zone rear to rack rear)
    rear_d = d - fz_y1
    if rear_d > 0:
        objs.append(_create_box_object(f"{name_prefix}_solid_Rr",
            cx=0.0, cy=(fz_y1 + d) / 2, cz=cz,
            w=fz_w, d=rear_d, h=h, collection=collection))

    # Slotted bars within fan zone (bars span fz_w, gaps in Y)
    # Array is centred in the fan zone so margins are equal at front and rear.
    pitch = VENT_BAR_W_M + VENT_SLOT_GAP_M
    n_bars = max(1, int(fz_d / pitch))
    total_span = n_bars * VENT_BAR_W_M + (n_bars - 1) * VENT_SLOT_GAP_M
    offset = (fz_d - total_span) / 2   # equal margin each side
    for i in range(n_bars):
        bar_cy = fz_y0 + offset + VENT_BAR_W_M / 2 + i * pitch
        objs.append(_create_box_object(f"{name_prefix}_slot_{i:02d}",
            cx=0.0, cy=bar_cy, cz=cz,
            w=fz_w, d=VENT_BAR_W_M, h=h, collection=collection))

    return objs


def _create_fan_tray(
    name_prefix: str,
    w: float,
    d: float,
    z_base: float,
    collection: bpy.types.Collection,
) -> List[bpy.types.Object]:
    """
    Create a 1U fan tray plate at the base of the top cap zone.
    Solid everywhere except the 2×2 fan zone, which has intake slots.
    """
    return _create_zoned_vent_plate(
        name_prefix=f"{name_prefix}_fan_tray",
        w=w, d=d,
        cz=z_base + FAN_TRAY_PANEL_H_M / 2,
        h=FAN_TRAY_PANEL_H_M,
        collection=collection,
    )


# ── Tool 1: create_rack_cabinet ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_rack_cabinet(
    name: str = "ServerRack",
    u_height: int = 42,
    width_mm: float = 600.0,
    depth_mm: float = 1000.0,
    post_size_mm: float = 60.0,
    sheet_thickness_mm: float = 1.5,
    include_side_panels: bool = True,
    include_top_panel: bool = True,
    include_base: bool = True,
    include_door_mounts: bool = True,
    include_fan_tray: bool = True,
    include_crossbars: bool = True,
    include_rear_panel: bool = False,
    bracket_left: bool = True,
    bracket_right: bool = True,
    join_mesh: bool = True,
    eia_holes: bool = True,
    lod_rails: bool = True,
    quality: str = "high",
) -> Dict[str, Any]:
    """
    Generate a parametric 4-post enclosed server rack cabinet.

    Produces accurate EIA-310 geometry: 42U interior = 1866.9 mm, 19" rail span
    (482.6 mm inner face to inner face), 4 structural corner posts with continuous
    L-bracket mounting rails, sheet-metal side/top panels, door hardware mounting
    points, floor-anchor L-brackets, structural crossbars, and a 1U exhaust fan
    tray with 2×2 fan grid in the top cap section.

    Front and rear are open by default (no door panels). Use create_rack_doors or
    add doors individually when needed.

    Origin is placed at base-front-centre:
      X = rack centreline, Y = front face (0), Z = floor (0)

    name:               Blender collection name for the rack
    u_height:           Rack unit height (default 42)
    width_mm:           External cabinet width in mm (default 600)
    depth_mm:           External cabinet depth in mm (default 1000)
    post_size_mm:       Square cross-section of corner posts in mm (default 60)
    sheet_thickness_mm: Panel sheet metal thickness in mm (default 1.5)
    include_side_panels: Add left/right side panels
    include_top_panel:   Add top cap panel
    include_base:        Add floor-mounting L-brackets (controlled per-side by bracket_left/right)
    bracket_left:        Include floor brackets on the left (−X) side (default True).
                         Set False for racks that have a neighbour on their left — brackets
                         only belong on exposed end faces, not between adjacent cabinets.
    bracket_right:       Include floor brackets on the right (+X) side (default True).
                         Set False for racks that have a neighbour on their right.
    include_door_mounts: Add hinge pin stubs and latch receivers on front and rear faces
    include_fan_tray:    Add 1U exhaust fan tray (2×2 fans) at top of rail zone
    include_crossbars:   Add structural side crossbars (front-to-back on each side, at 1/3 and 2/3 height)
    include_rear_panel:  Add solid rear panel (default False — rear is open)
    join_mesh:           Join all parts into a single mesh object (default True)
    eia_holes:           Punch EIA-310 square mounting holes into rail flanges
                         (default True); set False for a solid flange (no holes)
    lod_rails:           Also generate {name}_Rails_LOD1 — solid L-brackets with no
                         holes for medium-distance rendering (default True).
                         Only meaningful when join_mesh=True and eia_holes=True.

    Returns collection name, object list, key sockets, rack dimensions, and origin.
    """
    # ── Derive dimensions ──────────────────────────────────────────────────
    w          = width_mm / 1000.0
    d          = depth_mm / 1000.0
    ps         = post_size_mm / 1000.0
    st         = sheet_thickness_mm / 1000.0
    rt         = RACK_RAIL_THICK_M
    rfw        = RACK_RAIL_FLANGE_W_M
    half_inner = RACK_INNER_CLEAR_M / 2    # 225 mm — inner clear half-width
    bh         = RACK_BASE_HEIGHT_M
    th         = RACK_TOP_HEIGHT_M
    rh         = u_height * RACK_U_M       # usable rail height
    tot        = bh + rh + th              # total cabinet height

    # Outer X extent of each rail assembly (web outer face centre)
    half_outer = half_inner + rfw          # 282.15 mm from centre

    # Floor bracket and crossbar reference positions
    post_cx   = half_outer + rt / 2        # outermost X of web centre
    post_cy_f = ps / 2                     # front bracket Y centre
    post_cy_r = d - ps / 2                 # rear bracket / crossbar Y centre

    # ── Resolve quality flags ──────────────────────────────────────────────
    qf = QUALITY_TIERS.get(quality, QUALITY_TIERS["high"])
    # quality overrides explicit bool params so callers can pass quality="low"
    # without needing to specify every sub-flag individually.
    eia_holes        = qf["eia_holes"]
    lod_rails        = qf["lod_rails"]
    include_fan_tray = include_fan_tray and qf["fan_tray"]
    include_crossbars = include_crossbars and qf["crossbars"]

    warnings: List[str] = []
    if half_outer + rt > w / 2:
        warnings.append(
            f"Rail outer edge ({round((half_outer + rt) * 1000, 1)} mm) exceeds "
            f"cabinet half-width ({round(w / 2 * 1000, 1)} mm) — increase width_mm"
        )

    # ── Create collection ──────────────────────────────────────────────────
    # Ensure unique collection name
    col_name = name
    idx = 1
    while col_name in bpy.data.collections:
        col_name = f"{name}.{idx:03d}"
        idx += 1

    col = bpy.data.collections.new(col_name)
    bpy.context.scene.collection.children.link(col)

    # Store rack metadata as custom properties for downstream tools
    col["rack_u_height"]         = u_height
    col["rack_width_mm"]         = width_mm
    col["rack_depth_mm"]         = depth_mm
    col["rack_post_size_mm"]     = post_size_mm
    col["rack_sheet_thick_mm"]   = sheet_thickness_mm
    col["rack_base_height_m"]    = bh
    col["rack_rail_height_m"]    = rh
    col["rack_top_height_m"]     = th
    col["rack_total_height_m"]   = tot
    col["rack_half_span_m"]      = EIA_RAIL_SPAN_M / 2   # kept for back-compat
    col["rack_inner_clear_m"]    = RACK_INNER_CLEAR_M
    col["rack_setback_front_m"]  = RACK_SETBACK_FRONT_M
    col["rack_setback_rear_m"]   = RACK_SETBACK_REAR_M
    col["rack_has_fan_tray"]     = include_fan_tray
    col["rack_has_crossbars"]    = include_crossbars
    col["is_rack_cabinet"]       = True
    col["quality"]               = quality

    frame_objs: List[bpy.types.Object] = []   # open-frame skeleton → _Body
    shell_objs: List[bpy.types.Object] = []   # enclosure skin      → _Shell
    rail_objs:  List[bpy.types.Object] = []   # EIA mounting faces  → _Rails

    # ── Floor-mounting L-brackets ──────────────────────────────────────────
    # Part of the enclosure shell (not the open-frame skeleton).
    # Only placed on exposed sides: bracket_left controls −X, bracket_right +X.
    # Interior racks in a row should have both suppressed on their shared faces.
    if include_base:
        bracket_configs = [
            ("FL", post_cy_f),
            ("FR", post_cy_f),
            ("RL", post_cy_r),
            ("RR", post_cy_r),
        ]
        for tag, bcy in bracket_configs:
            if tag.endswith("L") and not bracket_left:
                continue
            if tag.endswith("R") and not bracket_right:
                continue
            bracket_parts = _create_floor_bracket(
                name_prefix=f"{col_name}_bracket_{tag}",
                half_w=w / 2,
                post_cy=bcy,
                corner_tag=tag,
                collection=col,
            )
            shell_objs.extend(bracket_parts)

    # ── Mounting rails — L-channel posts with integrated EIA mounting faces ─
    # Four rails: front-left, front-right, rear-left, rear-right.
    # Each rail IS the structural post: web at outer X spans full depth/height;
    # mounting flange at inner edge (±225 mm) with EIA-310 holes.
    #
    # Rail setback (Y of mounting face front surface):
    #   Front: RACK_SETBACK_FRONT_M = 75 mm
    #   Rear:  mounting face front at d − setback_r − rt
    setback_f   = RACK_SETBACK_FRONT_M                # 0.075 m
    setback_r   = RACK_SETBACK_REAR_M                 # 0.125 m
    setback_r_y = d - setback_r - rt                  # Y of rear flange front face

    # Post column Y centres and depths (front/rear posts, not spanning full depth)
    web_cy_f = ps / 2          # front post column Y centre
    web_cy_r = d - ps / 2      # rear post column Y centre

    rail_configs = [
        # (tag,  sign_x, setback_y,   web_cy,    web_depth)
        ("LF",   -1,     setback_f,   web_cy_f,  ps),
        ("RF",   +1,     setback_f,   web_cy_f,  ps),
        ("LR",   -1,     setback_r_y, web_cy_r,  ps),
        ("RR",   +1,     setback_r_y, web_cy_r,  ps),
    ]
    for tag, sx, sb_y, wcy, wdepth in rail_configs:
        parts = _create_l_rail(
            name=f"{col_name}_rail_{tag}",
            sign_x=sx,
            half_inner=half_inner,
            rfw=rfw,
            rt=rt,
            setback=sb_y,
            cz=bh + rh / 2,
            height=rh,
            frame_height=bh + rh,
            web_depth=wdepth,
            web_cy=wcy,
            collection=col,
            eia_holes=eia_holes,
            u_height=u_height,
        )
        frame_objs.append(parts["web"])
        # low quality: web posts only — no separate rail flanges
        if qf["rack_rails"]:
            rail_objs.extend(parts["flange_parts"])
        else:
            # discard flange objects — not needed for box-proxy quality
            for fp in parts["flange_parts"]:
                bpy.data.objects.remove(fp, do_unlink=True)

    # ── Structural side crossbars ──────────────────────────────────────────
    # Four horizontal bars (two per side) running front-to-back on each side of
    # the open frame at 1/3 and 2/3 of rail height.  Positioned at the outer face
    # of each rail web so they're visible from the side of the open-frame rack.
    # (Rear-spanning bars are part of the enclosure shell, not the open frame.)
    if include_crossbars:
        for frac, tag in ((1.0 / 3.0, "lower"), (2.0 / 3.0, "upper")):
            for sx, side_tag in ((-1, "L"), (+1, "R")):
                cbar = _create_box_object(
                    f"{col_name}_crossbar_{tag}_{side_tag}",
                    cx=sx * post_cx,
                    cy=d / 2,
                    cz=bh + rh * frac,
                    w=RACK_CROSSBAR_T_M,
                    d=d,
                    h=RACK_CROSSBAR_H_M,
                    collection=col,
                )
                frame_objs.append(cbar)

    # ── Side panels  ──→  SHELL ────────────────────────────────────────────
    if include_side_panels:
        for sx, tag in ((-1, "L"), (1, "R")):
            panel = _create_box_object(
                f"{col_name}_panel_{tag}",
                cx=sx * (w / 2 - st / 2),
                cy=d / 2,
                cz=tot / 2,
                w=st, d=d, h=tot,
                collection=col,
            )
            shell_objs.append(panel)

    # ── Rear panel  ──→  SHELL ────────────────────────────────────────────
    if include_rear_panel:
        rear = _create_box_object(
            f"{col_name}_panel_rear",
            cx=0.0,
            cy=d - st / 2,
            cz=tot / 2,
            w=w, d=st, h=tot,
            collection=col,
        )
        shell_objs.append(rear)

    # ── Top cap enclosure walls  ──→  SHELL ───────────────────────────────
    if include_top_panel:
        cap_front = _create_box_object(
            f"{col_name}_topcap_front",
            cx=0.0, cy=st / 2, cz=bh + rh + th / 2,
            w=w, d=st, h=th, collection=col,
        )
        shell_objs.append(cap_front)
        cap_rear = _create_box_object(
            f"{col_name}_topcap_rear",
            cx=0.0, cy=d - st / 2, cz=bh + rh + th / 2,
            w=w, d=st, h=th, collection=col,
        )
        shell_objs.append(cap_rear)

    # ── Top panel  ──→  SHELL ─────────────────────────────────────────────
    if include_top_panel:
        top_parts = _create_zoned_vent_plate(
            name_prefix=f"{col_name}_panel_top",
            w=w, d=d,
            cz=tot - st / 2,
            h=st,
            collection=col,
        )
        shell_objs.extend(top_parts)

    # ── Exhaust fan tray  ──→  FRAME ──────────────────────────────────────
    # Functional part of the open frame (pulls heat up from equipment).
    # Inset by sheet thickness so it sits flush inside the enclosure shell.
    if include_fan_tray:
        fan_parts = _create_fan_tray(
            name_prefix=col_name,
            w=w - 2 * st,
            d=d - 2 * st,
            z_base=bh + rh,
            collection=col,
        )
        frame_objs.extend(fan_parts)

    # ── Door hardware  ──→  SHELL ─────────────────────────────────────────
    if include_door_mounts:
        # Jamb strips: narrow vertical panels at the front and rear face openings,
        # left (hinge) and right (latch) sides.  These give the hardware a surface
        # to mount on.  Width = 2×ANCHOR_INSET so hardware cx sits at strip centre;
        # depth = st (sheet metal); height = tot (full cabinet height).
        jamb_w = 2.0 * ANCHOR_INSET_M
        for face_tag, jamb_cy in (("front", -st / 2), ("rear", d + st / 2)):
            for side_tag, jamb_cx in (("L", -(w / 2 - ANCHOR_INSET_M)),
                                       ("R",  (w / 2 - ANCHOR_INSET_M))):
                jamb = _create_box_object(
                    f"{col_name}_jamb_{face_tag}_{side_tag}",
                    cx=jamb_cx,
                    cy=jamb_cy,
                    cz=tot / 2,
                    w=jamb_w, d=st, h=tot,
                    collection=col,
                )
                shell_objs.append(jamb)

        hw_front = _add_door_hardware(
            col_name, w=w, base_h=bh, rail_h=rh, collection=col,
            face="front", depth=d,
        )
        hw_rear = _add_door_hardware(
            col_name, w=w, base_h=bh, rail_h=rh, collection=col,
            face="rear", depth=d,
        )
        shell_objs.extend(hw_front)
        shell_objs.extend(hw_rear)

    # ── Join parts (if requested) then set origin once ────────────────────
    # Origin is always placed at base-front-centre:
    #   X = 0  (rack centreline)
    #   Y = 0  (front face of the front posts)
    #   Z = 0  (floor / base of cabinet)
    #
    # Join strategy: pure bmesh combine — avoids bpy.ops.object.join() which
    # requires a visible 3D viewport context and can silently cancel when called
    # from an addon/MCP context without an active area.
    #
    # Each part is created with obj.location = (cx, cy, cz) so its vertices are
    # in LOCAL space centred at the part centre.  We apply the object's world
    # matrix into the vertex positions before merging so every vertex lands at
    # its correct world position in the final combined mesh.  The resulting
    # joined object is placed at location (0, 0, 0) — its origin IS already the
    # base-front-centre without any cursor trick needed.
    has_rails = bool(rail_objs)   # capture before _bmesh_join consumes the list
    if join_mesh and frame_objs:
        # Join open-frame parts → {col_name}_Body
        _bmesh_join(col_name + "_Body", frame_objs, col)
        # Join rail parts → {col_name}_Rails  (LOD0: full EIA-310 through-holes)
        # Skipped for low quality where rail_objs is empty.
        if rail_objs:
            _bmesh_join(col_name + "_Rails", rail_objs, col)
        # Join enclosure parts → {col_name}_Shell  (only if any shell parts exist)
        if shell_objs:
            _bmesh_join(col_name + "_Shell", shell_objs, col)

        # LOD1 rails: solid L-brackets, no holes — for medium-distance rendering (>5 m).
        # EIA holes (9.5 mm sq) are invisible beyond ~4–5 m; removing them drops the
        # rail mesh from several thousand faces to ~48 (4 rails × 2 box parts × 6 faces).
        if lod_rails and eia_holes:
            lod1_rail_objs: List[bpy.types.Object] = []
            for tag, sx, sb_y, wcy, wdepth in rail_configs:
                lod1_parts = _create_l_rail(
                    name=f"{col_name}_lod1_rail_{tag}",
                    sign_x=sx,
                    half_inner=half_inner,
                    rfw=rfw,
                    rt=rt,
                    setback=sb_y,
                    cz=bh + rh / 2,
                    height=rh,
                    frame_height=bh + rh,
                    web_depth=wdepth,
                    web_cy=wcy,
                    collection=col,
                    eia_holes=False,
                    u_height=u_height,
                )
                lod1_rail_objs.append(lod1_parts["web"])
                lod1_rail_objs.extend(lod1_parts["flange_parts"])
            _bmesh_join(col_name + "_Rails_LOD1", lod1_rail_objs, col)
            col["rack_has_rail_lod"] = True

    elif not join_mesh:
        # Each part gets origin at base-front-centre for consistent export
        for obj in frame_objs + shell_objs + rail_objs:
            _set_origin_to(obj, (0.0, 0.0, 0.0))

    # ── Build return ───────────────────────────────────────────────────────
    final_objects = [o.name for o in col.objects]

    # Socket positions for UE5 (base-front-centre origin)
    sockets = {
        "SOCKET_RackFront": [0.0, 0.0,  bh + rh / 2],
        "SOCKET_RackTop":   [0.0, d / 2, tot],
        "SOCKET_RackBase":  [0.0, d / 2, 0.0],
    }

    has_shell = join_mesh and bool(shell_objs)
    return {
        "collection":        col_name,
        "objects":           final_objects,
        "joined":            join_mesh,
        "body_object":       col_name + "_Body"  if join_mesh else None,
        "shell_object":      col_name + "_Shell" if has_shell  else None,
        "rails_object":      col_name + "_Rails" if (join_mesh and has_rails) else None,
        "rails_lod1_object": col_name + "_Rails_LOD1" if (join_mesh and has_rails and lod_rails and eia_holes) else None,
        "quality":           quality,
        "sockets":           sockets,
        "u_height":          u_height,
        "external_dimensions_mm": {
            "width":  width_mm,
            "depth":  depth_mm,
            "height": round(tot * 1000, 1),
        },
        "interior_dimensions_mm": {
            "width":       round(EIA_RAIL_SPAN_MM, 1),
            "rail_height": round(rh * 1000, 1),
        },
        "origin":   "base-front-centre (0, 0, 0)",
        "warnings": warnings,
    }


# ── Tool 2: get_rack_u_position ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def get_rack_u_position(
    collection_name: str,
    u_slot: int,
    side: str = "front",
) -> Dict[str, Any]:
    """
    Return the world-space coordinates for a specific U slot in a rack.

    collection_name: name of the rack collection (must have rack metadata)
    u_slot:         U slot number (1 = bottom, u_height = top)
    side:           'front' | 'rear' — which mounting rail pair

    Returns the XYZ centre of the U slot opening and the four corner
    coordinates of the equipment mounting face.
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"Collection '{collection_name}' has no rack metadata — "
                         "create it with create_rack_cabinet first")

    u_height  = col["rack_u_height"]
    bh        = col["rack_base_height_m"]
    rh        = col["rack_rail_height_m"]
    hs        = col["rack_half_span_m"]
    depth_m   = col["rack_depth_mm"] / 1000.0
    rt        = RACK_RAIL_THICK_M
    setback_f = col.get("rack_setback_front_m", RACK_SETBACK_FRONT_M)
    setback_r = col.get("rack_setback_rear_m",  RACK_SETBACK_REAR_M)

    if u_slot < 1 or u_slot > u_height:
        raise ValueError(f"u_slot must be 1–{u_height}; got {u_slot}")

    # U slots are numbered from the BOTTOM (1 = lowest)
    slot_z_bottom = bh + (u_slot - 1) * RACK_U_M
    slot_z_top    = slot_z_bottom + RACK_U_M
    slot_z_centre = (slot_z_bottom + slot_z_top) / 2

    # Equipment origin = ear-back-face = chassis front face.
    # Front: ear back face sits against the flange FRONT face (Y = setback_f).
    # Rear:  ear back face sits against the rear flange FRONT face.
    if side.lower() == "front":
        y = setback_f                     # ear back flush with front flange face
    else:
        y = depth_m - setback_r - rt      # ear back flush with rear flange face

    return {
        "collection":    collection_name,
        "u_slot":        u_slot,
        "side":          side.lower(),
        "centre":        [0.0, y, round(slot_z_centre, 5)],
        "z_bottom":      round(slot_z_bottom, 5),
        "z_top":         round(slot_z_top, 5),
        "rail_x_left":   round(-hs, 5),
        "rail_x_right":  round(+hs, 5),
        "u_height_m":    RACK_U_M,
        "u_height_mm":   RACK_U_MM,
    }


# ── Tool 3: list_rack_collections ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def list_rack_collections() -> List[Dict[str, Any]]:
    """
    Return all collections in the scene that were created by create_rack_cabinet.

    Each entry includes the collection name and key rack parameters.
    """
    racks = []
    for col in bpy.data.collections:
        if col.get("is_rack_cabinet"):
            racks.append({
                "collection": col.name,
                "u_height":   col.get("rack_u_height"),
                "width_mm":   col.get("rack_width_mm"),
                "depth_mm":   col.get("rack_depth_mm"),
                "height_mm":  round(col.get("rack_total_height_m", 0) * 1000, 1),
            })
    return racks


# ── Tool 4: get_rack_info ─────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def get_rack_info(collection_name: str) -> Dict[str, Any]:
    """
    Return the full metadata for a rack collection.

    collection_name: name of a collection created by create_rack_cabinet
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    bh  = col["rack_base_height_m"]
    rh  = col["rack_rail_height_m"]
    th  = col["rack_top_height_m"]
    tot = col["rack_total_height_m"]

    return {
        "collection":      collection_name,
        "u_height":        col["rack_u_height"],
        "width_mm":        col["rack_width_mm"],
        "depth_mm":        col["rack_depth_mm"],
        "total_height_mm": round(tot * 1000, 1),
        "base_height_mm":  round(bh * 1000, 1),
        "rail_height_mm":  round(rh * 1000, 1),
        "top_height_mm":   round(th * 1000, 1),
        "eia_rail_span_mm": EIA_RAIL_SPAN_MM,
        "u_pitch_mm":      RACK_U_MM,
        "objects":         [o.name for o in col.objects],
        "object_count":    len(list(col.objects)),
    }


# ── Tool 5: snap_to_rack_u ────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def snap_to_rack_u(
    object_name: str,
    collection_name: str,
    u_slot: int,
    u_size: int = 1,
    side: str = "front",
    x_offset: float = 0.0,
) -> Dict[str, Any]:
    """
    Move an object to align with a specific U slot in a rack.

    Positions the object's origin at the centre of the U slot on the
    specified mounting face. Useful for placing equipment models into a
    rack layout for visualisation or export.

    object_name:      name of the object to reposition
    collection_name:  rack collection (must have rack metadata)
    u_slot:           lowest U slot the equipment occupies (1 = bottom)
    u_size:           number of U slots the equipment spans (default 1)
    side:             'front' | 'rear'
    x_offset:         lateral offset from centreline in metres (default 0.0)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    # Call directly — we're already on the main thread inside @thread_safe
    pos = get_rack_u_position(collection_name, u_slot, side)

    col    = bpy.data.collections.get(collection_name)
    bh     = col["rack_base_height_m"]
    u_height     = col.get("rack_u_height", 42)
    has_fan_tray = col.get("rack_has_fan_tray", False)

    # Equipment origin is at front-face-bottom-centre (Z=0 in local space),
    # so place it at z_bot — NOT the U-slot centre.
    z_bot  = bh + (u_slot - 1) * RACK_U_M

    # The fan tray sits at z_base = bh + rh (top of rail zone), which is exactly
    # where U{u_height} ends — so all u_height slots are fully usable.
    # Warn only if equipment would exceed the rail zone entirely.
    warnings = []
    if (u_slot + u_size - 1) > u_height:
        warnings.append(
            f"U{u_slot}+{u_size}U exceeds the rack's {u_height}U rail zone"
        )

    # Equipment origin is at front-face-bottom-centre (Y=0 local = ear back face).
    # Place at rail_y so the ear back sits flush against the rail front face;
    # the ear then protrudes 2 mm toward the aisle — correct real-world mounting.
    rail_y     = pos["centre"][1]
    mount_y    = rail_y

    # Convert rack-local position to world space using the rack Body's matrix_world.
    # This handles racks at any world position or Z rotation (e.g. 180° Row B).
    rack_body = bpy.data.objects.get(f"{collection_name}_Body")
    if rack_body:
        local_pos = mathutils.Vector((x_offset, mount_y, z_bot))
        obj.location = rack_body.matrix_world @ local_pos
        obj.rotation_euler.z = rack_body.rotation_euler.z
    else:
        obj.location.x = x_offset
        obj.location.y = mount_y
        obj.location.z = z_bot

    result = {
        "object":       object_name,
        "collection":   collection_name,
        "u_slot":       u_slot,
        "u_size":       u_size,
        "new_location": list(obj.location),
    }
    if warnings:
        result["warnings"] = warnings
    return result


# ── Tool 6: get_rack_rail_positions ───────────────────────────────────────

@mcp.tool()
@thread_safe
def get_rack_rail_positions(
    collection_name: str,
) -> Dict[str, Any]:
    """
    Return the world-space positions of all U-slot centres on each rail.

    Output is structured for direct use as a UE5 DataTable or JSON config.
    Includes both front and rear rail positions for every U slot.
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    u_height = col["rack_u_height"]
    bh       = col["rack_base_height_m"]
    hs       = col["rack_half_span_m"]
    depth_m  = col["rack_depth_mm"] / 1000.0
    ps_m     = col["rack_post_size_mm"] / 1000.0

    y_front = ps_m / 2
    y_rear  = depth_m - ps_m / 2

    slots = []
    for u in range(1, u_height + 1):
        z_bot = bh + (u - 1) * RACK_U_M
        z_ctr = z_bot + RACK_U_M / 2
        slots.append({
            "u_slot":          u,
            "z_bottom_m":      round(z_bot, 5),
            "z_centre_m":      round(z_ctr, 5),
            "front_left":      [round(-hs, 5), round(y_front, 5), round(z_ctr, 5)],
            "front_right":     [round(+hs, 5), round(y_front, 5), round(z_ctr, 5)],
            "rear_left":       [round(-hs, 5), round(y_rear, 5),  round(z_ctr, 5)],
            "rear_right":      [round(+hs, 5), round(y_rear, 5),  round(z_ctr, 5)],
        })

    return {
        "collection": collection_name,
        "u_height":   u_height,
        "u_pitch_m":  RACK_U_M,
        "eia_rail_span_m": EIA_RAIL_SPAN_M,
        "slots":      slots,
    }


# ── Tool 7: add_rack_blanking_panel ───────────────────────────────────────

@mcp.tool()
@thread_safe
def add_rack_blanking_panel(
    collection_name: str,
    u_slot: int,
    u_size: int = 1,
    name: str = "",
) -> Dict[str, Any]:
    """
    Add a sheet-metal blanking panel at the specified U slot in a rack.

    Blanking panels fill unused rack space for airflow management.
    The panel is added to the rack's collection and positioned at the
    correct U slot using rack metadata.

    collection_name: rack collection name
    u_slot:         U slot to fill (1 = bottom)
    u_size:         number of U slots to cover (default 1)
    name:           optional panel object name (auto-generated if empty)
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    u_height = col["rack_u_height"]
    if u_slot < 1 or (u_slot + u_size - 1) > u_height:
        raise ValueError(
            f"u_slot {u_slot} + u_size {u_size} exceeds rack u_height {u_height}"
        )

    bh      = col["rack_base_height_m"]
    depth_m = col["rack_depth_mm"] / 1000.0
    hs      = col["rack_half_span_m"]
    st      = col.get("rack_sheet_thick_mm", RACK_SHEET_THICK_MM) / 1000.0

    panel_h = u_size * RACK_U_M
    panel_w = EIA_RAIL_SPAN_M
    z_ctr   = bh + (u_slot - 1) * RACK_U_M + panel_h / 2
    y_ctr   = col["rack_post_size_mm"] / 1000.0 / 2  # flush with front posts

    panel_name = name or f"{collection_name}_blank_{u_slot}U"
    panel = _create_box_object(
        panel_name,
        cx=0.0, cy=y_ctr, cz=z_ctr,
        w=panel_w, d=st, h=panel_h,
        collection=col,
    )
    _set_origin_to(panel, (0.0, 0.0, 0.0))

    return {
        "object":     panel.name,
        "collection": collection_name,
        "u_slot":     u_slot,
        "u_size":     u_size,
        "location":   list(panel.location),
    }


# ── Tool 8: validate_rack_fitment ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def validate_rack_fitment(
    collection_name: str,
    u_slot: int,
    u_size: int,
    width_mm: float = 482.6,
    depth_mm: float = 0.0,
) -> Dict[str, Any]:
    """
    Check whether equipment of given dimensions fits in a rack at a specified slot.

    Does NOT move any objects — validation only.

    collection_name: rack to validate against
    u_slot:         starting U slot (1 = bottom)
    u_size:         number of U slots the equipment needs
    width_mm:       equipment width (default 482.6 = full 19" width)
    depth_mm:       equipment depth (0 = skip depth check)
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    u_height  = col["rack_u_height"]
    rack_w_mm = EIA_RAIL_SPAN_MM
    rack_d_mm = col["rack_depth_mm"] - col["rack_post_size_mm"]  # usable depth

    issues = []
    if u_slot < 1:
        issues.append(f"u_slot {u_slot} is below the rack floor (minimum 1)")
    if u_slot + u_size - 1 > u_height:
        issues.append(
            f"Equipment spans U{u_slot}–U{u_slot + u_size - 1} but rack only has {u_height}U"
        )
    if width_mm > rack_w_mm:
        issues.append(
            f"Equipment width {width_mm} mm exceeds EIA rail span {rack_w_mm} mm"
        )
    if depth_mm > 0 and depth_mm > rack_d_mm:
        issues.append(
            f"Equipment depth {depth_mm} mm exceeds usable rack depth {rack_d_mm:.1f} mm"
        )

    return {
        "collection":    collection_name,
        "u_slot":        u_slot,
        "u_size":        u_size,
        "fits":          len(issues) == 0,
        "issues":        issues,
        "rack_u_height": u_height,
        "rack_width_mm": rack_w_mm,
        "rack_depth_mm": round(rack_d_mm, 1),
    }


# ── Tool 9: export_rack_layout_json ───────────────────────────────────────

@mcp.tool()
@thread_safe
def export_rack_layout_json(
    collection_name: str,
    output_path: str = "",
) -> Dict[str, Any]:
    """
    Export rack metadata and U-slot rail positions as a JSON file.

    The JSON structure is compatible with UE5 DataTable import (array of
    row structs with RowName = U slot number).

    collection_name: rack collection to export
    output_path:     absolute path to write JSON (auto-generated if empty)
    """
    import tempfile

    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    positions = get_rack_rail_positions(collection_name)

    payload = {
        "rack": {
            "collection":    collection_name,
            "u_height":      col["rack_u_height"],
            "width_mm":      col["rack_width_mm"],
            "depth_mm":      col["rack_depth_mm"],
            "total_height_m": col["rack_total_height_m"],
            "eia_rail_span_m": EIA_RAIL_SPAN_M,
            "u_pitch_m":     RACK_U_M,
        },
        "slots": positions["slots"],
    }

    if not output_path:
        output_path = os.path.join(
            tempfile.gettempdir(),
            f"{collection_name}_layout.json",
        )

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    return {
        "output_path":  output_path,
        "collection":   collection_name,
        "slot_count":   len(positions["slots"]),
    }


# ── Tool 10: set_rack_material ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def set_rack_material(
    collection_name: str,
    color: Tuple[float, float, float, float] = (0.12, 0.12, 0.12, 1.0),
    metallic: float = 0.8,
    roughness: float = 0.4,
    material_name: str = "",
) -> Dict[str, Any]:
    """
    Apply a Principled BSDF material to all mesh objects in a rack collection.

    collection_name: rack collection
    color:           RGBA base colour in linear space (default dark grey)
    metallic:        metallic value 0.0–1.0 (default 0.8)
    roughness:       roughness value 0.0–1.0 (default 0.4)
    material_name:   name for the material (auto-generated from collection name)
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")

    mat_name = material_name or f"MAT_{collection_name}"
    mat = bpy.data.materials.get(mat_name)
    if not mat:
        mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value  = color
        bsdf.inputs["Metallic"].default_value    = metallic
        bsdf.inputs["Roughness"].default_value   = roughness

    assigned = []
    for obj in col.objects:
        if obj.type == 'MESH':
            if obj.data.materials:
                obj.data.materials[0] = mat
            else:
                obj.data.materials.append(mat)
            assigned.append(obj.name)

    return {
        "material":   mat_name,
        "collection": collection_name,
        "assigned_to": assigned,
    }


# ── Tool 11: duplicate_rack ───────────────────────────────────────────────

@mcp.tool()
@thread_safe
def duplicate_rack(
    source_collection: str,
    new_name: str = "",
    offset: Tuple[float, float, float] = (0.7, 0.0, 0.0),
) -> Dict[str, Any]:
    """
    Duplicate a rack collection and offset it in world space.

    Copies all mesh objects and rack metadata custom properties.
    The new rack is independent — changes to one don't affect the other.

    source_collection: name of the rack collection to duplicate
    new_name:          name for the new collection (auto-generated if empty)
    offset:            XYZ offset applied to all objects in the new collection (metres)
    """
    src_col = bpy.data.collections.get(source_collection)
    if not src_col:
        raise ValueError(f"Collection '{source_collection}' not found")
    if not src_col.get("is_rack_cabinet"):
        raise ValueError(f"'{source_collection}' is not a rack cabinet collection")

    # Build new collection name
    dst_name = new_name or f"{source_collection}_copy"
    idx = 1
    while dst_name in bpy.data.collections:
        dst_name = f"{new_name or source_collection}_copy.{idx:03d}"
        idx += 1

    dst_col = bpy.data.collections.new(dst_name)
    bpy.context.scene.collection.children.link(dst_col)

    # Copy custom properties (rack metadata)
    for k, v in src_col.items():
        dst_col[k] = v

    # Duplicate objects
    new_names = []
    for src_obj in src_col.objects:
        if src_obj.type != 'MESH':
            continue
        new_obj = src_obj.copy()
        new_obj.data = src_obj.data.copy()
        new_obj.location = (
            src_obj.location.x + offset[0],
            src_obj.location.y + offset[1],
            src_obj.location.z + offset[2],
        )
        dst_col.objects.link(new_obj)
        new_names.append(new_obj.name)

    return {
        "source":  source_collection,
        "new":     dst_name,
        "objects": new_names,
        "offset":  list(offset),
    }


# ── Tool 12: create_rack_row ──────────────────────────────────────────────
# NOTE: MCP registration lives in bay_tools.py — this is a local helper.

@thread_safe
def create_rack_row(
    row_name: str = "RackRow",
    count: int = 4,
    u_height: int = 42,
    width_mm: float = 600.0,
    depth_mm: float = 1000.0,
    gap_mm: float = 0.0,
    axis: str = "X",
) -> Dict[str, Any]:
    """
    Generate a row of identical rack cabinets spaced along an axis.

    Each rack is created with create_rack_cabinet and placed side-by-side.
    Returns a list of collection names, one per rack.

    row_name:  base name for each rack (rack number appended automatically)
    count:     number of racks in the row (default 4)
    u_height:  U height for all racks
    width_mm:  rack width in mm
    depth_mm:  rack depth in mm
    gap_mm:    centre-to-centre gap between adjacent racks in mm (0 = flush)
    axis:      spacing axis — 'X' (default) or 'Y'
    """
    if count < 1:
        raise ValueError("count must be >= 1")
    if axis.upper() not in ("X", "Y"):
        raise ValueError("axis must be 'X' or 'Y'")

    stride_m = width_mm / 1000.0 + gap_mm / 1000.0
    collections = []

    for i in range(count):
        rack_name = f"{row_name}_{i + 1:02d}"
        result = create_rack_cabinet(
            name=rack_name,
            u_height=u_height,
            width_mm=width_mm,
            depth_mm=depth_mm,
            bracket_left=(i == 0),
            bracket_right=(i == count - 1),
        )
        col_name = result["collection"]
        col = bpy.data.collections.get(col_name)

        # Offset objects along the chosen axis
        offset_m = i * stride_m
        for obj in col.objects:
            if axis.upper() == "X":
                obj.location.x += offset_m
            else:
                obj.location.y += offset_m

        collections.append(col_name)

    return {
        "row_name":    row_name,
        "count":       count,
        "collections": collections,
        "stride_m":    stride_m,
        "axis":        axis.upper(),
    }


# ── Tool 13: delete_rack ──────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def delete_rack(collection_name: str) -> str:
    """
    Remove a rack collection and all its objects from the scene.

    This permanently deletes all mesh data in the collection.
    collection_name: name of the rack collection to remove
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")

    # Remove all objects in the collection
    for obj in list(col.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    # Unlink and remove the collection
    for parent in bpy.data.collections:
        if col.name in parent.children:
            parent.children.unlink(col)
    if col.name in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.unlink(col)
    bpy.data.collections.remove(col)

    return f"Rack '{collection_name}' deleted"


# ── Tool 14: update_rack_metadata ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def update_rack_metadata(
    collection_name: str,
    key: str,
    value: Any,
) -> Dict[str, Any]:
    """
    Update or add a custom property on a rack collection.

    Useful for storing project-specific metadata (asset ID, location label,
    power circuit, cabinet number) alongside the standard rack geometry data.

    collection_name: rack collection to update
    key:             property name (string)
    value:           property value (string, int, or float)
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")

    col[key] = value

    return {
        "collection": collection_name,
        "key":        key,
        "value":      value,
        "all_keys":   [k for k in col.keys()],
    }


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2 — EIA-310 HOLES, DOORS, CABLE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

# ── Tool 15: add_eia_holes_gn ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_eia_holes_gn(
    object_name: str,
    u_height: int = 42,
    bake: bool = False,
    mode: str = "instance",
) -> Dict[str, Any]:
    """
    Add EIA-310 mounting holes to a rail web object via a Geometry Nodes modifier.

    Distributes square hole geometry (9.525 mm) at the three standard offsets
    per U (0 / 15.88 / 28.57 mm) across the full rail height.

    mode='instance' (default):
        Adds thin square tile meshes at each hole position using Join Geometry.
        Tiles (9.525 × 1 mm × 9.525 mm) sit on the rail inner face as visual
        markers. No topology change to the rail — extremely lightweight and
        fully non-destructive. Hiding the modifier instantly restores the solid
        rail. Use this for viewport work, LOD generation, and any export that
        doesn't require physically cut openings.

    mode='boolean':
        Uses Mesh Boolean (DIFFERENCE) inside the GN tree to cut real voids
        through the rail web. Produces correct export geometry for close-up
        shots or hero assets.

        WHY Boolean instead of Instance on Points:
        'Instance on Points' places copies of a mesh AT positions — it cannot
        remove material from another mesh. Creating actual voids (holes you can
        see through) requires a Boolean Difference operation. There is no
        lighter GN alternative that produces real through-holes in mesh geometry.
        Use 'instance' mode for everything except final hero-asset export.

    object_name: name of a rail web mesh object (e.g. 'ServerRack_rail_LF_web')
    u_height:    number of rack units (controls hole count = u_height × 3)
    bake:        if True, apply the modifier after creation (default False);
                 required before UE5 FBX export regardless of mode
    mode:        'instance' (default, lightweight) | 'boolean' (real cuts)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    mode = mode.lower()
    if mode not in ("instance", "boolean"):
        raise ValueError("mode must be 'instance' or 'boolean'")

    mod_name = "EIA_Holes_GN"

    # Remove any existing EIA holes modifier on this object
    existing = obj.modifiers.get(mod_name)
    if existing:
        obj.modifiers.remove(existing)

    ng_name = f"EIA_Holes_{u_height}U_{mode}"
    ng = bpy.data.node_groups.get(ng_name)
    if ng:
        bpy.data.node_groups.remove(ng)

    ng = bpy.data.node_groups.new(name=ng_name, type='GeometryNodeTree')

    # ── Node group interface ───────────────────────────────────────────────
    # Blender 4.x uses ng.interface; older uses ng.inputs/outputs
    if hasattr(ng, "interface"):
        ng.interface.new_socket("Geometry", in_out='INPUT',  socket_type='NodeSocketGeometry')
        ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
        sock_u  = ng.interface.new_socket("U Height",    in_out='INPUT', socket_type='NodeSocketInt')
        sock_hs = ng.interface.new_socket("Hole Size M", in_out='INPUT', socket_type='NodeSocketFloat')
        sock_u.default_value  = u_height
        sock_hs.default_value = RACK_HOLE_SIZE_M
    else:
        ng.inputs.new('NodeSocketGeometry', 'Geometry')
        ng.outputs.new('NodeSocketGeometry', 'Geometry')
        sock_u  = ng.inputs.new('NodeSocketInt',   'U Height')
        sock_hs = ng.inputs.new('NodeSocketFloat', 'Hole Size M')
        sock_u.default_value  = u_height
        sock_hs.default_value = RACK_HOLE_SIZE_M

    nodes = ng.node_tree.nodes if hasattr(ng, "node_tree") else ng.nodes
    links = ng.node_tree.links if hasattr(ng, "node_tree") else ng.links

    node_in  = nodes.new('NodeGroupInput')
    node_out = nodes.new('NodeGroupOutput')
    node_in.location  = (-400, 0)
    node_out.location = (500, 0)

    # ── Hole tile mesh ─────────────────────────────────────────────────────
    # Instance mode: thin tile (1 mm depth) — visual marker, no topology change
    # Boolean mode:  full rail thickness + margin to guarantee clean cut
    tile_depth = 0.001 if mode == "instance" else (RACK_RAIL_THICK_M + 0.002)

    hole_mesh = nodes.new('GeometryNodeMeshCube')
    hole_mesh.location = (-200, -200)
    tile_scale = nodes.new('GeometryNodeTransform')
    tile_scale.location = (0, -200)
    tile_scale.inputs['Scale'].default_value = (RACK_HOLE_SIZE_M, tile_depth, RACK_HOLE_SIZE_M)
    links.new(hole_mesh.outputs['Mesh'], tile_scale.inputs['Geometry'])

    # ── Position a tile at every hole location (u_height × 3 positions) ───
    hole_geo_nodes = []
    for u in range(u_height):
        for offset_mm in RACK_HOLE_OFFSETS_MM:
            z = RACK_BASE_HEIGHT_M + u * RACK_U_M + offset_mm / 1000.0
            t = nodes.new('GeometryNodeTransform')
            t.location = (200, -len(hole_geo_nodes) * 40)
            t.inputs['Translation'].default_value = (0.0, 0.0, z)
            links.new(tile_scale.outputs['Geometry'], t.inputs['Geometry'])
            hole_geo_nodes.append(t)

    join = nodes.new('GeometryNodeJoinGeometry')
    join.location = (350, -200)
    for t in hole_geo_nodes:
        links.new(t.outputs['Geometry'], join.inputs['Geometry'])

    if mode == "instance":
        # Merge tile geometry with rail — tiles become part of the mesh output.
        # Hiding/removing this modifier restores the solid rail instantly.
        merge = nodes.new('GeometryNodeJoinGeometry')
        merge.location = (450, 0)
        links.new(node_in.outputs['Geometry'], merge.inputs['Geometry'])
        links.new(join.outputs['Geometry'],    merge.inputs['Geometry'])
        links.new(merge.outputs['Geometry'],   node_out.inputs['Geometry'])
    else:
        # Boolean Difference: subtract tile volumes from rail web.
        # This is the only GN approach that creates real through-holes.
        # Instance on Points places geometry AT positions; it cannot remove
        # material from another mesh — Boolean Difference is required.
        boolean = nodes.new('GeometryNodeMeshBoolean')
        boolean.location = (450, 0)
        boolean.operation = 'DIFFERENCE'
        links.new(node_in.outputs['Geometry'], boolean.inputs['Mesh 1'])
        links.new(join.outputs['Geometry'],    boolean.inputs['Mesh 2'])
        links.new(boolean.outputs['Mesh'],     node_out.inputs['Geometry'])

    mod = obj.modifiers.new(name=mod_name, type='NODES')
    mod.node_group = ng

    result = {
        "object":        object_name,
        "modifier":      mod_name,
        "node_group":    ng_name,
        "mode":          mode,
        "u_height":      u_height,
        "hole_count":    u_height * len(RACK_HOLE_OFFSETS_MM),
        "hole_size_mm":  RACK_HOLE_SIZE_M * 1000,
        "baked":         False,
    }

    if bake:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.modifier_apply(modifier=mod_name)
        result["baked"] = True

    return result


# ── Tool 16: remove_eia_holes_gn ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def remove_eia_holes_gn(object_name: str) -> Dict[str, Any]:
    """
    Remove the EIA-310 holes Geometry Nodes modifier from a rail object.

    Non-destructive — the original solid rail geometry is restored instantly.
    Does nothing if the modifier is not present.

    object_name: rail web object name
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    mod = obj.modifiers.get("EIA_Holes_GN")
    if not mod:
        return {"object": object_name, "removed": False, "message": "Modifier not found"}

    obj.modifiers.remove(mod)
    return {"object": object_name, "removed": True}


# ── Tool 17: apply_eia_holes_gn ──────────────────────────────────────────

@mcp.tool()
@thread_safe
def apply_eia_holes_gn(object_name: str) -> Dict[str, Any]:
    """
    Apply (bake) the EIA-310 holes modifier on a rail object.

    Required before UE5 FBX export — Geometry Nodes modifiers are not
    exported; the holes must be baked into the mesh data first.

    object_name: rail web object with an EIA_Holes_GN modifier
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    mod = obj.modifiers.get("EIA_Holes_GN")
    if not mod:
        raise ValueError(f"No EIA_Holes_GN modifier on '{object_name}' — run add_eia_holes_gn first")

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier="EIA_Holes_GN")

    return {
        "object":  object_name,
        "applied": True,
        "note":    "Holes baked into mesh — modifier removed",
    }


# ── Tool 18: create_rack_door ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_rack_door(
    collection_name: str,
    side: str = "front",
    vented: bool = False,
    name: str = "",
) -> Dict[str, Any]:
    """
    Create a detailed door panel (front or rear) for a rack cabinet.

    Builds a multi-part door joined into a single mesh: a 20 mm-deep outer
    frame (four perimeter strips), a recessed inner panel with a ~13 mm
    clearance pocket for server handles and cable-management rings, three
    hinge-knuckle blocks with pin-detail nubs on the left side, and a latch
    guard plate plus handle bar on the right side.

    Door origin is at the bottom hinge pin so UE5 blueprint door-open
    animations rotate around the correct pivot (rotate Z to open).

    Clearance: the inner panel face sits ≈ 7 mm in front of the rack face
    (front door), giving ≈ 2 mm gap beyond the 5 mm maximum handle protrusion
    from fully-loaded server chassis. Real pocket = 20 mm.

    collection_name: rack collection (must have rack metadata)
    side:            'front' | 'rear'
    vented:          mark for vent-pattern GN modifier (geometry not cut yet)
    name:            optional override for object name (auto-generated if empty)
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    side = side.lower()
    if side not in ("front", "rear"):
        raise ValueError("side must be 'front' or 'rear'")

    w   = col["rack_width_mm"] / 1000.0
    d   = col["rack_depth_mm"] / 1000.0
    bh  = col["rack_base_height_m"]
    rh  = col["rack_rail_height_m"]

    # ── Rack body world offset ────────────────────────────────────────────
    # Door parts are created as independent scene objects then baked into
    # a single mesh by _bmesh_join.  We must use world-space coordinates so
    # the joined mesh lands at the rack's actual world position, not the
    # origin.  Detect the Body object's world translation.
    rx, ry, rz = 0.0, 0.0, 0.0
    for _o in col.objects:
        if _o.type == 'MESH' and _o.name.endswith('_Body'):
            _t = _o.matrix_world.translation
            rx, ry, rz = float(_t.x), float(_t.y), float(_t.z)
            break

    # ── Door geometry constants ───────────────────────────────────────────
    FRAME_D      = 0.020   # total door depth from rack face to door outer face
    FRAME_W      = 0.030   # perimeter border strip width
    PANEL_THICK  = 0.003   # recessed panel thickness
    HINGE_W_EXT  = 0.020   # hinge knuckle outward width (protrudes past rack edge)
    HINGE_D      = 0.016   # hinge knuckle depth along door Y
    HINGE_H      = 0.048   # hinge knuckle height
    LATCH_W_EXT  = 0.022   # latch guard width
    LATCH_H      = 0.072   # latch guard height
    LATCH_BAR_D  = 0.014   # latch handle bar protrusion beyond door outer face

    panel_name = name or f"{collection_name}_door_{side}"
    parts: List[bpy.types.Object] = []

    # sign: -1 for front (aisle = -Y), +1 for rear (aisle = +Y)
    sign        = -1 if side == "front" else +1
    rack_face_y = 0.0 if side == "front" else d

    # Door Y reference positions (all in rack-local space)
    cy_frame = rack_face_y + sign * (FRAME_D / 2)
    cy_panel = rack_face_y + sign * (FRAME_D / 2 - PANEL_THICK / 2)
    cy_hinge = rack_face_y + sign * (FRAME_D / 2)
    cy_latch = rack_face_y + sign * (FRAME_D * 0.55)

    # ── Outer frame: four perimeter strips ───────────────────────────────
    # Left post
    parts.append(_create_box_object(
        f"{panel_name}_fr_L",
        cx=rx + (-(w / 2) + FRAME_W / 2), cy=ry + cy_frame, cz=rz + bh + rh / 2,
        w=FRAME_W, d=FRAME_D, h=rh,
        collection=col,
    ))
    # Right post
    parts.append(_create_box_object(
        f"{panel_name}_fr_R",
        cx=rx + (w / 2) - FRAME_W / 2, cy=ry + cy_frame, cz=rz + bh + rh / 2,
        w=FRAME_W, d=FRAME_D, h=rh,
        collection=col,
    ))
    # Top rail
    parts.append(_create_box_object(
        f"{panel_name}_fr_top",
        cx=rx + 0.0, cy=ry + cy_frame, cz=rz + bh + rh + FRAME_W / 2,
        w=w, d=FRAME_D, h=FRAME_W,
        collection=col,
    ))
    # Bottom rail
    parts.append(_create_box_object(
        f"{panel_name}_fr_bot",
        cx=rx + 0.0, cy=ry + cy_frame, cz=rz + bh - FRAME_W / 2,
        w=w, d=FRAME_D, h=FRAME_W,
        collection=col,
    ))

    # ── Recessed inner panel ──────────────────────────────────────────────
    # Sits at half the frame depth — creates the ~10 mm handle clearance pocket.
    inner_w = w - 2 * FRAME_W - 0.004
    inner_h = rh - 0.004
    parts.append(_create_box_object(
        f"{panel_name}_panel",
        cx=rx + 0.0, cy=ry + cy_panel, cz=rz + bh + rh / 2,
        w=inner_w, d=PANEL_THICK, h=inner_h,
        collection=col,
    ))

    # ── Hinge knuckle blocks (3, left exterior) ───────────────────────────
    # Each hinge = outer knuckle body + smaller pin-detail nub
    cx_hinge = -(w / 2) + HINGE_W_EXT / 2
    for i, rel_pos in enumerate(HINGE_POSITIONS):
        hz = bh + rh * rel_pos
        parts.append(_create_box_object(
            f"{panel_name}_hng_{i}_body",
            cx=rx + cx_hinge, cy=ry + cy_hinge, cz=rz + hz,
            w=HINGE_W_EXT, d=HINGE_D, h=HINGE_H,
            collection=col,
        ))
        parts.append(_create_box_object(
            f"{panel_name}_hng_{i}_pin",
            cx=rx + cx_hinge, cy=ry + cy_hinge + sign * (HINGE_D * 0.25),
            cz=rz + hz,
            w=HINGE_W_EXT * 0.45, d=HINGE_D * 0.55, h=HINGE_H * 0.22,
            collection=col,
        ))

    # ── Latch guard plate + handle bar (right exterior) ───────────────────
    cx_latch = (w / 2) - LATCH_W_EXT / 2
    parts.append(_create_box_object(
        f"{panel_name}_latch_guard",
        cx=rx + cx_latch, cy=ry + cy_latch, cz=rz + bh + rh * 0.50,
        w=LATCH_W_EXT, d=FRAME_D * 0.40, h=LATCH_H,
        collection=col,
    ))
    parts.append(_create_box_object(
        f"{panel_name}_latch_bar",
        cx=rx + cx_latch,
        cy=ry + rack_face_y + sign * (FRAME_D + LATCH_BAR_D / 2),
        cz=rz + bh + rh * 0.50,
        w=LATCH_W_EXT * 0.55, d=LATCH_BAR_D, h=LATCH_H * 0.44,
        collection=col,
    ))

    # ── Join all parts into single door mesh ──────────────────────────────
    # Use rack_tools' bmesh-based join (avoids bpy.ops.object.join context req)
    joined = _bmesh_join(panel_name, parts, col)

    # ── UE5 animation empties ─────────────────────────────────────────────
    hinge_y       = rack_face_y + sign * (FRAME_D / 2)
    hinge_empties = []
    for i, rel_pos in enumerate(HINGE_POSITIONS):
        hz = bh + rh * rel_pos
        e  = bpy.data.objects.new(f"{panel_name}_hinge_attach_{i}", None)
        e.empty_display_type = 'ARROWS'
        e.empty_display_size = 0.02
        e.location = (rx + (-(w / 2) + ANCHOR_INSET_M), ry + hinge_y, rz + hz)
        col.objects.link(e)
        e.parent = joined
        e.matrix_parent_inverse = joined.matrix_world.inverted()
        hinge_empties.append(e.name)

    latch_e = bpy.data.objects.new(f"{panel_name}_latch_socket", None)
    latch_e.empty_display_type = 'ARROWS'
    latch_e.empty_display_size = 0.02
    latch_e.location = (rx + (w / 2) - ANCHOR_INSET_M, ry + hinge_y, rz + bh + rh * 0.50)
    col.objects.link(latch_e)
    latch_e.parent = joined
    latch_e.matrix_parent_inverse = joined.matrix_world.inverted()

    # ── Origin at bottom hinge pin (UE5 door-open pivot) ──────────────────
    hz_bottom = bh + rh * HINGE_POSITIONS[0]
    _set_origin_to(joined, (rx + (-(w / 2) + ANCHOR_INSET_M), ry + hinge_y, rz + hz_bottom))

    # ── Door material — solid metallic steel, EEVEE-Next compatible ───────
    mat = bpy.data.materials.get("M_Door_Steel")
    if mat is None:
        mat = bpy.data.materials.new("M_Door_Steel")
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = (0.35, 0.35, 0.38, 1.0)
            bsdf.inputs["Metallic"].default_value = 0.9
            bsdf.inputs["Roughness"].default_value = 0.35
            bsdf.inputs["Alpha"].default_value = 1.0
    # BLENDED renders as solid opaque metal in EEVEE-Next; DITHERED renders
    # as near-invisible hash noise even at alpha=1.0.
    mat.surface_render_method = 'BLENDED'
    if joined.data.materials:
        joined.data.materials[0] = mat
    else:
        joined.data.materials.append(mat)

    if vented:
        joined["door_vented"] = True

    return {
        "object":         panel_name,
        "collection":     collection_name,
        "side":           side,
        "width_mm":       round(w * 1000, 1),
        "height_mm":      round(rh * 1000, 1),
        "depth_mm":       round(FRAME_D * 1000, 1),
        "pocket_mm":      round(FRAME_D * 500, 1),
        "hinge_empties":  hinge_empties,
        "latch_empty":    latch_e.name,
        "origin":         "bottom hinge pin",
        "vented":         vented,
    }


# ── Tool 19: create_rack_doors ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_rack_doors(
    collection_name: str,
    vented_front: bool = False,
    vented_rear: bool = False,
) -> Dict[str, Any]:
    """
    Create both front and rear door panels for a rack cabinet in one call.

    Convenience wrapper around create_rack_door that adds both doors and
    returns their names together.

    collection_name: rack collection (must have rack metadata)
    vented_front:    mark front door for vent pattern (default False)
    vented_rear:     mark rear door for vent pattern (default True for airflow)
    """
    front = create_rack_door(collection_name, side="front", vented=vented_front)
    rear  = create_rack_door(collection_name, side="rear",  vented=vented_rear)

    return {
        "collection":  collection_name,
        "front_door":  front["object"],
        "rear_door":   rear["object"],
        "front_vented": vented_front,
        "rear_vented":  vented_rear,
    }


# ── Tool 20: open_rack_door ───────────────────────────────────────────────

@mcp.tool()
@thread_safe
def open_rack_door(
    object_name: str,
    angle_deg: float = 90.0,
) -> Dict[str, Any]:
    """
    Rotate a rack door around its hinge axis (Z axis at the door origin).

    The door origin is set at the bottom hinge pin by create_rack_door, so
    rotation around Z produces a correct swing-open motion. Use this for
    visualisation or to set the default open pose for UE5 animations.

    object_name: door panel object name
    angle_deg:   rotation angle in degrees (90 = fully open, 0 = closed)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    obj.rotation_euler.z = math.radians(angle_deg)

    return {
        "object":     object_name,
        "angle_deg":  angle_deg,
        "rotation_z": obj.rotation_euler.z,
    }


# ── Tool 21: add_door_vent_pattern ────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_door_vent_pattern(
    object_name: str,
    slot_w_m: float = DOOR_VENT_SLOT_W_M,
    slot_h_m: float = DOOR_VENT_SLOT_H_M,
    gap_x_m: float  = DOOR_VENT_GAP_X_M,
    gap_y_m: float  = DOOR_VENT_GAP_Y_M,
    margin_m: float = DOOR_VENT_MARGIN_M,
) -> Dict[str, Any]:
    """
    Add a parametric vent slot pattern to a door panel via Geometry Nodes.

    Creates a GN modifier that instances rectangular vent slot cutouts across
    the door face in a regular grid, respecting edge margins. Non-destructive
    — the modifier can be removed or adjusted at any time.

    The pattern uses Mesh Boolean (DIFFERENCE) inside GN, so the slots are
    real geometry cuts when the modifier is applied (baked) for export.

    object_name: door panel mesh object
    slot_w_m:   vent slot width in metres (default 10 mm)
    slot_h_m:   vent slot height in metres (default 50 mm)
    gap_x_m:    horizontal gap between slots (default 12 mm)
    gap_y_m:    vertical gap between slots (default 8 mm)
    margin_m:   edge margin — no slots within this distance of edge (default 40 mm)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    mod_name = "DoorVent_GN"
    existing = obj.modifiers.get(mod_name)
    if existing:
        obj.modifiers.remove(existing)

    ng_name = f"DoorVent_{object_name}"
    ng = bpy.data.node_groups.get(ng_name)
    if ng:
        bpy.data.node_groups.remove(ng)

    ng = bpy.data.node_groups.new(name=ng_name, type='GeometryNodeTree')

    if hasattr(ng, "interface"):
        ng.interface.new_socket("Geometry", in_out='INPUT',  socket_type='NodeSocketGeometry')
        ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    else:
        ng.inputs.new('NodeSocketGeometry', 'Geometry')
        ng.outputs.new('NodeSocketGeometry', 'Geometry')

    nodes = ng.node_tree.nodes if hasattr(ng, "node_tree") else ng.nodes
    links = ng.node_tree.links if hasattr(ng, "node_tree") else ng.links

    node_in  = nodes.new('NodeGroupInput')
    node_out = nodes.new('NodeGroupOutput')
    node_in.location  = (-400, 0)
    node_out.location = (600, 0)

    # Single slot mesh (cube sized to slot dimensions, full door depth for clean cut)
    slot_mesh = nodes.new('GeometryNodeMeshCube')
    slot_mesh.location = (-200, -200)

    slot_scale = nodes.new('GeometryNodeTransform')
    slot_scale.location = (0, -200)
    # depth (Y) slightly oversized to guarantee clean boolean cut through 2 mm sheet
    slot_scale.inputs['Scale'].default_value = (slot_w_m, 0.006, slot_h_m)
    links.new(slot_mesh.outputs['Mesh'], slot_scale.inputs['Geometry'])

    # Distribute on a grid using individual Transform nodes
    # Compute grid extents from object bounding box
    bb = [obj.matrix_world @ v.co for v in obj.data.vertices] if obj.data.vertices else []
    if bb:
        xs = [v.x for v in bb]; ys_z = [v.z for v in bb]
        x_min, x_max = min(xs) + margin_m, max(xs) - margin_m
        z_min, z_max = min(ys_z) + margin_m, max(ys_z) - margin_m
    else:
        x_min, x_max = -0.2, 0.2
        z_min, z_max = 0.1, 1.8

    step_x = slot_w_m + gap_x_m
    step_z = slot_h_m + gap_y_m
    slot_nodes = []
    x = x_min + slot_w_m / 2
    while x + slot_w_m / 2 <= x_max:
        z = z_min + slot_h_m / 2
        while z + slot_h_m / 2 <= z_max:
            t = nodes.new('GeometryNodeTransform')
            t.location = (200, -len(slot_nodes) * 40)
            t.inputs['Translation'].default_value = (x, 0.0, z)
            links.new(slot_scale.outputs['Geometry'], t.inputs['Geometry'])
            slot_nodes.append(t)
            z += step_z
        x += step_x

    if slot_nodes:
        join = nodes.new('GeometryNodeJoinGeometry')
        join.location = (400, -200)
        for t in slot_nodes:
            links.new(t.outputs['Geometry'], join.inputs['Geometry'])

        boolean = nodes.new('GeometryNodeMeshBoolean')
        boolean.location = (500, 0)
        boolean.operation = 'DIFFERENCE'
        links.new(node_in.outputs['Geometry'],  boolean.inputs['Mesh 1'])
        links.new(join.outputs['Geometry'],     boolean.inputs['Mesh 2'])
        links.new(boolean.outputs['Mesh'],      node_out.inputs['Geometry'])
    else:
        # No slots fit with given margin — pass through
        links.new(node_in.outputs['Geometry'], node_out.inputs['Geometry'])

    mod = obj.modifiers.new(name=mod_name, type='NODES')
    mod.node_group = ng

    return {
        "object":       object_name,
        "modifier":     mod_name,
        "slot_count":   len(slot_nodes),
        "slot_w_mm":    round(slot_w_m * 1000, 1),
        "slot_h_mm":    round(slot_h_m * 1000, 1),
        "margin_mm":    round(margin_m * 1000, 1),
        "note":         "Non-destructive — apply modifier before UE5 export",
    }


# ── Tool 22: add_brush_strip ──────────────────────────────────────────────
# NOTE: MCP registration lives in cable_tools.py — this is a local helper.

@thread_safe
def add_brush_strip(
    collection_name: str,
    u_slot: int,
    name: str = "",
) -> Dict[str, Any]:
    """
    Add a 1U brush strip panel at the specified U slot in a rack.

    A brush strip is a 1U blanking panel with a central cable entry stub
    (rubber fingers in physical hardware). Modelled as a panel + depth stub
    for UE5 asset representation. Used at the top/bottom of cable runs.

    collection_name: rack collection (must have rack metadata)
    u_slot:          U slot position (1 = bottom)
    name:            optional object name (auto-generated if empty)
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    u_height = col["rack_u_height"]
    if u_slot < 1 or u_slot > u_height:
        raise ValueError(f"u_slot must be 1–{u_height}")

    bh      = col["rack_base_height_m"]
    ps_m    = col["rack_post_size_mm"] / 1000.0
    st      = col.get("rack_sheet_thick_mm", RACK_SHEET_THICK_MM) / 1000.0

    z_ctr   = bh + (u_slot - 1) * RACK_U_M + BRUSH_STRIP_HEIGHT_M / 2
    y_panel = ps_m / 2   # flush with front post face
    y_stub  = ps_m / 2 + BRUSH_STRIP_DEPTH_M / 2

    obj_name = name or f"{collection_name}_brush_{u_slot:02d}U"

    # Front panel skin
    panel = _create_box_object(
        obj_name + "_panel",
        cx=0.0, cy=y_panel, cz=z_ctr,
        w=EIA_RAIL_SPAN_M, d=st, h=BRUSH_STRIP_HEIGHT_M,
        collection=col,
    )
    # Depth stub behind the panel (represents brush entry zone)
    stub = _create_box_object(
        obj_name + "_stub",
        cx=0.0, cy=y_stub, cz=z_ctr,
        w=EIA_RAIL_SPAN_M * 0.6, d=BRUSH_STRIP_DEPTH_M, h=BRUSH_STRIP_HEIGHT_M * 0.6,
        collection=col,
    )

    # Join panel + stub, origin at rack base-front-centre
    bpy.ops.object.select_all(action='DESELECT')
    panel.select_set(True)
    stub.select_set(True)
    bpy.context.view_layer.objects.active = panel
    bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = obj_name
    _set_origin_to(joined, (0.0, 0.0, 0.0))

    return {
        "object":      obj_name,
        "collection":  collection_name,
        "u_slot":      u_slot,
        "z_centre_m":  round(z_ctr, 5),
    }


# ── Tool 23: add_cable_entry_panel ────────────────────────────────────────
# NOTE: MCP registration lives in cable_tools.py — this is a local helper.

@thread_safe
def add_cable_entry_panel(
    collection_name: str,
    u_slot: int,
    u_size: int = 1,
    cutout_width_mm: float = 100.0,
    cutout_height_mm: float = 0.0,
    name: str = "",
) -> Dict[str, Any]:
    """
    Create a blanking panel with a rectangular cable pass-through cutout.

    The cutout is modelled as a recess (not a Boolean cut) — a thin frame
    panel with the centre removed. This gives UE5 a clean mesh without
    Boolean complexity.

    collection_name:  rack collection
    u_slot:           U slot position (1 = bottom)
    u_size:           number of U slots this panel spans (default 1)
    cutout_width_mm:  cutout width in mm (default 100)
    cutout_height_mm: cutout height in mm (0 = 70% of panel height)
    name:             optional object name
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    u_height = col["rack_u_height"]
    if u_slot < 1 or (u_slot + u_size - 1) > u_height:
        raise ValueError(f"u_slot {u_slot}+{u_size} out of range for {u_height}U rack")

    bh      = col["rack_base_height_m"]
    ps_m    = col["rack_post_size_mm"] / 1000.0
    st      = col.get("rack_sheet_thick_mm", RACK_SHEET_THICK_MM) / 1000.0

    panel_h  = u_size * RACK_U_M
    z_ctr    = bh + (u_slot - 1) * RACK_U_M + panel_h / 2
    y_ctr    = ps_m / 2

    cw = cutout_width_mm / 1000.0
    ch = (cutout_height_mm / 1000.0) if cutout_height_mm > 0 else panel_h * 0.7
    border_x = (EIA_RAIL_SPAN_M - cw) / 2
    border_z = (panel_h - ch) / 2

    obj_name = name or f"{collection_name}_cableentry_{u_slot:02d}U"

    parts = []
    # Left border strip
    parts.append(_create_box_object(
        obj_name + "_L",
        cx=-(cw / 2 + border_x / 2), cy=y_ctr, cz=z_ctr,
        w=border_x, d=st, h=panel_h, collection=col,
    ))
    # Right border strip
    parts.append(_create_box_object(
        obj_name + "_R",
        cx=+(cw / 2 + border_x / 2), cy=y_ctr, cz=z_ctr,
        w=border_x, d=st, h=panel_h, collection=col,
    ))
    # Top bar
    parts.append(_create_box_object(
        obj_name + "_T",
        cx=0.0, cy=y_ctr, cz=z_ctr + ch / 2 + border_z / 2,
        w=cw, d=st, h=border_z, collection=col,
    ))
    # Bottom bar
    parts.append(_create_box_object(
        obj_name + "_B",
        cx=0.0, cy=y_ctr, cz=z_ctr - ch / 2 - border_z / 2,
        w=cw, d=st, h=border_z, collection=col,
    ))

    bpy.ops.object.select_all(action='DESELECT')
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = obj_name
    _set_origin_to(joined, (0.0, 0.0, 0.0))

    return {
        "object":           obj_name,
        "collection":       collection_name,
        "u_slot":           u_slot,
        "u_size":           u_size,
        "cutout_width_mm":  round(cw * 1000, 1),
        "cutout_height_mm": round(ch * 1000, 1),
    }


# ── Tool 24: add_top_cable_tray ───────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_top_cable_tray(
    collection_name: str,
    name: str = "",
) -> Dict[str, Any]:
    """
    Create a cable tray in the top cap zone of a rack cabinet.

    Generates a shallow U-channel (base + two side walls) spanning the full
    cabinet width in the RACK_TOP_HEIGHT_M zone above the mounting rails.
    Used for horizontal cable routing across rack rows.

    collection_name: rack collection (must have rack metadata)
    name:            optional object name (auto-generated if empty)
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    w    = col["rack_width_mm"] / 1000.0
    d    = col["rack_depth_mm"] / 1000.0
    bh   = col["rack_base_height_m"]
    rh   = col["rack_rail_height_m"]
    th   = col["rack_top_height_m"]
    wt   = CABLE_TRAY_WALL_THICK_M
    td   = CABLE_TRAY_DEPTH_M

    # Tray sits in the lower half of the top cap zone
    z_base = bh + rh + wt / 2          # tray floor
    z_wall = bh + rh + wt / 2 + td / 2 # side wall centres

    obj_name = name or f"{collection_name}_cable_tray_top"
    parts = []

    # Tray floor
    parts.append(_create_box_object(
        obj_name + "_floor",
        cx=0.0, cy=d / 2, cz=z_base,
        w=w, d=d, h=wt, collection=col,
    ))
    # Left wall
    parts.append(_create_box_object(
        obj_name + "_wall_L",
        cx=-(w / 2 - wt / 2), cy=d / 2, cz=z_wall,
        w=wt, d=d, h=td, collection=col,
    ))
    # Right wall
    parts.append(_create_box_object(
        obj_name + "_wall_R",
        cx=+(w / 2 - wt / 2), cy=d / 2, cz=z_wall,
        w=wt, d=d, h=td, collection=col,
    ))

    bpy.ops.object.select_all(action='DESELECT')
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = obj_name
    _set_origin_to(joined, (0.0, 0.0, 0.0))

    return {
        "object":      obj_name,
        "collection":  collection_name,
        "tray_depth_mm":  round(td * 1000, 1),
        "wall_thick_mm":  round(wt * 1000, 1),
        "z_floor_m":      round(z_base, 5),
    }


# ── Tool 25: add_vertical_cable_manager ───────────────────────────────────
# NOTE: MCP registration lives in cable_tools.py — this is a local helper.

@thread_safe
def add_vertical_cable_manager(
    collection_name: str,
    side: str = "right",
    name: str = "",
) -> Dict[str, Any]:
    """
    Create a vertical cable management channel on the exterior side of a rack.

    Generates a U-channel (back + two sides) running the full interior height
    of the rack, mounted on the outside of the specified side panel. Used for
    routing patch cables, fibre runs, and power drops vertically.

    collection_name: rack collection (must have rack metadata)
    side:            'left' | 'right' — which side of the cabinet
    name:            optional object name (auto-generated if empty)
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")

    side = side.lower()
    if side not in ("left", "right"):
        raise ValueError("side must be 'left' or 'right'")

    w    = col["rack_width_mm"] / 1000.0
    d    = col["rack_depth_mm"] / 1000.0
    bh   = col["rack_base_height_m"]
    rh   = col["rack_rail_height_m"]
    st   = col.get("rack_sheet_thick_mm", RACK_SHEET_THICK_MM) / 1000.0
    cw   = VERT_CABLE_MGMT_WIDTH_M
    wt   = CABLE_TRAY_WALL_THICK_M

    sign = -1 if side == "left" else +1
    # Back plate: sits flush against the side panel exterior
    cx_back = sign * (w / 2 + cw / 2)
    cx_wall = sign * (w / 2 + wt / 2)

    z_ctr = bh + rh / 2

    obj_name = name or f"{collection_name}_vcm_{side}"
    parts = []

    # Back plate
    parts.append(_create_box_object(
        obj_name + "_back",
        cx=cx_back, cy=d / 2, cz=z_ctr,
        w=cw, d=d, h=rh, collection=col,
    ))
    # Front lip (closes the channel at front)
    parts.append(_create_box_object(
        obj_name + "_front_lip",
        cx=cx_wall, cy=wt / 2, cz=z_ctr,
        w=wt, d=wt, h=rh, collection=col,
    ))
    # Rear lip (closes the channel at rear)
    parts.append(_create_box_object(
        obj_name + "_rear_lip",
        cx=cx_wall, cy=d - wt / 2, cz=z_ctr,
        w=wt, d=wt, h=rh, collection=col,
    ))

    bpy.ops.object.select_all(action='DESELECT')
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = obj_name
    _set_origin_to(joined, (0.0, 0.0, 0.0))

    return {
        "object":      obj_name,
        "collection":  collection_name,
        "side":        side,
        "channel_width_mm": round(cw * 1000, 1),
        "height_mm":        round(rh * 1000, 1),
    }
