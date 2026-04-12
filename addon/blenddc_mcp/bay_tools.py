"""
Bay and row generation tools for the BlendDC asset pipeline.

Provides tools to build complete rack rows, hot-aisle/cold-aisle bays,
and full bay presets ready for UE5 level dressing.

Coordinate conventions:
  Row axis:   +X (racks are spaced along X, each origin at its base-front-centre)
  Aisle axis: +Y (Row_B is offset along Y by rack_depth + aisle_width)
  Vertical:   +Z

All generated collections have metadata stored as custom properties so that
export_bay_layout_json and validate_bay can inspect any bay without re-parsing
the scene hierarchy.
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
    RACK_U_M,
    RACK_DEFAULT_U_HEIGHT,
    RACK_DEFAULT_WIDTH_MM,
    RACK_DEFAULT_DEPTH_MM,
    RACK_BASE_HEIGHT_M,
    RACK_TOP_HEIGHT_M,
    RACK_POST_SIZE_M,
    RACK_SHEET_THICK_M,
    CABLE_TRAY_DEPTH_M,
    CABLE_TRAY_WALL_THICK_M,
)


# ── Local geometry helpers ─────────────────────────────────────────────────
# Self-contained copies — no circular imports with rack_tools or equipment_tools.

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
    """Get existing collection or create and link it to the scene root."""
    col = bpy.data.collections.get(name)
    if not col:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return col


def _nest_collection(child: bpy.types.Collection, parent: bpy.types.Collection) -> None:
    """Link child into parent; unlink from scene root if it was there directly."""
    if child.name not in parent.children:
        parent.children.link(child)
    # Remove from scene root if nested — avoids duplicate collection memberships
    scene_root = bpy.context.scene.collection
    if child.name in scene_root.children:
        scene_root.children.unlink(child)


def _rack_world_width_m(width_mm: float = RACK_DEFAULT_WIDTH_MM) -> float:
    """Cabinet total width in metres."""
    return width_mm / 1000.0


def _rack_world_depth_m(depth_mm: float = RACK_DEFAULT_DEPTH_MM) -> float:
    """Cabinet total depth in metres."""
    return depth_mm / 1000.0


def _rack_total_height_m(u_height: int = RACK_DEFAULT_U_HEIGHT) -> float:
    """Cabinet total height: base + u zone + top cap."""
    return RACK_BASE_HEIGHT_M + u_height * RACK_U_M + RACK_TOP_HEIGHT_M


# ── Tool 1: create_rack_row ────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_rack_row(
    row_name: str = "Row_A",
    rack_count: int = 5,
    u_height: int = 42,
    width_mm: float = 600.0,
    depth_mm: float = 1000.0,
    rack_gap_mm: float = 50.0,
    start_x_m: float = 0.0,
    start_y_m: float = 0.0,
    parent_collection: str = "",
) -> Dict[str, Any]:
    """
    Create a linear row of rack cabinets spaced along the X axis.

    Calls create_rack_cabinet for each rack, then groups all rack collections
    inside a Row_<row_name> parent collection with metadata.

    row_name:           name for this row (used as collection name prefix)
    rack_count:         number of racks in the row
    u_height:           rack height in U (applied to all racks)
    width_mm:           individual rack width in mm
    depth_mm:           individual rack depth in mm
    rack_gap_mm:        centre-to-centre gap between adjacent racks in mm
                        (applied on top of rack width — set 0 for flush)
    start_x_m:         world X position of first rack's origin
    start_y_m:         world Y position of all racks in this row
    parent_collection:  if set, nest the row collection inside this collection
    """
    import rack_tools as _rt

    row_col_name = row_name
    row_col = _get_or_create_collection(row_col_name)
    if parent_collection:
        parent_col = bpy.data.collections.get(parent_collection)
        if parent_col:
            _nest_collection(row_col, parent_col)

    rack_w_m   = width_mm / 1000.0
    rack_gap_m = rack_gap_mm / 1000.0
    step_m     = rack_w_m + rack_gap_m

    rack_names: List[str] = []
    rack_x_positions: List[float] = []

    for i in range(rack_count):
        rack_name = f"{row_name}_Rack_{i + 1:02d}"
        x_m       = start_x_m + i * step_m

        _rt.create_rack_cabinet(
            name=rack_name,
            u_height=u_height,
            width_mm=width_mm,
            depth_mm=depth_mm,
            bracket_left=(i == 0),
            bracket_right=(i == rack_count - 1),
        )

        rack_col = bpy.data.collections.get(rack_name)
        if rack_col:
            # Translate all objects in the rack collection to the row position
            for obj in rack_col.all_objects:
                if obj.parent is None:
                    obj.location.x += x_m
                    obj.location.y += start_y_m

            rack_col["row_x_m"] = round(x_m, 4)
            rack_col["row_y_m"] = round(start_y_m, 4)
            _nest_collection(rack_col, row_col)

        rack_names.append(rack_name)
        rack_x_positions.append(round(x_m, 4))

    # Row metadata
    row_col["row_rack_count"]  = rack_count
    row_col["row_u_height"]    = u_height
    row_col["row_width_mm"]    = width_mm
    row_col["row_depth_mm"]    = depth_mm
    row_col["row_gap_mm"]      = rack_gap_mm
    row_col["row_start_x_m"]   = round(start_x_m, 4)
    row_col["row_start_y_m"]   = round(start_y_m, 4)
    row_col["is_rack_row"]     = True

    return {
        "row":              row_name,
        "rack_count":       rack_count,
        "racks":            rack_names,
        "x_positions_m":    rack_x_positions,
        "step_m":           round(step_m, 4),
        "total_length_m":   round(rack_count * step_m - rack_gap_m, 4),
    }


# ── Tool 2: create_server_row ──────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_server_row(
    row_name: str = "ServerRow_A",
    rack_count: int = 5,
    u_height: int = 42,
    width_mm: float = 600.0,
    depth_mm: float = 1000.0,
    rack_gap_mm: float = 50.0,
    start_x_m: float = 0.0,
    start_y_m: float = 0.0,
    random_variation: bool = False,
    parent_collection: str = "",
) -> Dict[str, Any]:
    """
    Create a fully-populated server row using the server_dense preset.

    Calls create_rack_row to lay out the cabinets, then calls
    populate_rack_procedural(preset='server_dense') on every rack.
    Each rack is filled with 2U servers from U1 to the top.

    row_name:           name prefix for row and rack collections
    rack_count:         number of racks in the row
    u_height:           rack height in U
    width_mm:           rack width in mm
    depth_mm:           rack depth in mm
    rack_gap_mm:        gap between adjacent racks in mm
    start_x_m:         world X of first rack origin
    start_y_m:         world Y of all racks
    random_variation:   pass to populate_rack_procedural for visual variety
    parent_collection:  optional parent collection for the row
    """
    import equipment_tools as _et

    row_result = create_rack_row(
        row_name=row_name,
        rack_count=rack_count,
        u_height=u_height,
        width_mm=width_mm,
        depth_mm=depth_mm,
        rack_gap_mm=rack_gap_mm,
        start_x_m=start_x_m,
        start_y_m=start_y_m,
        parent_collection=parent_collection,
    )

    populated: List[str] = []
    for rack_name in row_result["racks"]:
        _et.populate_rack_procedural(
            collection_name=rack_name,
            preset="server_dense",
            random_variation=random_variation,
        )
        populated.append(rack_name)

    return {
        "row":            row_name,
        "preset":         "server_dense",
        "rack_count":     rack_count,
        "populated":      populated,
        "random_variation": random_variation,
        "total_length_m": row_result["total_length_m"],
    }


# ── Tool 3: create_network_row ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_network_row(
    row_name: str = "NetworkRow_A",
    rack_count: int = 4,
    u_height: int = 42,
    width_mm: float = 600.0,
    depth_mm: float = 1000.0,
    rack_gap_mm: float = 50.0,
    start_x_m: float = 0.0,
    start_y_m: float = 0.0,
    random_variation: bool = False,
    parent_collection: str = "",
) -> Dict[str, Any]:
    """
    Create a spine/leaf network row — switches, routers, and patch panels.

    Calls create_rack_row then populate_rack_procedural(preset='spine_leaf')
    on every rack. Bottom 65 % of each rack = 2U servers; top 35 % =
    alternating 1U switches + 1U patch panels.

    row_name:           name prefix for row and rack collections
    rack_count:         number of racks in the row
    u_height:           rack height in U
    width_mm / depth_mm / rack_gap_mm: cabinet dimensions and spacing
    start_x_m / start_y_m: world position of first rack origin
    random_variation:   pass to populate_rack_procedural for visual variety
    parent_collection:  optional parent collection for the row
    """
    import equipment_tools as _et

    row_result = create_rack_row(
        row_name=row_name,
        rack_count=rack_count,
        u_height=u_height,
        width_mm=width_mm,
        depth_mm=depth_mm,
        rack_gap_mm=rack_gap_mm,
        start_x_m=start_x_m,
        start_y_m=start_y_m,
        parent_collection=parent_collection,
    )

    populated: List[str] = []
    for rack_name in row_result["racks"]:
        _et.populate_rack_procedural(
            collection_name=rack_name,
            preset="spine_leaf",
            random_variation=random_variation,
        )
        populated.append(rack_name)

    return {
        "row":            row_name,
        "preset":         "spine_leaf",
        "rack_count":     rack_count,
        "populated":      populated,
        "random_variation": random_variation,
        "total_length_m": row_result["total_length_m"],
    }


# ── Tool 4: create_mixed_row ───────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_mixed_row(
    row_name: str = "MixedRow_A",
    rack_count: int = 5,
    u_height: int = 42,
    width_mm: float = 600.0,
    depth_mm: float = 1000.0,
    rack_gap_mm: float = 50.0,
    start_x_m: float = 0.0,
    start_y_m: float = 0.0,
    random_variation: bool = False,
    parent_collection: str = "",
) -> Dict[str, Any]:
    """
    Create a general-purpose mixed equipment row.

    Calls create_rack_row then populate_rack_procedural(preset='mixed_dc')
    on every rack. Each rack cycles through patch panels, switches, and servers
    in the mixed_dc pattern.

    row_name:           name prefix for row and rack collections
    rack_count:         number of racks in the row
    u_height:           rack height in U
    width_mm / depth_mm / rack_gap_mm: cabinet dimensions and spacing
    start_x_m / start_y_m: world position of first rack origin
    random_variation:   pass to populate_rack_procedural for visual variety
    parent_collection:  optional parent collection for the row
    """
    import equipment_tools as _et

    row_result = create_rack_row(
        row_name=row_name,
        rack_count=rack_count,
        u_height=u_height,
        width_mm=width_mm,
        depth_mm=depth_mm,
        rack_gap_mm=rack_gap_mm,
        start_x_m=start_x_m,
        start_y_m=start_y_m,
        parent_collection=parent_collection,
    )

    populated: List[str] = []
    for rack_name in row_result["racks"]:
        _et.populate_rack_procedural(
            collection_name=rack_name,
            preset="mixed_dc",
            random_variation=random_variation,
        )
        populated.append(rack_name)

    return {
        "row":            row_name,
        "preset":         "mixed_dc",
        "rack_count":     rack_count,
        "populated":      populated,
        "random_variation": random_variation,
        "total_length_m": row_result["total_length_m"],
    }


# ── Tool 5: create_bay ────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_bay(
    bay_name: str = "Bay_01",
    racks_per_row: int = 5,
    u_height: int = 42,
    width_mm: float = 600.0,
    depth_mm: float = 1000.0,
    rack_gap_mm: float = 50.0,
    aisle_width_mm: float = 1200.0,
    hot_aisle_containment: bool = False,
    start_x_m: float = 0.0,
    start_y_m: float = 0.0,
) -> Dict[str, Any]:
    """
    Create a full hot-aisle/cold-aisle bay with two facing rack rows,
    raised-floor tile strip, and overhead cable tray.

    Layout (top-down, Y axis):
      Row_A  ← cold-aisle face (front doors face +Y)
      [cold aisle]
      Row_B  ← hot-aisle face  (rear faces cold aisle, front faces hot aisle)
      [hot aisle — contained if hot_aisle_containment=True]

    Row_A origin is at start_y_m.
    Row_B origin is at start_y_m + rack_depth + aisle_width (cold aisle).
    The hot aisle sits behind Row_B.

    Geometry added to the bay:
      FloorTiles  — raised floor tile strip under the cold aisle (1-tile-thick
                    boxes, 600 mm × 600 mm × 30 mm, stamped with is_floor_tile)
      CableTrays  — single overhead cable tray running the row length at
                    top-of-rack height, centred over the cold aisle
      [optional] HotAisleContainment — two thin cap planes (one at each end
                    of the hot aisle) for visual containment

    bay_name:              parent collection name
    racks_per_row:         racks per row (both rows get the same count)
    u_height:              rack U height
    width_mm:              rack width in mm
    depth_mm:              rack depth in mm
    rack_gap_mm:           gap between adjacent racks in mm
    aisle_width_mm:        cold aisle width in mm (1200 mm recommended minimum)
    hot_aisle_containment: add geometry caps at each end of the hot aisle
    start_x_m:            world X of bay origin (first rack in Row_A)
    start_y_m:            world Y of bay origin (Row_A front face)
    """
    rack_w_m    = width_mm / 1000.0
    rack_d_m    = depth_mm / 1000.0
    rack_gap_m  = rack_gap_mm / 1000.0
    aisle_m     = aisle_width_mm / 1000.0
    step_m      = rack_w_m + rack_gap_m
    row_len_m   = racks_per_row * step_m - rack_gap_m
    rack_h_m    = _rack_total_height_m(u_height)

    # ── Parent bay collection ──────────────────────────────────────────────
    bay_col = _get_or_create_collection(bay_name)

    # ── Row A (cold-aisle side, front faces cold aisle) ───────────────────
    # Row A is placed at start_y_m and then rotated 180° around Z so its
    # front face points toward +Y (into the cold aisle).
    # After rotation: front at start_y_m (faces +Y), rear at start_y_m - rack_d_m.
    row_a_name = f"{bay_name}_Row_A"
    row_a_y    = start_y_m
    create_rack_row(
        row_name=row_a_name,
        rack_count=racks_per_row,
        u_height=u_height,
        width_mm=width_mm,
        depth_mm=depth_mm,
        rack_gap_mm=rack_gap_mm,
        start_x_m=start_x_m,
        start_y_m=row_a_y,
        parent_collection=bay_name,
    )
    # Front-to-front fix: rotate Row_A 180° so front faces +Y (toward cold aisle)
    row_a_col = bpy.data.collections.get(row_a_name)
    if row_a_col:
        for rack_subcol in row_a_col.children:
            for obj in rack_subcol.all_objects:
                if obj.parent is None:
                    obj.rotation_euler.z += math.pi

    # ── Row B (cold-aisle side, front faces cold aisle) ───────────────────
    # Row B front face is at start_y_m + aisle_m (no rotation — front faces -Y).
    # Cold aisle = gap between Row_A front (start_y_m) and Row_B front (start_y_m+aisle_m).
    # Row_A rear: start_y_m - rack_d_m (hot side).
    # Row_B rear: start_y_m + aisle_m + rack_d_m (hot side).
    row_b_name = f"{bay_name}_Row_B"
    row_b_y    = start_y_m + aisle_m
    create_rack_row(
        row_name=row_b_name,
        rack_count=racks_per_row,
        u_height=u_height,
        width_mm=width_mm,
        depth_mm=depth_mm,
        rack_gap_mm=rack_gap_mm,
        start_x_m=start_x_m,
        start_y_m=row_b_y,
        parent_collection=bay_name,
    )

    # ── Floor tiles (cold aisle) ───────────────────────────────────────────
    # Tiles cover the cold aisle: Row_A front to Row_B front.
    tile_col_name = f"{bay_name}_FloorTiles"
    tile_col      = _get_or_create_collection(tile_col_name)
    _nest_collection(tile_col, bay_col)

    tile_size_m   = 0.600
    tile_thick_m  = 0.030
    # Cold aisle spans from Row_A front face (start_y_m) to Row_B front face (row_b_y)
    aisle_y_start = start_y_m
    aisle_y_end   = row_b_y
    aisle_y_ctr   = (aisle_y_start + aisle_y_end) / 2.0

    tiles_x = math.ceil(row_len_m / tile_size_m)
    tiles_y = max(1, math.ceil(aisle_m / tile_size_m))
    tile_idx = 0
    for ix in range(tiles_x):
        for iy in range(tiles_y):
            tx = start_x_m + ix * tile_size_m + tile_size_m / 2
            ty = aisle_y_start + iy * tile_size_m + tile_size_m / 2
            tile = _create_box_object(
                name=f"{bay_name}_Tile_{tile_idx:03d}",
                cx=tx, cy=ty, cz=-tile_thick_m / 2,
                w=tile_size_m - 0.004,   # 4 mm grout gap
                d=tile_size_m - 0.004,
                h=tile_thick_m,
                collection=tile_col,
            )
            tile["is_floor_tile"] = True
            tile_idx += 1

    # ── Overhead cable tray (centred over cold aisle at top-of-rack height) ─
    tray_col_name = f"{bay_name}_CableTrays"
    tray_col      = _get_or_create_collection(tray_col_name)
    _nest_collection(tray_col, bay_col)

    tray_y_ctr  = aisle_y_ctr
    tray_width  = 0.200        # 200 mm wide cable ladder
    tray_depth  = CABLE_TRAY_DEPTH_M
    tray_z_base = rack_h_m     # sits at top-of-rack height

    # Main trough body
    tray_body = _create_box_object(
        name=f"{bay_name}_CableTray_Body",
        cx=start_x_m + row_len_m / 2,
        cy=tray_y_ctr,
        cz=tray_z_base + tray_depth / 2,
        w=row_len_m,
        d=tray_width,
        h=tray_depth,
        collection=tray_col,
    )
    tray_body["is_cable_tray"] = True

    # Left side wall
    wall_l = _create_box_object(
        name=f"{bay_name}_CableTray_WallL",
        cx=start_x_m + row_len_m / 2,
        cy=tray_y_ctr - tray_width / 2 + CABLE_TRAY_WALL_THICK_M / 2,
        cz=tray_z_base + tray_depth / 2,
        w=row_len_m,
        d=CABLE_TRAY_WALL_THICK_M,
        h=tray_depth,
        collection=tray_col,
    )
    wall_l["is_cable_tray"] = True

    # Right side wall
    wall_r = _create_box_object(
        name=f"{bay_name}_CableTray_WallR",
        cx=start_x_m + row_len_m / 2,
        cy=tray_y_ctr + tray_width / 2 - CABLE_TRAY_WALL_THICK_M / 2,
        cz=tray_z_base + tray_depth / 2,
        w=row_len_m,
        d=CABLE_TRAY_WALL_THICK_M,
        h=tray_depth,
        collection=tray_col,
    )
    wall_r["is_cable_tray"] = True

    # ── Hot aisle containment caps (optional) ─────────────────────────────
    # Two thin planar panels, one at each end of the hot aisle (X-axis ends),
    # spanning from Row B front face to Row B rear (hot aisle width = rack depth).
    # Height = full rack height. Thickness = 10 mm (visual only, no collision).
    containment_names: List[str] = []
    if hot_aisle_containment:
        hac_col_name = f"{bay_name}_HotAisleContainment"
        hac_col      = _get_or_create_collection(hac_col_name)
        _nest_collection(hac_col, bay_col)

        cap_thick_m  = 0.010
        hot_y_front  = row_b_y                      # Row B front face
        hot_y_rear   = row_b_y + rack_d_m           # Row B rear face
        hot_y_ctr    = (hot_y_front + hot_y_rear) / 2.0
        hot_aisle_d  = rack_d_m                     # depth of hot aisle = rack depth

        for side, cap_x in (
            ("Near", start_x_m - cap_thick_m / 2),
            ("Far",  start_x_m + row_len_m + cap_thick_m / 2),
        ):
            cap = _create_box_object(
                name=f"{bay_name}_HAC_{side}",
                cx=cap_x,
                cy=hot_y_ctr,
                cz=rack_h_m / 2,
                w=cap_thick_m,
                d=hot_aisle_d,
                h=rack_h_m,
                collection=hac_col,
            )
            cap["is_hac_panel"] = True
            containment_names.append(cap.name)

    # ── Bay metadata ──────────────────────────────────────────────────────
    bay_col["is_bay"]                    = True
    bay_col["bay_racks_per_row"]         = racks_per_row
    bay_col["bay_u_height"]             = u_height
    bay_col["bay_width_mm"]             = width_mm
    bay_col["bay_depth_mm"]             = depth_mm
    bay_col["bay_rack_gap_mm"]          = rack_gap_mm
    bay_col["bay_aisle_width_mm"]       = aisle_width_mm
    bay_col["bay_hot_containment"]      = hot_aisle_containment
    bay_col["bay_start_x_m"]            = round(start_x_m, 4)
    bay_col["bay_start_y_m"]            = round(start_y_m, 4)
    bay_col["bay_row_a"]                = row_a_name
    bay_col["bay_row_b"]                = row_b_name
    bay_col["bay_total_racks"]          = racks_per_row * 2
    bay_col["bay_total_length_m"]       = round(row_len_m, 4)
    bay_col["bay_total_width_m"]        = round(
        rack_d_m + aisle_m + rack_d_m, 4
    )

    result: Dict[str, Any] = {
        "bay":                   bay_name,
        "rows":                  [row_a_name, row_b_name],
        "racks_per_row":         racks_per_row,
        "total_racks":           racks_per_row * 2,
        "aisle_width_mm":        aisle_width_mm,
        "hot_aisle_containment": hot_aisle_containment,
        "floor_tiles":           tile_idx,
        "cable_tray_length_m":   round(row_len_m, 4),
        "bay_footprint_m":       {
            "length_x": round(row_len_m, 4),
            "width_y":  round(rack_d_m + aisle_m + rack_d_m, 4),
        },
    }
    if hot_aisle_containment:
        result["hac_panels"] = containment_names

    return result


# ── Tool 6: populate_bay_from_json ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def populate_bay_from_json(
    json_path: str,
    random_variation: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Populate multiple racks across a bay from a single JSON layout file.

    The JSON describes the full bay: one or more rows, each containing one or
    more racks with equipment lists. Each rack's equipment list is passed
    directly to populate_rack_from_json via an in-memory temp file.

    JSON schema:
      {
        "bay": "Bay_01",
        "rows": [
          {
            "row": "Bay_01_Row_A",
            "racks": [
              {
                "rack": "Bay_01_Row_A_Rack_01",
                "equipment": [
                  {
                    "u_slot": 1, "u_size": 2, "type": "server",
                    "name": "SVR_A01_01", "depth_mm": 700
                  }
                ]
              }
            ]
          }
        ]
      }

    json_path:        absolute path to bay layout JSON
    random_variation: pass to each rack's populate_rack_from_json call
    dry_run:          validate without creating objects
    """
    import equipment_tools as _et
    import tempfile

    if not os.path.isfile(json_path):
        raise FileNotFoundError(f"JSON not found: {json_path}")

    with open(json_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    bay_name = payload.get("bay", "")
    rows     = payload.get("rows", [])
    if not rows:
        raise ValueError("JSON 'rows' list is empty or missing")

    summary: List[Dict[str, Any]] = []
    total_placed  = 0
    total_skipped = 0

    for row_spec in rows:
        row_name  = row_spec.get("row", "")
        racks     = row_spec.get("racks", [])

        for rack_spec in racks:
            rack_name  = rack_spec.get("rack", "")
            equipment  = rack_spec.get("equipment", [])

            # Write a per-rack temp JSON and delegate to populate_rack_from_json
            rack_payload = {"rack": rack_name, "equipment": equipment}

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as tmp:
                json.dump(rack_payload, tmp)
                tmp_path = tmp.name

            try:
                result = _et.populate_rack_from_json(
                    json_path=tmp_path,
                    collection_name=rack_name,
                    random_variation=random_variation,
                    dry_run=dry_run,
                )
                total_placed  += result.get("count", 0)
                total_skipped += len(result.get("skipped", []))
                summary.append({
                    "row":     row_name,
                    "rack":    rack_name,
                    "placed":  result.get("count", 0),
                    "skipped": len(result.get("skipped", [])),
                })
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    return {
        "bay":           bay_name,
        "total_placed":  total_placed,
        "total_skipped": total_skipped,
        "dry_run":       dry_run,
        "racks":         summary,
    }


# ── Tool 7: create_bay_preset ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_bay_preset(
    bay_name: str = "Bay_01",
    preset: str = "server_dense",
    racks_per_row: int = 5,
    u_height: int = 42,
    width_mm: float = 600.0,
    depth_mm: float = 1000.0,
    aisle_width_mm: float = 1200.0,
    hot_aisle_containment: bool = False,
    start_x_m: float = 0.0,
    start_y_m: float = 0.0,
    random_variation: bool = False,
) -> Dict[str, Any]:
    """
    Create a fully-populated bay in one call using a named preset.

    Presets:
      'server_dense'   — both rows filled with 2U servers (server_dense preset)
      'network_core'   — both rows filled spine/leaf style (spine_leaf preset)
      'mixed_dc'       — Row A: server_dense, Row B: mixed_dc
      'edge_pod'       — 2 racks per row, mixed_dc, 42U

    Calls create_bay to build the bay geometry, then populate_rack_procedural
    on every rack collection according to the preset.

    bay_name:              parent collection name
    preset:                layout preset (see above)
    racks_per_row:         racks per row (both rows)
    u_height:              rack U height (overridden to 42 for edge_pod)
    width_mm / depth_mm:   rack dimensions
    aisle_width_mm:        cold aisle width
    hot_aisle_containment: add end-cap panels to hot aisle
    start_x_m / start_y_m: world position of bay origin
    random_variation:      randomize equipment detail geometry for variety
    """
    import equipment_tools as _et

    preset = preset.lower().replace(" ", "_")
    valid = ("server_dense", "network_core", "mixed_dc", "edge_pod")
    if preset not in valid:
        raise ValueError(f"preset must be one of {valid}")

    # edge_pod override: 2 racks per row, 42U
    if preset == "edge_pod":
        racks_per_row = 2
        u_height      = 42

    # ── Build bay geometry (empty racks) ───────────────────────────────────
    bay_result = create_bay(
        bay_name=bay_name,
        racks_per_row=racks_per_row,
        u_height=u_height,
        width_mm=width_mm,
        depth_mm=depth_mm,
        rack_gap_mm=50.0,
        aisle_width_mm=aisle_width_mm,
        hot_aisle_containment=hot_aisle_containment,
        start_x_m=start_x_m,
        start_y_m=start_y_m,
    )

    # ── Determine equipment preset per row ─────────────────────────────────
    row_preset_map = {
        "server_dense": ("server_dense", "server_dense"),
        "network_core": ("spine_leaf",   "spine_leaf"),
        "mixed_dc":     ("server_dense", "mixed_dc"),
        "edge_pod":     ("mixed_dc",     "mixed_dc"),
    }
    preset_a, preset_b = row_preset_map[preset]

    row_a_name = bay_result["rows"][0]
    row_b_name = bay_result["rows"][1]

    row_a_col = bpy.data.collections.get(row_a_name)
    row_b_col = bpy.data.collections.get(row_b_name)

    populated: List[str] = []

    for row_col, eq_preset in ((row_a_col, preset_a), (row_b_col, preset_b)):
        if not row_col:
            continue
        for rack_col in row_col.children:
            if not rack_col.get("is_rack_cabinet"):
                continue
            _et.populate_rack_procedural(
                collection_name=rack_col.name,
                preset=eq_preset,
                random_variation=random_variation,
            )
            populated.append(rack_col.name)

    return {
        "bay":            bay_name,
        "preset":         preset,
        "racks_per_row":  racks_per_row,
        "total_racks":    bay_result["total_racks"],
        "populated":      populated,
        "total_populated": len(populated),
        "random_variation": random_variation,
        "bay_footprint_m": bay_result["bay_footprint_m"],
    }


