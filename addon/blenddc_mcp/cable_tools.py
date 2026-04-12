"""
Cable management geometry, routing, and export tools for the BlendDC asset pipeline.

Provides parametric cable management hardware (brush strips, vertical managers,
overhead trays, entry panels), curve-based cable routing between equipment sockets,
bundle generation for visual realism, and JSON export for UE5 spline/cable spawning.

Coordinate convention (matches rack_tools / equipment_tools):
  X = row axis (racks spaced along X)
  Y = depth   (0 = front face, positive = toward rear)
  Z = height  (positive = up)

Cable curves:
  Created as Blender NURBS curves with bevel_depth for tube thickness.
  Control points: start → sag-midpoint(s) → end.
  The 'cable_path' custom property on each curve object stores routing metadata
  so export_cable_data_json and validate_cable_routing can inspect any cable
  without parsing the full scene.
"""

import bpy
import bmesh
import json
import math
import os
import hashlib
import random as _random
from typing import Any, Dict, List, Optional, Tuple, Union

import mathutils

from core import mcp, thread_safe, _log
from constants import (
    RACK_U_M,
    RACK_BASE_HEIGHT_M,
    RACK_DEFAULT_DEPTH_MM,
    RACK_POST_SIZE_M,
    RACK_SHEET_THICK_M,
    EIA_RAIL_SPAN_M,
    RACK_INTERIOR_HEIGHT_M,
    SOCKET_PREFIX,
    BRUSH_STRIP_HEIGHT_M,
    BRUSH_STRIP_DEPTH_M,
    CABLE_ENTRY_CUTOUT_W_M,
    CABLE_ENTRY_CUTOUT_H_M,
    CABLE_TRAY_DEPTH_M,
    CABLE_TRAY_WALL_THICK_M,
    VERT_CABLE_MGMT_WIDTH_M,
    TRAPEZE_CEILING_PLATE_W_M, TRAPEZE_CEILING_PLATE_T_M,
    TRAPEZE_ROD_DIAM_M,
    TRAPEZE_BAR_H_M, TRAPEZE_BAR_T_M, TRAPEZE_BAR_OVERHANG_M,
)


# ── Cable type presets ─────────────────────────────────────────────────────
# Keyed by cable_type string → (R, G, B) linear colour, bevel_radius_m
_CABLE_PRESETS: Dict[str, Tuple[Tuple[float, float, float], float]] = {
    "cat6":    ((0.15, 0.15, 0.15), 0.003),   # grey, 3 mm radius
    "cat6a":   ((0.02, 0.40, 0.02), 0.003),   # green, 3 mm radius
    "fiber":   ((0.80, 0.65, 0.00), 0.002),   # yellow, 2 mm radius
    "power":   ((0.04, 0.04, 0.04), 0.005),   # black, 5 mm radius
    "kvm":     ((0.50, 0.10, 0.10), 0.003),   # dark red, 3 mm radius
    "custom":  ((0.15, 0.15, 0.15), 0.003),   # grey fallback
}

# Map cable_type → material_tools cable_type arg (material_tools only knows ethernet/power/fiber)
_MAT_TYPE_MAP: Dict[str, str] = {
    "cat6":   "ethernet",
    "cat6a":  "ethernet",
    "fiber":  "fiber",
    "power":  "power",
    "kvm":    "ethernet",
    "custom": "ethernet",
}


# ── Local geometry helpers ─────────────────────────────────────────────────

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


def _get_or_create_collection(name: str) -> bpy.types.Collection:
    col = bpy.data.collections.get(name)
    if not col:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return col


def _nest_collection(child: bpy.types.Collection, parent: bpy.types.Collection) -> None:
    if child.name not in parent.children:
        parent.children.link(child)
    scene_root = bpy.context.scene.collection
    if child.name in scene_root.children:
        scene_root.children.unlink(child)