# ── Tool 8: duplicate_bay ─────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def duplicate_bay(
    source_bay: str,
    new_bay_name: str,
    offset_x_m: float = 0.0,
    offset_y_m: float = 0.0,
    linked: bool = False,
) -> Dict[str, Any]:
    """
    Duplicate an existing bay collection and offset it in world space.

    Use linked=True for a lightweight linked duplicate (shared mesh data,
    independent object transforms) — best for multiple identical bays in a
    level layout. Use linked=False for a fully independent copy.

    The duplicated bay's metadata is updated with the new world origin.

    source_bay:  name of the existing bay collection to copy
    new_bay_name: name for the new bay collection
    offset_x_m:  X offset in metres from source bay's start_x
    offset_y_m:  Y offset in metres from source bay's start_y
    linked:      True = linked duplicate (shared mesh data); False = full copy
    """
    src_col = bpy.data.collections.get(source_bay)
    if not src_col:
        raise ValueError(f"Bay collection '{source_bay}' not found")
    if not src_col.get("is_bay"):
        raise ValueError(f"'{source_bay}' is not a bay collection (missing is_bay flag)")

    if bpy.data.collections.get(new_bay_name):
        raise ValueError(f"Collection '{new_bay_name}' already exists")

    # Deselect all, then select all objects in source bay (recursively)
    bpy.ops.object.select_all(action='DESELECT')
    for obj in src_col.all_objects:
        obj.select_set(True)

    # Set active to the first mesh in the bay
    active_candidate = next(
        (o for o in src_col.all_objects if o.type == 'MESH'), None
    )
    if active_candidate:
        bpy.context.view_layer.objects.active = active_candidate

    dup_type = 'LINKED' if linked else 'OBJECTS'
    bpy.ops.object.duplicate(linked=(linked))

    # The duplicated objects are now selected — move them
    for obj in bpy.context.selected_objects:
        obj.location.x += offset_x_m
        obj.location.y += offset_y_m

    # Build a new collection containing the duplicates
    new_col = bpy.data.collections.new(new_bay_name)
    bpy.context.scene.collection.children.link(new_col)

    for obj in bpy.context.selected_objects:
        # Link into new collection; remove from wherever Blender placed them
        for old_col in list(obj.users_collection):
            old_col.objects.unlink(obj)
        new_col.objects.link(obj)

    # Copy metadata from source, update origin
    for key in src_col.keys():
        new_col[key] = src_col[key]

    src_x = src_col.get("bay_start_x_m", 0.0)
    src_y = src_col.get("bay_start_y_m", 0.0)
    new_col["bay_start_x_m"] = round(src_x + offset_x_m, 4)
    new_col["bay_start_y_m"] = round(src_y + offset_y_m, 4)

    return {
        "source_bay":    source_bay,
        "new_bay":       new_bay_name,
        "offset_x_m":   offset_x_m,
        "offset_y_m":   offset_y_m,
        "linked":        linked,
        "objects_duped": len(bpy.context.selected_objects),
    }