def _add_socket_empty(
    name: str,
    location: Tuple[float, float, float],
    parent: bpy.types.Object,
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    full_name = name if name.startswith(SOCKET_PREFIX) else f"{SOCKET_PREFIX}{name}"
    existing  = bpy.data.objects.get(full_name)
    if existing:
        bpy.data.objects.remove(existing, do_unlink=True)
    e = bpy.data.objects.new(full_name, None)
    e.empty_display_type = 'ARROWS'
    e.empty_display_size = 0.012
    e.location = location
    collection.objects.link(e)
    e.parent = parent
    e.matrix_parent_inverse = parent.matrix_world.inverted()
    return e


def _rack_meta(collection_name: str) -> Tuple[
    bpy.types.Collection, float, float, float, float
]:
    """Return (collection, base_h, u_height, post_size_m, rack_depth_m) from rack metadata."""
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")
    if not col.get("is_rack_cabinet"):
        raise ValueError(f"'{collection_name}' is not a rack cabinet collection")
    bh        = float(col.get("rack_base_height_m",  RACK_BASE_HEIGHT_M))
    u_height  = int(col.get("rack_u_height",          42))
    ps_m      = float(col.get("rack_post_size_mm",    60.0)) / 1000.0
    depth_m   = float(col.get("rack_depth_mm",         RACK_DEFAULT_DEPTH_MM)) / 1000.0
    return col, bh, u_height, ps_m, depth_m


def _world_origin_of_rack(rack_col: bpy.types.Collection) -> mathutils.Vector:
    """Return world location of the rack's root empty / first parented object."""
    for obj in rack_col.all_objects:
        if obj.parent is None and obj.type in ('EMPTY', 'MESH'):
            return obj.location.copy()
    return mathutils.Vector((0.0, 0.0, 0.0))


def _socket_world_location(socket_name: str) -> Optional[mathutils.Vector]:
    """Return world location of a SOCKET_ empty by name (with or without prefix)."""
    full = socket_name if socket_name.startswith(SOCKET_PREFIX) else f"{SOCKET_PREFIX}{socket_name}"
    obj  = bpy.data.objects.get(full) or bpy.data.objects.get(socket_name)
    if obj:
        return obj.matrix_world.translation.copy()
    return None


def _ensure_cable_material(cable_type: str, color_override: Optional[str]) -> Optional[str]:
    """
    Ensure a cable material exists for this type. Returns the material name or None.
    Calls material_tools.create_cable_material inside a try/except so cable creation
    never fails just because the material module isn't loaded yet.
    """
    mat_name = f"MAT_Cable_{cable_type}"
    if bpy.data.materials.get(mat_name):
        return mat_name

    try:
        import material_tools as _mt
        preset_color, _ = _CABLE_PRESETS.get(cable_type, ((0.15, 0.15, 0.15), 0.003))

        # Parse optional hex color override (#RRGGBB)
        if color_override and color_override.startswith("#") and len(color_override) == 7:
            r = int(color_override[1:3], 16) / 255.0
            g = int(color_override[3:5], 16) / 255.0
            b = int(color_override[5:7], 16) / 255.0
            preset_color = (r, g, b)

        _mt.create_cable_material(
            name=mat_name,
            color=preset_color,
            cable_type=_MAT_TYPE_MAP.get(cable_type, "ethernet"),
        )
        return mat_name
    except Exception:
        return None


# ── Tool 1: add_brush_strip ────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_brush_strip(
    rack_name: str,
    u_slot: int = 1,
    color: str = "black",
    bristle_count: int = 20,
    random_variation: bool = False,
) -> Dict[str, Any]:
    """
    Add a 1U brush strip to a rack at the specified U slot.

    Creates a sheet-metal body box (1U × EIA span × 50 mm depth) with a row of
    thin bristle tiles across the front face — simulated brush strands for cable
    routing. Bristle tiles are joined into a single mesh with the body.

    Adds a SOCKET_Brush_XX empty at the front-centre of the bristle zone so UE5
    can spawn cable actors entering/exiting through the strip.

    rack_name:        rack cabinet collection name
    u_slot:           U slot to place the brush strip (1 = bottom of rack)
    color:            'black' | 'grey' (sets a base material tag)
    bristle_count:    number of bristle tile columns (default 20)
    random_variation: slightly randomize bristle heights for visual variety
    """
    col, bh, u_height, ps_m, depth_m = _rack_meta(rack_name)

    if u_slot < 1 or u_slot > u_height:
        raise ValueError(f"u_slot {u_slot} out of range for {u_height}U rack")

    # World origin of this rack (X, Y of rack base-front-centre)
    rack_origin = _world_origin_of_rack(col)
    rx, ry      = rack_origin.x, rack_origin.y

    cable_col_name = f"{rack_name}_CableMgmt"
    cable_col      = _get_or_create_collection(cable_col_name)

    strip_name   = f"{rack_name}_BrushStrip_U{u_slot:02d}"
    z_bottom     = bh + (u_slot - 1) * RACK_U_M
    z_centre     = z_bottom + BRUSH_STRIP_HEIGHT_M / 2
    body_depth   = BRUSH_STRIP_DEPTH_M
    y_centre     = ry + body_depth / 2
    base_color   = (0.04, 0.04, 0.04) if color == "black" else (0.35, 0.35, 0.35)

    parts: List[bpy.types.Object] = []

    # Body — full-width sheet metal channel
    body = _create_box_object(
        strip_name + "_body",
        cx=rx, cy=y_centre, cz=z_centre,
        w=EIA_RAIL_SPAN_M, d=body_depth, h=BRUSH_STRIP_HEIGHT_M,
        collection=cable_col,
    )
    parts.append(body)

    # Bristle tiles — thin rectangular columns across the front face
    bristle_w  = (EIA_RAIL_SPAN_M * 0.90) / bristle_count
    bristle_h  = BRUSH_STRIP_HEIGHT_M * 0.75
    bristle_d  = 0.012   # 12 mm depth — bristle stub

    rng = _random.Random(f"{rack_name}_brush_{u_slot}")

    for i in range(bristle_count):
        bx = rx - (EIA_RAIL_SPAN_M * 0.90 / 2) + i * bristle_w + bristle_w / 2
        bz = z_bottom + (BRUSH_STRIP_HEIGHT_M - bristle_h) / 2
        if random_variation:
            # Each bristle column slightly different height
            h_var = bristle_h * rng.uniform(0.70, 1.0)
            bz    = z_bottom + (BRUSH_STRIP_HEIGHT_M - h_var) / 2
        else:
            h_var = bristle_h

        bristle = _create_box_object(
            f"{strip_name}_bristle_{i:02d}",
            cx=bx, cy=ry + bristle_d / 2, cz=bz + h_var / 2,
            w=bristle_w * 0.60, d=bristle_d, h=h_var,
            collection=cable_col,
        )
        parts.append(bristle)

    # Join into single mesh
    bpy.ops.object.select_all(action='DESELECT')
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = strip_name

    # Tag and material hint
    joined["is_brush_strip"] = True
    joined["u_slot"]         = u_slot
    joined["cable_color"]    = color

    # Socket at front-centre of bristle zone
    socket = _add_socket_empty(
        f"Brush_{rack_name}_U{u_slot:02d}",
        location=(rx, ry, z_centre),
        parent=joined,
        collection=cable_col,
    )

    return {
        "object":    strip_name,
        "rack":      rack_name,
        "u_slot":    u_slot,
        "collection": cable_col_name,
        "socket":    socket.name,
    }


# ── Tool 2: add_vertical_cable_manager ────────────────────────────────────

@mcp.tool()
@thread_safe
def add_vertical_cable_manager(
    rack_name: str,
    side: str = "right",
    finger_ducts: int = 10,
    channel_width_mm: float = 80.0,
    with_cover: bool = False,
) -> Dict[str, Any]:
    """
    Add a full-height vertical cable manager to the left or right side post of a rack.

    Builds a U-channel body (width × 60 mm depth × interior rail height) with
    horizontal finger dividers spaced every ~2U. An optional snap-on cover adds
    a thin lid box along the full height.

    SOCKET_VCM_Top and SOCKET_VCM_Bottom empties mark the cable entry/exit
    endpoints for UE5 cable routing.

    rack_name:        rack cabinet collection name
    side:             'left' | 'right' — which side post to mount on
    finger_ducts:     number of horizontal finger divider plates (default 10)
    channel_width_mm: channel opening width in mm (default 80)
    with_cover:       add a snap-on lid panel over the channel opening
    """
    side = side.lower()
    if side not in ("left", "right"):
        raise ValueError("side must be 'left' or 'right'")

    col, bh, u_height, ps_m, depth_m = _rack_meta(rack_name)
    rack_origin = _world_origin_of_rack(col)
    rx, ry      = rack_origin.x, rack_origin.y

    cable_col_name = f"{rack_name}_CableMgmt"
    cable_col      = _get_or_create_collection(cable_col_name)

    vcm_name    = f"{rack_name}_VCM_{side.capitalize()}"
    ch_w        = channel_width_mm / 1000.0
    ch_d        = 0.060    # 60 mm depth (Y axis)
    ch_h        = RACK_INTERIOR_HEIGHT_M
    wall_t      = RACK_SHEET_THICK_M

    # X position: just outside the rack post, left or right
    half_span   = EIA_RAIL_SPAN_M / 2 + ps_m / 2
    sign        = 1.0 if side == "right" else -1.0
    cx          = rx + sign * (half_span + ch_w / 2 + wall_t)
    cy          = ry + ch_d / 2
    cz          = bh + ch_h / 2

    parts: List[bpy.types.Object] = []

    # Back wall
    back = _create_box_object(vcm_name + "_back",
        cx=cx, cy=ry + wall_t / 2, cz=cz,
        w=ch_w, d=wall_t, h=ch_h, collection=cable_col)
    parts.append(back)

    # Side walls (left/right of channel)
    for side_sign, suffix in ((-1, "L"), (1, "R")):
        sw = _create_box_object(vcm_name + f"_wall{suffix}",
            cx=cx + side_sign * (ch_w / 2 - wall_t / 2),
            cy=ry + ch_d / 2, cz=cz,
            w=wall_t, d=ch_d, h=ch_h, collection=cable_col)
        parts.append(sw)

    # Finger dividers — horizontal plates spaced evenly along channel height
    finger_spacing = ch_h / (finger_ducts + 1)
    for i in range(finger_ducts):
        fz = bh + finger_spacing * (i + 1)
        fd = _create_box_object(vcm_name + f"_finger_{i:02d}",
            cx=cx, cy=ry + ch_d / 2, cz=fz,
            w=ch_w - wall_t * 2, d=ch_d * 0.55, h=wall_t * 1.5,
            collection=cable_col)
        parts.append(fd)

    # Optional snap-on lid
    if with_cover:
        lid = _create_box_object(vcm_name + "_lid",
            cx=cx, cy=ry + ch_d + wall_t / 2, cz=cz,
            w=ch_w, d=wall_t, h=ch_h, collection=cable_col)
        parts.append(lid)

    # Join
    bpy.ops.object.select_all(action='DESELECT')
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = vcm_name

    joined["is_vcm"]        = True
    joined["vcm_side"]      = side
    joined["finger_ducts"]  = finger_ducts
    joined["with_cover"]    = with_cover

    # Entry/exit sockets
    sock_top = _add_socket_empty(f"VCM_{rack_name}_{side.capitalize()}_Top",
        location=(cx, ry, bh + ch_h), parent=joined, collection=cable_col)
    sock_bot = _add_socket_empty(f"VCM_{rack_name}_{side.capitalize()}_Bottom",
        location=(cx, ry, bh), parent=joined, collection=cable_col)

    return {
        "object":       vcm_name,
        "rack":         rack_name,
        "side":         side,
        "finger_ducts": finger_ducts,
        "with_cover":   with_cover,
        "collection":   cable_col_name,
        "sockets":      [sock_top.name, sock_bot.name],
    }


# ── Tool 3: add_overhead_cable_tray ───────────────────────────────────────

@mcp.tool()
@thread_safe
def add_overhead_cable_tray(
    tray_name: str = "CableTray",
    length_m: float = 3.0,
    width_mm: float = 200.0,
    height_mm: float = 100.0,
    bracket_interval_m: float = 1.2,
    with_lid: bool = False,
    start_x_m: float = 0.0,
    start_y_m: float = 0.0,
    z_m: float = 2.3,
    ceiling_height_m: float = 3.5,
    collection_name: str = "CableTrays",
) -> Dict[str, Any]:
    """
    Create a parametric overhead cable tray (U-channel ladder) running along a row.

    Builds a full U-channel profile (bottom plate + two side walls) along the X axis.
    Trapeze hangers (ceiling anchor plate → M8 all-thread rod → trapeze bar) are added
    at regular intervals. The tray is suspended from the ceiling — it does not float.

    tray_name:          base name for tray objects
    length_m:           run length in metres (X axis)
    width_mm:           inner channel width in mm (default 200)
    height_mm:          channel side wall height in mm (default 100)
    bracket_interval_m: spacing between trapeze hangers in metres (default 1.2; 0 = skip)
    with_lid:           add a flat lid panel over the channel opening (default False)
    start_x_m:          world X of tray start
    start_y_m:          world Y of tray centreline
    z_m:                world Z of tray bottom face (top-of-rack height)
    ceiling_height_m:   world Z of the ceiling where hangers attach (default 3.5 m)
    collection_name:    collection to place tray objects into
    """
    tray_col = _get_or_create_collection(collection_name)

    w       = width_mm  / 1000.0
    h       = height_mm / 1000.0
    wt      = CABLE_TRAY_WALL_THICK_M
    cx      = start_x_m + length_m / 2
    cy      = start_y_m
    cz_base = z_m

    parts: List[bpy.types.Object] = []

    # Bottom plate
    bot = _create_box_object(tray_name + "_bottom",
        cx=cx, cy=cy, cz=cz_base + wt / 2,
        w=length_m, d=w, h=wt, collection=tray_col)
    parts.append(bot)

    # Left side wall
    lw = _create_box_object(tray_name + "_wallL",
        cx=cx, cy=cy - w / 2 + wt / 2, cz=cz_base + h / 2,
        w=length_m, d=wt, h=h, collection=tray_col)
    parts.append(lw)

    # Right side wall
    rw = _create_box_object(tray_name + "_wallR",
        cx=cx, cy=cy + w / 2 - wt / 2, cz=cz_base + h / 2,
        w=length_m, d=wt, h=h, collection=tray_col)
    parts.append(rw)

    # Optional lid
    if with_lid:
        lid = _create_box_object(tray_name + "_lid",
            cx=cx, cy=cy, cz=cz_base + h + wt / 2,
            w=length_m, d=w, h=wt, collection=tray_col)
        parts.append(lid)

    # Trapeze hangers — ceiling plate + threaded rod + trapeze bar
    # Each hanger suspends the tray from the ceiling structure above.
    # Geometry: square ceiling plate → vertical all-thread rod → horizontal bar
    bracket_names: List[str] = []
    bar_span = w + 2 * TRAPEZE_BAR_OVERHANG_M  # bar wider than tray channel

    if bracket_interval_m and bracket_interval_m > 0:
        n_brackets = max(1, int(length_m / bracket_interval_m))
        for i in range(n_brackets + 1):
            bx = start_x_m + i * bracket_interval_m
            if bx > start_x_m + length_m + 0.001:
                break

            # Ceiling anchor plate — sits at ceiling height, centred over tray
            ceil_plate = _create_box_object(
                f"{tray_name}_hanger_{i:02d}_plate",
                cx=bx, cy=cy,
                cz=ceiling_height_m - TRAPEZE_CEILING_PLATE_T_M / 2,
                w=TRAPEZE_CEILING_PLATE_W_M,
                d=TRAPEZE_CEILING_PLATE_W_M,
                h=TRAPEZE_CEILING_PLATE_T_M,
                collection=tray_col,
            )
            ceil_plate["is_tray_bracket"] = True
            parts.append(ceil_plate)
            bracket_names.append(ceil_plate.name)

            # Vertical all-thread rod — from ceiling plate down to tray level
            rod_top_z = ceiling_height_m - TRAPEZE_CEILING_PLATE_T_M
            rod_bot_z = cz_base
            rod_len   = rod_top_z - rod_bot_z
            rod = _create_box_object(
                f"{tray_name}_hanger_{i:02d}_rod",
                cx=bx, cy=cy,
                cz=rod_bot_z + rod_len / 2,
                w=TRAPEZE_ROD_DIAM_M, d=TRAPEZE_ROD_DIAM_M, h=rod_len,
                collection=tray_col,
            )
            rod["is_tray_bracket"] = True
            parts.append(rod)
            bracket_names.append(rod.name)

            # Trapeze bar — horizontal bar at tray bottom, cradling the tray
            bar = _create_box_object(
                f"{tray_name}_hanger_{i:02d}_bar",
                cx=bx, cy=cy,
                cz=cz_base - TRAPEZE_BAR_H_M / 2,
                w=TRAPEZE_BAR_T_M, d=bar_span, h=TRAPEZE_BAR_H_M,
                collection=tray_col,
            )
            bar["is_tray_bracket"] = True
            parts.append(bar)
            bracket_names.append(bar.name)

    # Join all tray parts into one mesh
    bpy.ops.object.select_all(action='DESELECT')
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = tray_name

    joined["is_cable_tray"]    = True
    joined["tray_length_m"]    = round(length_m, 4)
    joined["tray_width_mm"]    = width_mm
    joined["tray_height_mm"]   = height_mm
    joined["with_lid"]         = with_lid
    joined["bracket_count"]    = len(bracket_names)

    return {
        "object":       tray_name,
        "collection":   collection_name,
        "length_m":     length_m,
        "width_mm":     width_mm,
        "height_mm":    height_mm,
        "with_lid":     with_lid,
        "hangers":           len(bracket_names) // 3,
        "hanger_objects":    len(bracket_names),
        "z_m":               z_m,
        "ceiling_height_m":  ceiling_height_m,
    }