# ── Tool 9: export_bay_layout_json ────────────────────────────────────────

@mcp.tool()
@thread_safe
def export_bay_layout_json(
    bay_name: str,
    output_path: str,
) -> Dict[str, Any]:
    """
    Export the full bay layout to a JSON manifest for UE5 PCG / level population.

    Walks the Bay collection hierarchy and records:
      - Bay-level metadata (footprint, aisle width, row names)
      - Per-row: rack count, world Y position
      - Per-rack: world transform origin, U height, all equipment in
        the _Equipment sub-collection with type/u_slot/world position

    The output JSON can drive a UE5 PCG graph or Blueprint spawner to
    instance racks and equipment without re-running the Blender pipeline.

    bay_name:    name of the bay collection to export
    output_path: absolute path for the output JSON file
    """
    bay_col = bpy.data.collections.get(bay_name)
    if not bay_col:
        raise ValueError(f"Bay collection '{bay_name}' not found")
    if not bay_col.get("is_bay"):
        raise ValueError(f"'{bay_name}' is not a bay collection")

    manifest: Dict[str, Any] = {
        "bay":       bay_name,
        "metadata": {k: bay_col[k] for k in bay_col.keys() if not k.startswith("_")},
        "rows":      [],
    }

    row_a_name = bay_col.get("bay_row_a", "")
    row_b_name = bay_col.get("bay_row_b", "")

    for row_name in (row_a_name, row_b_name):
        if not row_name:
            continue
        row_col = bpy.data.collections.get(row_name)
        if not row_col:
            continue

        row_entry: Dict[str, Any] = {
            "row":      row_name,
            "metadata": {k: row_col[k] for k in row_col.keys() if not k.startswith("_")},
            "racks":    [],
        }

        for rack_col in row_col.children:
            if not rack_col.get("is_rack_cabinet"):
                continue

            rack_obj  = bpy.data.objects.get(rack_col.name)
            rack_loc  = list(rack_obj.location) if rack_obj else [0.0, 0.0, 0.0]

            rack_entry: Dict[str, Any] = {
                "rack":      rack_col.name,
                "location":  [round(v, 4) for v in rack_loc],
                "metadata":  {k: rack_col[k] for k in rack_col.keys() if not k.startswith("_")},
                "equipment": [],
            }

            equip_col_name = f"{rack_col.name}_Equipment"
            equip_col      = bpy.data.collections.get(equip_col_name)
            if equip_col:
                for eq_obj in equip_col.all_objects:
                    if eq_obj.type != 'MESH':
                        continue
                    eq_entry: Dict[str, Any] = {
                        "name":     eq_obj.name,
                        "type":     eq_obj.get("equipment_type", "unknown"),
                        "u_size":   eq_obj.get("u_size", 1),
                        "location": [round(v, 4) for v in eq_obj.location],
                    }
                    # Include any extra per-type metadata stored on the object
                    for prop in ("depth_mm", "port_count", "pdu_type", "outlet_count"):
                        if prop in eq_obj:
                            eq_entry[prop] = eq_obj[prop]
                    rack_entry["equipment"].append(eq_entry)

            row_entry["racks"].append(rack_entry)

        manifest["rows"].append(row_entry)

    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, default=str)

    total_racks = sum(len(r["racks"]) for r in manifest["rows"])
    total_equip = sum(
        len(rack["equipment"])
        for row in manifest["rows"]
        for rack in row["racks"]
    )

    return {
        "bay":          bay_name,
        "output_path":  output_path,
        "rows":         len(manifest["rows"]),
        "total_racks":  total_racks,
        "total_equipment": total_equip,
    }