# ── Tool 4: add_cable_entry_panel ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_cable_entry_panel(
    rack_name: str,
    u_slot: int = 1,
    u_size: int = 1,
    grommet_count: int = 6,
    grommet_diam_mm: float = 30.0,
    random_variation: bool = False,
) -> Dict[str, Any]:
    """
    Add a cable entry panel with grommet holes to a rack at the specified U slot.

    Creates a sheet-metal faceplate (u_size U × EIA span × 30 mm depth) with
    circular grommet holes represented as inset cylinder stubs. Each grommet gets
    a SOCKET_Grommet_XX empty at its centre for UE5 cable spawn assignment.

    rack_name:       rack cabinet collection name
    u_slot:          U slot for the bottom of this panel
    u_size:          panel height in U (1 or 2)
    grommet_count:   number of grommet holes (default 6)
    grommet_diam_mm: grommet inner diameter in mm (default 30)
    random_variation: slightly offset grommet spacing for visual variety
    """
    col, bh, u_height, ps_m, depth_m = _rack_meta(rack_name)

    if u_slot < 1 or u_slot + u_size - 1 > u_height:
        raise ValueError(f"U{u_slot}+{u_size} out of range for {u_height}U rack")

    rack_origin = _world_origin_of_rack(col)
    rx, ry      = rack_origin.x, rack_origin.y

    cable_col_name = f"{rack_name}_CableMgmt"
    cable_col      = _get_or_create_collection(cable_col_name)

    panel_name = f"{rack_name}_EntryPanel_U{u_slot:02d}"
    ph         = u_size * RACK_U_M
    panel_d    = 0.030
    z_bottom   = bh + (u_slot - 1) * RACK_U_M
    z_centre   = z_bottom + ph / 2
    y_centre   = ry + panel_d / 2

    parts: List[bpy.types.Object] = []
    sockets:  List[str] = []

    # Panel body
    body = _create_box_object(panel_name + "_body",
        cx=rx, cy=y_centre, cz=z_centre,
        w=EIA_RAIL_SPAN_M, d=panel_d, h=ph,
        collection=cable_col)
    parts.append(body)

    # Grommet stubs — cylindrical recesses approximated as thin-walled ring boxes
    grom_r     = grommet_diam_mm / 2000.0  # radius in metres
    grom_depth = 0.015
    spacing    = (EIA_RAIL_SPAN_M * 0.85) / grommet_count
    rng        = _random.Random(f"{rack_name}_grom_{u_slot}")

    for i in range(grommet_count):
        gx = rx - (EIA_RAIL_SPAN_M * 0.85 / 2) + i * spacing + spacing / 2
        gz = z_centre
        if random_variation:
            gx += rng.uniform(-spacing * 0.05, spacing * 0.05)

        # Outer ring
        outer = _create_box_object(f"{panel_name}_grom_outer_{i:02d}",
            cx=gx, cy=ry + grom_depth / 2, cz=gz,
            w=grom_r * 2 + 0.006, d=grom_depth, h=grom_r * 2 + 0.006,
            collection=cable_col)
        parts.append(outer)

        # Inner void approximation (slightly smaller box, same depth)
        inner = _create_box_object(f"{panel_name}_grom_inner_{i:02d}",
            cx=gx, cy=ry + grom_depth / 2 + 0.001, cz=gz,
            w=grom_r * 2, d=grom_depth + 0.002, h=grom_r * 2,
            collection=cable_col)
        parts.append(inner)

    # Join
    bpy.ops.object.select_all(action='DESELECT')
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    joined = bpy.context.active_object
    joined.name = panel_name
    joined["is_entry_panel"] = True
    joined["u_slot"]         = u_slot
    joined["u_size"]         = u_size
    joined["grommet_count"]  = grommet_count

    # Add grommet sockets
    for i in range(grommet_count):
        gx = rx - (EIA_RAIL_SPAN_M * 0.85 / 2) + i * spacing + spacing / 2
        sock = _add_socket_empty(f"Grommet_{rack_name}_U{u_slot:02d}_{i:02d}",
            location=(gx, ry, z_centre), parent=joined, collection=cable_col)
        sockets.append(sock.name)

    return {
        "object":         panel_name,
        "rack":           rack_name,
        "u_slot":         u_slot,
        "u_size":         u_size,
        "grommet_count":  grommet_count,
        "collection":     cable_col_name,
        "sockets":        sockets,
    }


# ── Tool 5: add_cable_endpoint_sockets ────────────────────────────────────

@mcp.tool()
@thread_safe
def add_cable_endpoint_sockets(
    object_name: str,
    cable_type: str = "power",
    count: int = 2,
    side: str = "rear",
    offset_x_m: float = 0.0,
    offset_z_m: float = 0.0,
    collection_name: str = "",
) -> Dict[str, Any]:
    """
    Bulk-add named SOCKET_Cable_XX empties to any equipment object.

    Groups sockets by cable_type and distributes them evenly across the
    rear (or front) face. These are the cable-pull endpoints UE5 uses to
    spawn and connect cable actors, distinct from the per-port sockets
    placed by equipment creators.

    object_name:     target equipment or rack object
    cable_type:      'power' | 'data' | 'fiber' | 'kvm' — used in socket name
    count:           number of sockets to add (default 2)
    side:            'rear' | 'front' — which face to place sockets on
    offset_x_m:      horizontal shift from object centre in metres
    offset_z_m:      vertical shift from object bottom in metres
    collection_name: collection for socket empties (defaults to object's collection)
    """
    side = side.lower()
    if side not in ("rear", "front"):
        raise ValueError("side must be 'rear' or 'front'")
    cable_type = cable_type.lower()

    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")

    if collection_name:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
    else:
        user_cols = list(obj.users_collection)
        if not user_cols:
            raise ValueError(f"Object '{object_name}' is not in any collection")
        col = user_cols[0]

    # Derive object dimensions from bounding box
    bb      = obj.bound_box
    xs      = [v[0] for v in bb]
    zs      = [v[2] for v in bb]
    ys      = [v[1] for v in bb]
    obj_w   = max(xs) - min(xs)
    obj_h   = max(zs) - min(zs)
    y_face  = max(ys) if side == "rear" else min(ys)

    spacing = obj_w / (count + 1) if count > 1 else 0.0
    z_sock  = min(zs) + obj_h * 0.5 + offset_z_m

    created: List[str] = []
    for i in range(count):
        x_sock = min(xs) + (i + 1) * spacing + offset_x_m if count > 1 else offset_x_m
        sock = _add_socket_empty(
            f"Cable_{cable_type.capitalize()}_{object_name}_{i:02d}",
            location=(x_sock, y_face, z_sock),
            parent=obj, collection=col,
        )
        created.append(sock.name)

    return {
        "object":      object_name,
        "cable_type":  cable_type,
        "side":        side,
        "count":       count,
        "sockets":     created,
    }


# ── Tool 6: create_cable_path ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_cable_path(
    cable_name: str = "Cable_001",
    source: Union[str, List[float]] = [0.0, 0.0, 0.0],
    destination: Union[str, List[float]] = [1.0, 0.0, 0.0],
    sag_m: float = 0.05,
    radius_mm: float = 3.0,
    segments: int = 12,
    cable_type: str = "cat6",
    color_hex: str = "",
    random_variation: bool = False,
    collection_name: str = "Cables",
) -> Dict[str, Any]:
    """
    Create a NURBS curve cable between two SOCKET_ empties or world positions.

    The curve has 'segments' control points distributed along a catenary-like
    arc — a straight line from source to destination bent downward by 'sag_m'
    at the midpoint. Additional random micro-offsets when random_variation=True
    prevent cables from looking perfectly identical.

    A bevel_depth of radius_mm/1000 gives the curve a tube cross-section.
    The cable_path custom property stores routing metadata for export and
    validation tools.

    Calls create_cable_material (from material_tools) to assign a type-matched
    material; falls back gracefully if material_tools isn't loaded.

    cable_name:       Blender object name for the curve
    source:           SOCKET_ object name (str) or world [x, y, z] position
    destination:      SOCKET_ object name (str) or world [x, y, z] position
    sag_m:            midpoint droop in metres (default 0.05 = 50 mm)
    radius_mm:        tube bevel radius in mm (default 3.0)
    segments:         number of curve control points for smoothness (default 12)
    cable_type:       'cat6' | 'cat6a' | 'fiber' | 'power' | 'kvm' | 'custom'
    color_hex:        optional #RRGGBB hex override for the cable material
    random_variation: add micro-jitter to control points for visual variety
    collection_name:  collection to place the curve into
    """
    cable_type = cable_type.lower()
    if cable_type not in _CABLE_PRESETS:
        cable_type = "custom"

    _, bevel_default = _CABLE_PRESETS[cable_type]
    bevel_r = radius_mm / 1000.0

    # Resolve source/destination to world positions
    if isinstance(source, str):
        src_loc = _socket_world_location(source)
        if src_loc is None:
            raise ValueError(f"Socket '{source}' not found in scene")
        src_name = source if source.startswith(SOCKET_PREFIX) else f"{SOCKET_PREFIX}{source}"
    else:
        src_loc  = mathutils.Vector(source)
        src_name = str(source)

    if isinstance(destination, str):
        dst_loc = _socket_world_location(destination)
        if dst_loc is None:
            raise ValueError(f"Socket '{destination}' not found in scene")
        dst_name = destination if destination.startswith(SOCKET_PREFIX) else f"{SOCKET_PREFIX}{destination}"
    else:
        dst_loc  = mathutils.Vector(destination)
        dst_name = str(destination)

    # Build control points: parametric catenary arc
    # t in [0, 1] → lerp + sinusoidal sag at midpoint
    rng = _random.Random(cable_name) if random_variation else None

    seg_count = max(4, segments)
    coords: List[Tuple[float, float, float, float]] = []  # (x, y, z, w)

    for i in range(seg_count):
        t   = i / (seg_count - 1)
        pos = src_loc.lerp(dst_loc, t)
        # Sag: maximum droop at t=0.5 — sin curve ensures smooth catenary shape
        sag_factor = math.sin(t * math.pi) * sag_m
        pos.z -= sag_factor

        if rng:
            # Micro-jitter on intermediate points (not first/last)
            if 0 < i < seg_count - 1:
                jitter_scale = sag_m * 0.15
                pos.x += rng.uniform(-jitter_scale, jitter_scale)
                pos.y += rng.uniform(-jitter_scale, jitter_scale)
                pos.z += rng.uniform(-jitter_scale * 0.5, jitter_scale * 0.5)

        coords.append((pos.x, pos.y, pos.z, 1.0))

    # Create NURBS curve object
    curve_data = bpy.data.curves.new(cable_name, type='CURVE')
    curve_data.dimensions      = '3D'
    curve_data.bevel_depth     = bevel_r
    curve_data.bevel_resolution = 4
    curve_data.use_fill_caps   = True

    spline = curve_data.splines.new('NURBS')
    spline.points.add(seg_count - 1)  # spline starts with 1 point
    for i, (x, y, z, w) in enumerate(coords):
        spline.points[i].co = (x, y, z, w)
    spline.use_endpoint_u = True
    spline.order_u = min(4, seg_count)

    cable_obj = bpy.data.objects.new(cable_name, curve_data)
    cable_col = _get_or_create_collection(collection_name)
    cable_col.objects.link(cable_obj)

    # Store routing metadata
    cable_obj["cable_path"]   = True
    cable_obj["cable_type"]   = cable_type
    cable_obj["cable_source"] = src_name
    cable_obj["cable_dest"]   = dst_name
    cable_obj["sag_m"]        = round(sag_m, 4)
    cable_obj["radius_mm"]    = radius_mm
    cable_obj["segments"]     = seg_count

    length_m = round((dst_loc - src_loc).length + sag_m * 1.2, 4)
    cable_obj["cable_length_m"] = length_m

    # Assign material
    mat_name = _ensure_cable_material(cable_type, color_hex if color_hex else None)
    if mat_name and bpy.data.materials.get(mat_name):
        cable_obj.data.materials.append(bpy.data.materials[mat_name])

    return {
        "object":       cable_name,
        "collection":   collection_name,
        "cable_type":   cable_type,
        "source":       src_name,
        "destination":  dst_name,
        "sag_m":        sag_m,
        "radius_mm":    radius_mm,
        "segments":     seg_count,
        "length_m":     length_m,
        "material":     mat_name,
    }