# ── Tool 10: validate_bay ─────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def validate_bay(
    bay_name: str,
) -> Dict[str, Any]:
    """
    Validate the entire bay for UE5 export readiness.

    Checks every rack and equipment object in the bay hierarchy and reports:
      - Bay-level: required metadata present, row collections found
      - Per-rack: is_rack_cabinet flag, U height metadata, origin at base-front-centre
      - Per-equipment: fits within rack U boundaries, no overlapping U slots,
        equipment_type and u_size custom properties set

    Returns a structured report with pass/warn/fail per rack plus bay-level summary.
    Fail count > 0 means the bay should not be exported until issues are resolved.

    bay_name: name of the bay collection to validate
    """
    import rack_tools as _rt

    bay_col = bpy.data.collections.get(bay_name)
    if not bay_col:
        raise ValueError(f"Bay collection '{bay_name}' not found")

    issues:    List[Dict[str, Any]] = []
    rack_reports: List[Dict[str, Any]] = []
    warn_count = 0
    fail_count = 0

    # ── Bay-level checks ──────────────────────────────────────────────────
    required_bay_meta = ("is_bay", "bay_racks_per_row", "bay_u_height",
                         "bay_row_a", "bay_row_b")
    for key in required_bay_meta:
        if key not in bay_col:
            issues.append({"level": "FAIL", "scope": "bay",
                           "msg": f"Bay missing metadata key: {key}"})
            fail_count += 1

    row_a_name = bay_col.get("bay_row_a", "")
    row_b_name = bay_col.get("bay_row_b", "")
    for row_name in (row_a_name, row_b_name):
        if row_name and not bpy.data.collections.get(row_name):
            issues.append({"level": "FAIL", "scope": "bay",
                           "msg": f"Row collection '{row_name}' not found in scene"})
            fail_count += 1

    # ── Per-rack checks ───────────────────────────────────────────────────
    for row_name in (row_a_name, row_b_name):
        row_col = bpy.data.collections.get(row_name)
        if not row_col:
            continue

        for rack_col in row_col.children:
            rack_issues: List[str] = []
            rack_warns:  List[str] = []
            u_height = rack_col.get("rack_u_height", 42)

            if not rack_col.get("is_rack_cabinet"):
                rack_issues.append("Missing is_rack_cabinet flag — not a valid rack collection")

            required_rack_meta = ("rack_u_height", "rack_base_height_m", "rack_post_size_mm")
            for key in required_rack_meta:
                if key not in rack_col:
                    rack_issues.append(f"Missing metadata: {key}")

            # Delegate per-rack validation to rack_tools if available
            try:
                vr = _rt.validate_rack_collection(collection_name=rack_col.name)
                if vr.get("status") == "fail":
                    for item in vr.get("issues", []):
                        rack_issues.append(item.get("msg", str(item)))
                elif vr.get("status") == "warn":
                    for item in vr.get("issues", []):
                        rack_warns.append(item.get("msg", str(item)))
            except Exception as exc:
                rack_warns.append(f"validate_rack_collection error: {exc}")

            # Equipment U-slot overlap detection
            equip_col_name = f"{rack_col.name}_Equipment"
            equip_col      = bpy.data.collections.get(equip_col_name)
            occupied: Dict[int, str] = {}

            if equip_col:
                for eq_obj in equip_col.all_objects:
                    if eq_obj.type != 'MESH':
                        continue
                    if "equipment_type" not in eq_obj:
                        rack_warns.append(f"'{eq_obj.name}' missing equipment_type property")
                    if "u_size" not in eq_obj:
                        rack_warns.append(f"'{eq_obj.name}' missing u_size property")
                        continue

                    u_size = eq_obj.get("u_size", 1)
                    bh     = rack_col.get("rack_base_height_m", RACK_BASE_HEIGHT_M)
                    # Derive u_slot from Z position
                    dz   = eq_obj.location.z - bh
                    u_slot = max(1, round(dz / RACK_U_M) + 1)

                    for u in range(u_slot, u_slot + u_size):
                        if u in occupied:
                            rack_issues.append(
                                f"U slot {u} overlap: '{eq_obj.name}' and '{occupied[u]}'"
                            )
                        else:
                            occupied[u] = eq_obj.name

                    if u_slot + u_size - 1 > u_height:
                        rack_issues.append(
                            f"'{eq_obj.name}' at U{u_slot}+{u_size} exceeds "
                            f"rack U{u_height} boundary"
                        )

            status = "fail" if rack_issues else ("warn" if rack_warns else "pass")
            if rack_issues:
                fail_count += len(rack_issues)
            if rack_warns:
                warn_count += len(rack_warns)

            rack_reports.append({
                "rack":       rack_col.name,
                "row":        row_name,
                "status":     status,
                "issues":     rack_issues,
                "warnings":   rack_warns,
                "equipment_count": len(occupied),
            })

    overall = "fail" if fail_count > 0 else ("warn" if warn_count > 0 else "pass")

    return {
        "bay":          bay_name,
        "status":       overall,
        "fail_count":   fail_count,
        "warn_count":   warn_count,
        "bay_issues":   issues,
        "rack_reports": rack_reports,
        "total_racks":  len(rack_reports),
    }


# ── Tool 11: get_bay_info ─────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def get_bay_info(
    bay_name: str,
) -> Dict[str, Any]:
    """
    Return a summary of the bay's contents and bounding box.

    Reports: row count, rack count, total U capacity, total U used,
    equipment type breakdown, and bay footprint in metres. Useful as a
    quick health-check and level-design reference before export.

    bay_name: name of the bay collection to inspect
    """
    bay_col = bpy.data.collections.get(bay_name)
    if not bay_col:
        raise ValueError(f"Bay collection '{bay_name}' not found")
    if not bay_col.get("is_bay"):
        raise ValueError(f"'{bay_name}' is not a bay collection")

    row_a_name = bay_col.get("bay_row_a", "")
    row_b_name = bay_col.get("bay_row_b", "")
    u_height   = bay_col.get("bay_u_height", RACK_DEFAULT_U_HEIGHT)

    total_racks    = 0
    total_u_cap    = 0
    total_u_used   = 0
    type_breakdown: Dict[str, int] = {}
    bounding_verts: List[Tuple[float, float, float]] = []

    for row_name in (row_a_name, row_b_name):
        row_col = bpy.data.collections.get(row_name)
        if not row_col:
            continue

        for rack_col in row_col.children:
            if not rack_col.get("is_rack_cabinet"):
                continue

            total_racks  += 1
            total_u_cap  += rack_col.get("rack_u_height", u_height)

            equip_col_name = f"{rack_col.name}_Equipment"
            equip_col      = bpy.data.collections.get(equip_col_name)
            if equip_col:
                for eq_obj in equip_col.all_objects:
                    if eq_obj.type != 'MESH':
                        continue
                    eq_type = eq_obj.get("equipment_type", "unknown")
                    u_size  = eq_obj.get("u_size", 1)
                    total_u_used += u_size
                    type_breakdown[eq_type] = type_breakdown.get(eq_type, 0) + 1

            # Collect bounding box corners for this rack
            for rack_obj in rack_col.all_objects:
                if rack_obj.type == 'MESH':
                    for corner in rack_obj.bound_box:
                        world_pt = rack_obj.matrix_world @ mathutils.Vector(corner)
                        bounding_verts.append(tuple(world_pt))

    footprint: Dict[str, float] = {}
    if bounding_verts:
        xs = [v[0] for v in bounding_verts]
        ys = [v[1] for v in bounding_verts]
        zs = [v[2] for v in bounding_verts]
        footprint = {
            "min_x": round(min(xs), 3), "max_x": round(max(xs), 3),
            "min_y": round(min(ys), 3), "max_y": round(max(ys), 3),
            "min_z": round(min(zs), 3), "max_z": round(max(zs), 3),
            "length_x": round(max(xs) - min(xs), 3),
            "width_y":  round(max(ys) - min(ys), 3),
            "height_z": round(max(zs) - min(zs), 3),
        }

    u_utilization = round(total_u_used / max(total_u_cap, 1) * 100, 1)

    return {
        "bay":              bay_name,
        "row_count":        2,
        "rack_count":       total_racks,
        "total_u_capacity": total_u_cap,
        "total_u_used":     total_u_used,
        "u_utilization_pct": u_utilization,
        "equipment_count":  sum(type_breakdown.values()),
        "type_breakdown":   type_breakdown,
        "bounding_box_m":   footprint,
        "aisle_width_mm":   bay_col.get("bay_aisle_width_mm", 1200),
        "hot_containment":  bay_col.get("bay_hot_containment", False),
    }