# ── Tool 7: route_cables_between_racks ────────────────────────────────────

@mcp.tool()
@thread_safe
def route_cables_between_racks(
    rack_a: str,
    rack_b: str,
    cable_type: str = "cat6",
    max_cables: int = 4,
    tray_z_m: float = 0.0,
    random_variation: bool = False,
    collection_name: str = "Cables",
) -> Dict[str, Any]:
    """
    Connect matching SOCKET_ empties on two racks with cable curves.

    Finds SOCKET_ empties whose names contain the cable_type suffix on both
    racks. For each matching pair (up to max_cables), creates a three-waypoint
    cable: vertical rise from source socket to tray height, horizontal run
    across to the target rack X, then drop down to destination socket.

    tray_z_m = 0 auto-detects from bay metadata (rack top height + 0.1 m).
    Cable routing goes up → across → down rather than a direct straight line,
    matching real cable tray practice.

    rack_a:           first rack collection name
    rack_b:           second rack collection name
    cable_type:       cable type — filters matching sockets and sets material
    max_cables:       maximum number of cable pairs to connect (default 4)
    tray_z_m:         overhead tray Z height; 0 = auto from rack height
    random_variation: randomize sag and micro-jitter on each cable
    collection_name:  collection to place cable curve objects into
    """
    col_a = bpy.data.collections.get(rack_a)
    col_b = bpy.data.collections.get(rack_b)
    if not col_a:
        raise ValueError(f"Rack collection '{rack_a}' not found")
    if not col_b:
        raise ValueError(f"Rack collection '{rack_b}' not found")

    # Auto tray height from rack metadata
    if tray_z_m == 0:
        u_a = int(col_a.get("rack_u_height", 42))
        bh  = float(col_a.get("rack_base_height_m", RACK_BASE_HEIGHT_M))
        tray_z_m = bh + u_a * RACK_U_M + float(col_a.get("rack_top_height_m", 0.073)) + 0.10

    # Collect sockets from each rack that match the cable_type keyword
    type_token = cable_type.lower()

    def _collect_sockets(rack_col: bpy.types.Collection) -> List[bpy.types.Object]:
        results: List[bpy.types.Object] = []
        for obj in rack_col.all_objects:
            if obj.type == 'EMPTY' and SOCKET_PREFIX in obj.name:
                if type_token in obj.name.lower():
                    results.append(obj)
        return results

    socks_a = _collect_sockets(col_a)
    socks_b = _collect_sockets(col_b)

    pairs = list(zip(socks_a, socks_b))[:max_cables]
    if not pairs:
        return {
            "rack_a": rack_a, "rack_b": rack_b,
            "cables_created": 0,
            "note": "No matching SOCKET_ empties found for cable_type filter",
        }

    cable_col  = _get_or_create_collection(collection_name)
    created:   List[str] = []
    rng        = _random.Random(f"{rack_a}_{rack_b}_{cable_type}") if random_variation else None

    for idx, (sa, sb) in enumerate(pairs):
        loc_a = sa.matrix_world.translation.copy()
        loc_b = sb.matrix_world.translation.copy()
        sag   = 0.04 + (rng.uniform(0.0, 0.03) if rng else 0.0)

        # Build three-segment waypoint path:
        # P0 = source socket
        # P1 = source socket XY, raised to tray height
        # P2 = destination socket XY, at tray height
        # P3 = destination socket
        mid_z = tray_z_m + (rng.uniform(0.0, 0.05) if rng else 0.0)

        seg_count = 16  # more segments for three-bend path smoothness
        seg_source = [list(loc_a)]
        seg_tray_a = [loc_a.x, loc_a.y, mid_z]
        seg_tray_b = [loc_b.x, loc_b.y, mid_z]
        seg_dest   = [list(loc_b)]

        # Interpolate a smooth path through four waypoints
        waypoints = [
            mathutils.Vector(loc_a),
            mathutils.Vector((loc_a.x, loc_a.y, mid_z)),
            mathutils.Vector((loc_b.x, loc_b.y, mid_z)),
            mathutils.Vector(loc_b),
        ]

        curve_name = f"{rack_a}_to_{rack_b}_{cable_type}_{idx:02d}"
        curve_data = bpy.data.curves.new(curve_name, type='CURVE')
        curve_data.dimensions      = '3D'
        _, bevel_r = _CABLE_PRESETS.get(cable_type, ((0, 0, 0), 0.003))
        curve_data.bevel_depth     = bevel_r
        curve_data.bevel_resolution = 4
        curve_data.use_fill_caps   = True

        # Distribute seg_count points across the four waypoints
        spline = curve_data.splines.new('NURBS')
        spline.points.add(seg_count - 1)
        for i in range(seg_count):
            t_global = i / (seg_count - 1) * 3.0
            seg_idx  = min(int(t_global), 2)
            t_local  = t_global - seg_idx
            p = waypoints[seg_idx].lerp(waypoints[seg_idx + 1], t_local)
            # Sag on horizontal spans only
            if seg_idx == 1:
                p.z -= math.sin(t_local * math.pi) * sag
            if rng and 0 < i < seg_count - 1:
                jit = sag * 0.1
                p.x += rng.uniform(-jit, jit)
                p.y += rng.uniform(-jit, jit)
            spline.points[i].co = (p.x, p.y, p.z, 1.0)

        spline.use_endpoint_u = True
        spline.order_u = min(4, seg_count)

        cable_obj = bpy.data.objects.new(curve_name, curve_data)
        cable_col.objects.link(cable_obj)

        cable_obj["cable_path"]   = True
        cable_obj["cable_type"]   = cable_type
        cable_obj["cable_source"] = sa.name
        cable_obj["cable_dest"]   = sb.name
        cable_obj["sag_m"]        = round(sag, 4)
        cable_obj["tray_z_m"]     = round(mid_z, 4)

        mat_name = _ensure_cable_material(cable_type, None)
        if mat_name and bpy.data.materials.get(mat_name):
            cable_obj.data.materials.append(bpy.data.materials[mat_name])

        created.append(curve_name)

    return {
        "rack_a":          rack_a,
        "rack_b":          rack_b,
        "cable_type":      cable_type,
        "cables_created":  len(created),
        "cables":          created,
        "tray_z_m":        round(tray_z_m, 4),
    }


# ── Tool 8: generate_cable_bundle ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def generate_cable_bundle(
    cable_names: List[str],
    bundle_radius_mm: float = 15.0,
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Offset a group of cable curves into a realistic bundle.

    Each cable in the list has its intermediate control points shifted by a
    unique XY offset within a circle of bundle_radius_mm. The first and last
    control points are not moved — cables still start and end at their original
    sockets. Modifies curve data in-place; no new objects are created.

    Use after create_cable_path or route_cables_between_racks to make a dense
    patch panel or inter-rack link look like a real cable bundle rather than
    a stack of identical overlapping tubes.

    cable_names:      list of curve object names to bundle together
    bundle_radius_mm: max radial spread in mm (default 15 = 30 mm diameter)
    seed:             integer seed for deterministic offsets (0 = use name hash)
    """
    if not cable_names:
        raise ValueError("cable_names list is empty")

    r_m = bundle_radius_mm / 1000.0
    modified: List[str] = []
    skipped:  List[str] = []

    count = len(cable_names)
    for i, name in enumerate(cable_names):
        obj = bpy.data.objects.get(name)
        if not obj or obj.type != 'CURVE':
            skipped.append(name)
            continue

        # Deterministic angle and radius for this cable in the bundle
        actual_seed = seed if seed != 0 else int(
            hashlib.md5(name.encode()).hexdigest()[:8], 16
        )
        rng     = _random.Random(actual_seed + i)
        angle   = (i / count) * 2 * math.pi + rng.uniform(-0.3, 0.3)
        radius  = rng.uniform(0.0, r_m)
        dx      = math.cos(angle) * radius
        dy      = math.sin(angle) * radius

        for spline in obj.data.splines:
            pts = spline.points
            n   = len(pts)
            if n < 3:
                continue
            # Shift intermediate points — skip first (index 0) and last (index n-1)
            for j in range(1, n - 1):
                # Taper offset toward endpoints for smooth entry/exit
                taper = math.sin(j / (n - 1) * math.pi)
                pts[j].co.x += dx * taper
                pts[j].co.y += dy * taper

        modified.append(name)

    return {
        "bundled":          modified,
        "skipped":          skipped,
        "bundle_radius_mm": bundle_radius_mm,
        "seed":             seed,
        "count":            len(modified),
    }


# ── Tool 9: add_patch_panel_connections ───────────────────────────────────

@mcp.tool()
@thread_safe
def add_patch_panel_connections(
    panel_name: str,
    target_name: str,
    port_pairs: Optional[List[List[int]]] = None,
    cable_type: str = "cat6",
    random_variation: bool = False,
    collection_name: str = "Cables",
) -> Dict[str, Any]:
    """
    Create short patch cable curves from a patch panel to an adjacent switch or panel.

    Finds SOCKET_Port_XX empties on the panel and SOCKET_Port_XX empties on
    the target. For each pair in port_pairs, creates a short curved cable
    (sag_m=0.02) typical of front-of-rack patch cable rat's nest.

    port_pairs: list of [panel_port_idx, target_port_idx] pairs, e.g. [[0,0],[1,1]].
                If None, auto-pairs all ports up to min(panel_ports, target_ports).

    panel_name:       patch panel object name (must have SOCKET_Port_XX empties)
    target_name:      switch or other panel object name
    port_pairs:       explicit port index pairs, or None for auto-pairing
    cable_type:       'cat6' | 'fiber' (default 'cat6')
    random_variation: randomize sag and color per cable for visual variety
    collection_name:  collection for the patch cable curves
    """
    # Collect port sockets on panel
    def _port_sockets(obj_name: str) -> List[bpy.types.Object]:
        obj = bpy.data.objects.get(obj_name)
        if not obj:
            return []
        result: List[bpy.types.Object] = []
        for child in bpy.data.objects:
            if child.parent and child.parent.name == obj_name:
                if "Port_" in child.name and SOCKET_PREFIX in child.name:
                    result.append(child)
        result.sort(key=lambda o: o.name)
        return result

    panel_sockets = _port_sockets(panel_name)
    target_sockets = _port_sockets(target_name)

    if not panel_sockets:
        raise ValueError(f"No SOCKET_Port_XX empties found on '{panel_name}'")
    if not target_sockets:
        raise ValueError(f"No SOCKET_Port_XX empties found on '{target_name}'")

    # Build port_pairs list
    if port_pairs is None:
        n = min(len(panel_sockets), len(target_sockets))
        pairs = [[i, i] for i in range(n)]
    else:
        pairs = port_pairs

    rng = _random.Random(f"{panel_name}_{target_name}") if random_variation else None
    created: List[str] = []

    for pidx, (pi, ti) in enumerate(pairs):
        if pi >= len(panel_sockets) or ti >= len(target_sockets):
            continue

        src_sock = panel_sockets[pi]
        dst_sock = target_sockets[ti]

        sag = 0.020
        if rng:
            sag += rng.uniform(-0.008, 0.020)

        cable_name = f"Patch_{panel_name}_to_{target_name}_{pidx:02d}"
        loc_src    = src_sock.matrix_world.translation
        loc_dst    = dst_sock.matrix_world.translation

        result = create_cable_path(
            cable_name=cable_name,
            source=[loc_src.x, loc_src.y, loc_src.z],
            destination=[loc_dst.x, loc_dst.y, loc_dst.z],
            sag_m=sag,
            radius_mm=2.5,
            segments=8,
            cable_type=cable_type,
            color_hex="",
            random_variation=random_variation,
            collection_name=collection_name,
        )

        # Tag with port metadata
        obj = bpy.data.objects.get(cable_name)
        if obj:
            obj["panel_port"]  = pi
            obj["target_port"] = ti

        created.append(cable_name)

    return {
        "panel":          panel_name,
        "target":         target_name,
        "cables_created": len(created),
        "cables":         created,
        "cable_type":     cable_type,
    }


# ── Tool 10: export_cable_data_json ───────────────────────────────────────

@mcp.tool()
@thread_safe
def export_cable_data_json(
    output_path: str,
    collection_name: str = "",
) -> Dict[str, Any]:
    """
    Export all cable curve routing data to a JSON manifest for UE5.

    Walks the scene (or a named collection) and collects every object with a
    'cable_path' custom property. For each cable, records:
      - Object name, cable_type, source socket, destination socket
      - World-space control point positions (for Spline Mesh / Cable Component)
      - Bevel radius, sag, estimated length

    UE5 PCG graphs or Blueprints can read this JSON to spawn spline meshes or
    Cable Components at the correct world positions without re-importing geometry.

    output_path:      absolute path for the output JSON file
    collection_name:  limit export to this collection (empty = full scene)
    """
    if collection_name:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
        objects = list(col.all_objects)
    else:
        objects = list(bpy.data.objects)

    cables: List[Dict[str, Any]] = []

    for obj in objects:
        if obj.type != 'CURVE':
            continue
        if not obj.get("cable_path"):
            continue

        control_points: List[List[float]] = []
        for spline in obj.data.splines:
            for pt in spline.points:
                # pt.co is (x, y, z, w) — world space since curve has no parent
                world_pt = obj.matrix_world @ mathutils.Vector(pt.co[:3])
                control_points.append([round(v, 5) for v in world_pt])

        entry: Dict[str, Any] = {
            "name":          obj.name,
            "cable_type":    obj.get("cable_type",   "unknown"),
            "source":        obj.get("cable_source", ""),
            "destination":   obj.get("cable_dest",   ""),
            "sag_m":         obj.get("sag_m",        0.05),
            "radius_mm":     obj.get("radius_mm",    3.0),
            "length_m":      obj.get("cable_length_m", 0.0),
            "control_points": control_points,
        }
        # Include any extra per-cable metadata stored as custom props
        for key in ("tray_z_m", "panel_port", "target_port", "segments"):
            if key in obj:
                entry[key] = obj[key]

        cables.append(entry)

    manifest = {
        "source":       "blenddc_mcp",
        "collection":   collection_name or "scene",
        "cable_count":  len(cables),
        "cables":       cables,
    }

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, default=str)

    total_length = round(sum(c.get("length_m", 0) for c in cables), 2)

    return {
        "output_path":    output_path,
        "cable_count":    len(cables),
        "total_length_m": total_length,
        "collection":     collection_name or "scene",
    }


# ── Tool 11: validate_cable_routing ───────────────────────────────────────

@mcp.tool()
@thread_safe
def validate_cable_routing(
    collection_name: str = "",
    max_length_m: float = 10.0,
) -> Dict[str, Any]:
    """
    Validate all cable curves in a collection or scene.

    Checks each cable with a 'cable_path' custom property for:
      FAIL — source or destination socket no longer exists in the scene
             (socket deleted after cable was created — loose endpoint)
      FAIL — cable exceeds max_length_m (likely a routing error)
      WARN — two cables share identical source + destination (duplicate route)
      WARN — cable has no material assigned
      WARN — cable has fewer than 4 control points (degenerate curve)

    Returns a structured pass/warn/fail report matching the validate_bay format.

    collection_name: limit check to this collection (empty = full scene)
    max_length_m:    maximum acceptable cable length in metres (default 10)
    """
    if collection_name:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
        objects = list(col.all_objects)
    else:
        objects = list(bpy.data.objects)

    cable_objects = [o for o in objects if o.type == 'CURVE' and o.get("cable_path")]

    reports:   List[Dict[str, Any]] = []
    fail_count = 0
    warn_count = 0
    seen_routes: Dict[str, str] = {}  # "src→dst" → first cable name

    for obj in cable_objects:
        issues:   List[str] = []
        warnings: List[str] = []

        src_name = obj.get("cable_source", "")
        dst_name = obj.get("cable_dest",   "")
        length_m = float(obj.get("cable_length_m", 0.0))

        # Loose endpoint checks
        if src_name and src_name.startswith(SOCKET_PREFIX):
            if not bpy.data.objects.get(src_name):
                issues.append(f"Source socket '{src_name}' no longer exists (loose endpoint)")

        if dst_name and dst_name.startswith(SOCKET_PREFIX):
            if not bpy.data.objects.get(dst_name):
                issues.append(f"Destination socket '{dst_name}' no longer exists (loose endpoint)")

        # Length check
        if length_m > max_length_m:
            issues.append(
                f"Cable length {length_m:.2f} m exceeds max {max_length_m} m — "
                "likely a routing error"
            )

        # Duplicate route check
        route_key = f"{src_name}→{dst_name}"
        rev_key   = f"{dst_name}→{src_name}"
        if route_key in seen_routes:
            warnings.append(
                f"Duplicate route: same src/dst as '{seen_routes[route_key]}'"
            )
        elif rev_key in seen_routes:
            warnings.append(
                f"Reverse duplicate: mirrors route of '{seen_routes[rev_key]}'"
            )
        else:
            seen_routes[route_key] = obj.name

        # No material assigned
        if not obj.data.materials:
            warnings.append("No material assigned — cable will render as default grey")

        # Degenerate curve
        total_pts = sum(len(s.points) for s in obj.data.splines)
        if total_pts < 4:
            warnings.append(f"Only {total_pts} control points — curve may render poorly")

        status = "fail" if issues else ("warn" if warnings else "pass")
        if issues:
            fail_count += len(issues)
        if warnings:
            warn_count += len(warnings)

        reports.append({
            "cable":    obj.name,
            "type":     obj.get("cable_type", "unknown"),
            "length_m": round(length_m, 3),
            "status":   status,
            "issues":   issues,
            "warnings": warnings,
        })

    overall = "fail" if fail_count > 0 else ("warn" if warn_count > 0 else "pass")

    return {
        "scope":        collection_name or "scene",
        "status":       overall,
        "cable_count":  len(cable_objects),
        "fail_count":   fail_count,
        "warn_count":   warn_count,
        "max_length_m": max_length_m,
        "cables":       reports,
    }


# ── Tool 12: clear_cables ─────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def clear_cables(
    collection_name: str = "",
    cable_type: str = "",
    confirm: bool = False,
) -> Dict[str, Any]:
    """
    Remove cable curve objects (and orphaned materials) from a collection or scene.

    Safety gate: confirm must be True to execute. This prevents accidental wipes
    when the tool is called without intent — the caller must explicitly set
    confirm=True to proceed.

    collection_name: limit removal to this collection (empty = scene-wide)
    cable_type:      only remove cables of this type (empty = all cable types)
    confirm:         must be True to execute; False returns a dry-run count only
    """
    if collection_name:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
        objects = list(col.all_objects)
    else:
        objects = list(bpy.data.objects)

    type_filter = cable_type.lower() if cable_type else ""

    targets: List[bpy.types.Object] = []
    for obj in objects:
        if obj.type != 'CURVE':
            continue
        if not obj.get("cable_path"):
            continue
        if type_filter and obj.get("cable_type", "").lower() != type_filter:
            continue
        targets.append(obj)

    if not confirm:
        return {
            "would_remove": len(targets),
            "confirmed":    False,
            "note":         "Set confirm=True to execute removal",
            "cables":       [o.name for o in targets],
        }

    removed: List[str] = []
    orphaned_mats = 0

    for obj in targets:
        removed.append(obj.name)
        # Collect materials before removing object
        mat_users = [m for m in obj.data.materials if m and m.users <= 1]
        bpy.data.objects.remove(obj, do_unlink=True)
        for mat in mat_users:
            bpy.data.materials.remove(mat, do_unlink=True)
            orphaned_mats += 1

    return {
        "removed":         removed,
        "count":           len(removed),
        "orphaned_mats":   orphaned_mats,
        "collection":      collection_name or "scene",
        "cable_type":      cable_type or "all",
        "confirmed":       True,
    }
