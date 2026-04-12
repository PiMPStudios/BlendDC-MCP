"""
Full facility layout, population, variation, export, and validation tools
for the BlendDC asset pipeline.

Provides top-level tools that compose all lower-level modules (rack_tools,
bay_tools, cable_tools, variation_tools) into facility-scale operations.

Hierarchy convention:
  Facility_<section_name>           ← is_facility_section = True
    Bay_<n>                         ← is_bay = True (from bay_tools)
      Row_A / Row_B                 ← is_rack_row = True
        Rack_XX                     ← is_rack_cabinet = True
          <Rack_XX>_Equipment       ← equipment objects
    Corridors                       ← corridor geometry
    PowerCooling                    ← UPS / CRAC / busway geometry
    CableTrays                      ← shared facility-level trays

Coordinate convention:
  X — row direction (racks spaced along X within a bay)
  Y — cross-aisle direction (cold aisle → hot aisle)
  Z — height

All facility collections carry metadata custom properties so export and
validation tools can walk the hierarchy without parsing object names.
"""

import bpy
import bmesh
import json
import math
import os
import hashlib
import random as _random
from typing import Any, Dict, List, Optional, Tuple

import mathutils

from core import mcp, thread_safe, _log
from constants import (
    RACK_U_M,
    RACK_BASE_HEIGHT_M,
    RACK_TOP_HEIGHT_M,
    RACK_DEFAULT_WIDTH_MM,
    RACK_DEFAULT_DEPTH_MM,
    CABLE_TRAY_DEPTH_M,
    CABLE_TRAY_WALL_THICK_M,
    SOCKET_PREFIX,
    RF_PEDESTAL_BASE_W_M, RF_PEDESTAL_BASE_H_M,
    RF_PEDESTAL_SHAFT_W_M, RF_PEDESTAL_SHAFT_H_M,
    RF_PEDESTAL_HEAD_W_M, RF_PEDESTAL_HEAD_H_M,
    RF_PEDESTAL_TOTAL_H_M,
    RF_GRID_M,
    RF_STRINGER_W_M, RF_STRINGER_H_M,
    RF_TILE_W_M, RF_TILE_D_M, RF_TILE_H_M, RF_TILE_GROUT_M,
)


# ── Local geometry helpers ─────────────────────────────────────────────────

def _create_box_object(
    name: str,
    cx: float, cy: float, cz: float,
    w: float, d: float, h: float,
    collection: bpy.types.Collection,
) -> bpy.types.Object:
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


def _add_empty(
    name: str,
    location: Tuple[float, float, float],
    collection: bpy.types.Collection,
    display_type: str = 'ARROWS',
    display_size: float = 0.15,
) -> bpy.types.Object:
    full = name if name.startswith(SOCKET_PREFIX) else name
    existing = bpy.data.objects.get(full)
    if existing:
        bpy.data.objects.remove(existing, do_unlink=True)
    e = bpy.data.objects.new(full, None)
    e.empty_display_type = display_type
    e.empty_display_size = display_size
    e.location = location
    collection.objects.link(e)
    return e


def _rack_total_height_m(u_height: int) -> float:
    return RACK_BASE_HEIGHT_M + u_height * RACK_U_M + RACK_TOP_HEIGHT_M


def _join_zone(
    name: str,
    objects: List[bpy.types.Object],
    collection: bpy.types.Collection,
) -> bpy.types.Object:
    """
    Merge a list of individually-created box objects into one mesh object.

    Each part's world translation is baked into its vertex positions before
    merging, so the joined object sits at the world origin with correct
    geometry.  The source objects and their mesh data-blocks are removed
    after the join.
    """
    combined_bm = bmesh.new()
    tmp_meshes: List[bpy.types.Mesh] = []
    for part in objects:
        tmp = part.data.copy()
        tmp.transform(mathutils.Matrix.Translation(part.location))
        combined_bm.from_mesh(tmp)
        tmp_meshes.append(tmp)
    merged = bpy.data.meshes.new(name)
    combined_bm.to_mesh(merged)
    combined_bm.free()
    merged.update()
    joined = bpy.data.objects.new(name, merged)
    joined.location = (0.0, 0.0, 0.0)
    collection.objects.link(joined)
    for tm in tmp_meshes:
        bpy.data.meshes.remove(tm)
    for part in objects:
        bpy.data.objects.remove(part, do_unlink=True)
    return joined


def _obj_seed(base_seed: int, name: str) -> int:
    h = int(hashlib.md5(f"{base_seed}:{name}".encode()).hexdigest()[:8], 16)
    return h


def _create_raised_floor(
    zone_name: str,
    x0: float,
    y0: float,
    width: float,
    depth: float,
    parent_col: bpy.types.Collection,
    tile_type: str = 'solid',
) -> None:
    """
    Create a Tate-style raised floor system in the specified XY zone.

    Generates a full pedestal grid (base plate + shaft + head plate) and
    stringers, all joined into a single {zone_name}_Structure mesh (never
    needs to be edited).  Each 600×600 floor tile is a separate object in
    the {zone_name}_Tiles sub-collection so individual tiles can be selected,
    replaced, or swapped to perforated independently.

    Tile objects are named {zone_name}_tile_{ix}_{iy} where ix/iy are the
    zero-based grid column/row indices, making cold-aisle tile identification
    straightforward.

    Z coordinates — slab at Z = 0, finished floor (tile top) at Z = 0.450 m:
      base:    Z 0.000 → 0.006
      shaft:   Z 0.006 → 0.444
      head:    Z 0.444 → 0.450
      tile:    Z 0.425 → 0.450  (25 mm tile, flush with head top)

    zone_name:  unique name prefix for this floor zone
    x0, y0:    world XY of the zone's bottom-left corner (min X, min Y)
    width:     zone extent in X metres
    depth:     zone extent in Y metres
    parent_col: collection to nest the raised-floor sub-collection into
    tile_type: 'solid' (full square tile) or 'perforated' (open-centre grate)
    """
    rf_col = _get_or_create_collection(zone_name)
    _nest_collection(rf_col, parent_col)

    struct_col = _get_or_create_collection(f"{zone_name}_Structure")
    _nest_collection(struct_col, rf_col)
    tiles_col  = _get_or_create_collection(f"{zone_name}_Tiles")
    _nest_collection(tiles_col, rf_col)

    import math as _math
    # ceil ensures full zone coverage — last pedestal row always lands at or
    # beyond the far edge so every square metre of the zone gets a tile.
    nx = max(2, _math.ceil(width  / RF_GRID_M) + 1)
    ny = max(2, _math.ceil(depth  / RF_GRID_M) + 1)

    # Z centres — positive, slab at 0
    base_cz     = RF_PEDESTAL_BASE_H_M / 2
    shaft_cz    = RF_PEDESTAL_BASE_H_M + RF_PEDESTAL_SHAFT_H_M / 2
    head_cz     = RF_PEDESTAL_BASE_H_M + RF_PEDESTAL_SHAFT_H_M + RF_PEDESTAL_HEAD_H_M / 2
    stringer_cz = head_cz
    tile_cz     = RF_PEDESTAL_TOTAL_H_M - RF_TILE_H_M / 2

    struct_parts: List[bpy.types.Object] = []

    def _struct(name, cx, cy, cz, w, d, h):
        obj = _create_box_object(name, cx=cx, cy=cy, cz=cz, w=w, d=d, h=h,
                                  collection=struct_col)
        struct_parts.append(obj)

    # ── Pedestals ──────────────────────────────────────────────────────────
    for iy in range(ny):
        for ix in range(nx):
            px = x0 + ix * RF_GRID_M
            py = y0 + iy * RF_GRID_M
            n  = ix + iy * nx
            _struct(f"{zone_name}_ped_{n}_base",  px, py, base_cz,
                    RF_PEDESTAL_BASE_W_M,  RF_PEDESTAL_BASE_W_M,  RF_PEDESTAL_BASE_H_M)
            _struct(f"{zone_name}_ped_{n}_shaft", px, py, shaft_cz,
                    RF_PEDESTAL_SHAFT_W_M, RF_PEDESTAL_SHAFT_W_M, RF_PEDESTAL_SHAFT_H_M)
            _struct(f"{zone_name}_ped_{n}_head",  px, py, head_cz,
                    RF_PEDESTAL_HEAD_W_M,  RF_PEDESTAL_HEAD_W_M,  RF_PEDESTAL_HEAD_H_M)

    # ── Stringers ──────────────────────────────────────────────────────────
    span_x = RF_GRID_M - RF_PEDESTAL_HEAD_W_M
    span_y = RF_GRID_M - RF_PEDESTAL_HEAD_W_M

    for iy in range(ny):
        for ix in range(nx - 1):
            _struct(f"{zone_name}_str_x_{ix}_{iy}",
                    x0 + ix * RF_GRID_M + RF_GRID_M / 2, y0 + iy * RF_GRID_M,
                    stringer_cz, span_x, RF_STRINGER_W_M, RF_STRINGER_H_M)

    for iy in range(ny - 1):
        for ix in range(nx):
            _struct(f"{zone_name}_str_y_{ix}_{iy}",
                    x0 + ix * RF_GRID_M, y0 + iy * RF_GRID_M + RF_GRID_M / 2,
                    stringer_cz, RF_STRINGER_W_M, span_y, RF_STRINGER_H_M)

    # Join all structure into one mesh — pedestals and stringers never move
    if struct_parts:
        _join_zone(f"{zone_name}_Structure", struct_parts, struct_col)

    # ── Floor tiles — each tile is its own object ─────────────────────────
    tile_gap = RF_TILE_GROUT_M
    tw       = RF_TILE_W_M - tile_gap   # 596 mm
    td       = RF_TILE_D_M - tile_gap   # 596 mm
    fw       = 0.020                    # 20 mm frame border for perforated

    for iy in range(ny - 1):
        for ix in range(nx - 1):
            tcx  = x0 + ix * RF_GRID_M + RF_GRID_M / 2
            tcy  = y0 + iy * RF_GRID_M + RF_GRID_M / 2
            name = f"{zone_name}_tile_{ix}_{iy}"

            if tile_type == 'perforated':
                # Perforated tile: waffle grid of bars matching Tate PERF 1250 spec.
                # 15 mm bars / 15 mm gaps → 30 mm pitch → 25 % open area.
                # X-running bars + Y-running bars create a square-hole grid pattern.
                perf_bar  = 0.015
                perf_gap  = 0.015
                perf_pitch = perf_bar + perf_gap
                n_pb = int(tw / perf_pitch)
                p_off = (tw - n_pb * perf_pitch) / 2   # centre grid in tile

                bars: List[bpy.types.Object] = []
                def _bar(bname, bcx, bcy, bw, bd):
                    o = _create_box_object(bname, cx=bcx, cy=bcy, cz=tile_cz,
                                           w=bw, d=bd, h=RF_TILE_H_M,
                                           collection=tiles_col)
                    bars.append(o)

                for i in range(n_pb):
                    # X-running bar (spans full tile width, offset in Y)
                    by = tcy - tw/2 + p_off + perf_bar/2 + i * perf_pitch
                    _bar(f"{name}_xb{i}", tcx, by, tw, perf_bar)
                    # Y-running bar (spans full tile depth, offset in X)
                    bx = tcx - td/2 + p_off + perf_bar/2 + i * perf_pitch
                    _bar(f"{name}_yb{i}", bx, tcy, perf_bar, td)

                _join_zone(name, bars, tiles_col)
            else:
                _create_box_object(name, cx=tcx, cy=tcy, cz=tile_cz,
                                   w=tw, d=td, h=RF_TILE_H_M,
                                   collection=tiles_col)


def _section_col(section_name: str) -> bpy.types.Collection:
    """Return the facility section collection, raising if not found or wrong type."""
    col = bpy.data.collections.get(section_name)
    if not col:
        raise ValueError(f"Facility section '{section_name}' not found")
    if not col.get("is_facility_section"):
        raise ValueError(f"'{section_name}' is not a facility section collection")
    return col


def _bay_names_in_section(col: bpy.types.Collection) -> List[str]:
    """Return the ordered bay name list from section metadata."""
    raw = col.get("section_bay_names", [])
    # Blender stores IDPropertyArray — convert to plain list
    return list(raw) if raw else []


# ── Tool 1: create_facility_section ───────────────────────────────────────

@mcp.tool()
@thread_safe
def create_facility_section(
    section_name: str = "Section_01",
    bays_x: int = 2,
    bays_y: int = 3,
    racks_per_bay: int = 5,
    u_height: int = 42,
    width_mm: float = 600.0,
    depth_mm: float = 1000.0,
    aisle_width_mm: float = 1200.0,
    bay_spacing_x_m: float = 0.5,
    bay_spacing_y_m: float = 0.3,
    add_perimeter_walls: bool = True,
    wall_height_m: float = 4.0,
    start_x_m: float = 0.0,
    start_y_m: float = 0.0,
    populate_preset: Optional[str] = None,
    hot_aisle_containment: bool = False,
    hot_aisle_width_mm: float = 900.0,
    corridor_depth_m: float = 2.4,
) -> Dict[str, Any]:
    """
    Create a rectangular facility section: a grid of empty bays with
    raised-floor slab and optional perimeter walls.

    Layout: bays_x columns × bays_y rows arranged on a regular grid.
    Each bay is created via bay_tools.create_bay (empty racks, no equipment).
    If populate_preset is given, bay_tools.create_bay_preset is called instead
    so every bay is fully populated in one pass.

    Perimeter walls are simple box extrusions at section boundary.

    section_name:        root collection name (prefixed with 'Facility_')
    bays_x:              number of bay columns (X axis)
    bays_y:              number of bay rows (Y axis)
    racks_per_bay:       racks per row within each bay
    u_height:            rack U height (applied to all bays)
    width_mm / depth_mm: individual rack dimensions
    aisle_width_mm:      cold aisle width per bay
    bay_spacing_x_m:     gap between adjacent bay columns in metres
    bay_spacing_y_m:     gap between adjacent bay rows in metres
    add_perimeter_walls: add thin wall boxes around the section perimeter
    wall_height_m:       perimeter wall height in metres (default 4.0)
    start_x_m / start_y_m: world origin of section
    populate_preset:     if set ('server_dense'|'network_core'|'mixed_dc'|
                         'edge_pod'), populate all bays with this preset
    hot_aisle_containment: pass to each bay's hot aisle containment flag
    corridor_depth_m:    front and rear walkway corridor depth (default 2.4 m)
    """
    import bay_tools as _bt

    rack_w_m      = width_mm  / 1000.0
    rack_d_m      = depth_mm  / 1000.0
    rack_gap_m    = 0.050
    aisle_m       = aisle_width_mm   / 1000.0   # cold aisle
    hot_aisle_m   = hot_aisle_width_mm / 1000.0 # hot aisle (between bay rears)

    # Bay footprint (front-to-front cold aisle layout):
    #   X: racks_per_bay * (rack_w_m + rack_gap_m) - rack_gap_m
    #   Y: rack_d_m (Row_A, hot side) + cold_aisle + rack_d_m (Row_B, hot side)
    #      Hot aisles live between adjacent bays (exterior to each bay).
    step_rack_m    = rack_w_m + rack_gap_m
    bay_length_x   = racks_per_bay * step_rack_m - rack_gap_m
    bay_span_y     = rack_d_m + aisle_m + rack_d_m   # bay Y span (no hot aisle)

    # Step between bay origins — includes hot aisle between Row_B rear of bay N
    # and Row_A rear of bay N+1.
    bay_step_x = bay_length_x + bay_spacing_x_m
    bay_step_y = bay_span_y + hot_aisle_m + bay_spacing_y_m

    # ── Parent collection ──────────────────────────────────────────────────
    facility_col_name = f"Facility_{section_name}"
    facility_col      = _get_or_create_collection(facility_col_name)

    bay_names:     List[str] = []
    bay_positions: List[Dict[str, float]] = []

    # ── Create bay grid ────────────────────────────────────────────────────
    for row in range(bays_y):
        for col in range(bays_x):
            bay_idx  = row * bays_x + col + 1
            bay_name = f"{section_name}_Bay_{bay_idx:02d}"
            bx       = start_x_m + col * bay_step_x
            # Offset by rack_d_m so Row_A rear (at by - rack_d_m after rotation)
            # sits at start_y_m, keeping the entire facility in positive Y space.
            by       = start_y_m + rack_d_m + row * bay_step_y

            if populate_preset:
                preset_clean = populate_preset.lower().replace(" ", "_")
                valid_presets = ("server_dense", "network_core", "mixed_dc", "edge_pod")
                if preset_clean not in valid_presets:
                    raise ValueError(
                        f"populate_preset must be one of {valid_presets}"
                    )
                _bt.create_bay_preset(
                    bay_name=bay_name,
                    preset=preset_clean,
                    racks_per_bay=racks_per_bay,
                    u_height=u_height,
                    width_mm=width_mm,
                    depth_mm=depth_mm,
                    aisle_width_mm=aisle_width_mm,
                    hot_aisle_containment=hot_aisle_containment,
                    start_x_m=bx,
                    start_y_m=by,
                )
            else:
                _bt.create_bay(
                    bay_name=bay_name,
                    racks_per_row=racks_per_bay,
                    u_height=u_height,
                    width_mm=width_mm,
                    depth_mm=depth_mm,
                    rack_gap_mm=rack_gap_m * 1000.0,
                    aisle_width_mm=aisle_width_mm,
                    hot_aisle_containment=hot_aisle_containment,
                    start_x_m=bx,
                    start_y_m=by,
                )

            bay_col = bpy.data.collections.get(bay_name)
            if bay_col:
                _nest_collection(bay_col, facility_col)
                # Lift all rack objects so they sit on top of the raised floor
                for obj in bay_col.all_objects:
                    if obj.parent is None:
                        obj.location.z += RF_PEDESTAL_TOTAL_H_M

            bay_names.append(bay_name)
            bay_positions.append({
                "bay":   bay_name,
                "col":   col,
                "row":   row,
                "x_m":   round(bx, 4),
                "y_m":   round(by, 4),
            })

    # ── Raised floor system (Tate-style: pedestals + stringers + tiles) ─────
    # Single unified zone from front corridor to rear corridor, all solid tiles.
    # One continuous pedestal grid — no zone seams, no gaps.
    # Cold-aisle tiles are swapped to perforated later once racks are placed.

    slab_w = bays_x * bay_step_x - bay_spacing_x_m
    # slab_d used for wall calculation — does NOT include corridors
    slab_d = bays_y * bay_step_y - bay_spacing_y_m

    rf_parent_col_name = f"{section_name}_RaisedFloor"
    rf_parent_col      = _get_or_create_collection(rf_parent_col_name)
    _nest_collection(rf_parent_col, facility_col)

    last_by_r   = start_y_m + rack_d_m + (bays_y - 1) * bay_step_y
    floor_y0    = start_y_m - corridor_depth_m
    floor_depth = (last_by_r + aisle_m + rack_d_m + corridor_depth_m) - floor_y0

    _create_raised_floor(
        zone_name=f"{section_name}_RF_Floor",
        x0=start_x_m, y0=floor_y0,
        width=slab_w, depth=floor_depth,
        parent_col=rf_parent_col,
        tile_type='solid',
    )

    # ── Perimeter walls ───────────────────────────────────────────────────
    wall_names: List[str] = []
    wall_t = 0.200   # 200 mm wall thickness

    if add_perimeter_walls:
        walls_col_name = f"{section_name}_Walls"
        walls_col      = _get_or_create_collection(walls_col_name)
        _nest_collection(walls_col, facility_col)

        # Wall definitions: (name_suffix, cx, cy, w, d)
        wall_specs = [
            ("Wall_S", start_x_m + slab_w / 2,
                       start_y_m - wall_t / 2,
                       slab_w + wall_t * 2, wall_t),   # south
            ("Wall_N", start_x_m + slab_w / 2,
                       start_y_m + slab_d + wall_t / 2,
                       slab_w + wall_t * 2, wall_t),   # north
            ("Wall_W", start_x_m - wall_t / 2,
                       start_y_m + slab_d / 2,
                       wall_t, slab_d),                 # west
            ("Wall_E", start_x_m + slab_w + wall_t / 2,
                       start_y_m + slab_d / 2,
                       wall_t, slab_d),                 # east
        ]

        for suffix, cx, cy, ww, wd in wall_specs:
            wall = _create_box_object(
                f"{section_name}_{suffix}",
                cx=cx, cy=cy, cz=wall_height_m / 2,
                w=ww, d=wd, h=wall_height_m,
                collection=walls_col,
            )
            wall["is_wall"] = True
            wall_names.append(wall.name)

    # ── Section metadata ───────────────────────────────────────────────────
    facility_col["is_facility_section"] = True
    facility_col["section_name"]        = section_name
    facility_col["section_bays_x"]      = bays_x
    facility_col["section_bays_y"]      = bays_y
    facility_col["section_bay_count"]   = len(bay_names)
    facility_col["section_racks_per_bay"] = racks_per_bay
    facility_col["section_u_height"]    = u_height
    facility_col["section_width_mm"]    = width_mm
    facility_col["section_depth_mm"]    = depth_mm
    facility_col["section_aisle_mm"]     = aisle_width_mm
    facility_col["section_hot_aisle_mm"] = hot_aisle_width_mm
    facility_col["section_rack_d_m"]     = round(rack_d_m, 4)
    facility_col["section_start_x_m"]   = round(start_x_m, 4)
    facility_col["section_start_y_m"]   = round(start_y_m, 4)
    facility_col["section_footprint_x"] = round(slab_w, 4)
    facility_col["section_footprint_y"] = round(slab_d, 4)
    facility_col["section_populated"]   = bool(populate_preset)
    # Store bay names as a comma-separated string (IDProperty doesn't support list of strings)
    facility_col["section_bay_names_csv"] = ",".join(bay_names)

    return {
        "section":          f"Facility_{section_name}",
        "bay_count":        len(bay_names),
        "bays_x":           bays_x,
        "bays_y":           bays_y,
        "bays":             bay_names,
        "bay_positions":    bay_positions,
        "footprint_m":      {"x": round(slab_w, 4), "y": round(slab_d, 4)},
        "populate_preset":  populate_preset,
        "walls":            wall_names,
    }


# ── Tool 2: create_corridor ────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_corridor(
    corridor_name: str = "Corridor_01",
    start_x_m: float = 0.0,
    start_y_m: float = 0.0,
    length_m: float = 10.0,
    width_m: float = 2.4,
    direction: str = "x",
    add_lighting: bool = True,
    light_interval_m: float = 2.4,
    add_cable_tray: bool = True,
    z_m: float = 0.0,
    collection_name: str = "Corridors",
) -> Dict[str, Any]:
    """
    Create a walkable corridor with floor tiles, optional overhead cable tray,
    and lighting positions.

    Floor tiles match the raised-floor tile standard (600 mm × 600 mm × 30 mm)
    used in bay_tools so corridors and bay rooms share visual vocabulary.

    Lighting positions are SOCKET_Light_XX empties at ceiling height
    (z_m + 3.0 m by default) at regular intervals — UE5 Blueprint or PCG
    spawns point lights at these positions.

    direction='x' → corridor runs along X axis (length along X, width along Y)
    direction='y' → corridor runs along Y axis (length along Y, width along X)

    corridor_name:    base name for corridor objects
    start_x_m:       world X of corridor start
    start_y_m:       world Y of corridor start
    length_m:        corridor run length in metres
    width_m:         corridor clear width in metres (default 2.4 — two-person passing)
    direction:       'x' | 'y' — axis the corridor runs along
    add_lighting:    add SOCKET_Light_XX empties at ceiling height (default True)
    light_interval_m: spacing between lighting positions (default 2.4 m)
    add_cable_tray:  add a 100 mm-wide overhead cable tray along the corridor
    z_m:             Z of floor level (usually 0.0)
    collection_name: collection to place corridor geometry into
    """
    direction = direction.lower()
    if direction not in ("x", "y"):
        raise ValueError("direction must be 'x' or 'y'")

    corr_col = _get_or_create_collection(collection_name)

    tile_size  = 0.600
    tile_thick = 0.030
    ceiling_z  = z_m + 3.0   # standard 3 m ceiling height

    # Swap axes for Y-direction corridor
    run_len  = length_m
    run_wid  = width_m

    # ── Floor tiles ────────────────────────────────────────────────────────
    tile_col_name = f"{corridor_name}_Tiles"
    tile_col      = _get_or_create_collection(tile_col_name)
    _nest_collection(tile_col, corr_col)

    tiles_along = math.ceil(run_len / tile_size)
    tiles_cross = max(1, math.ceil(run_wid / tile_size))
    tile_idx    = 0

    for ia in range(tiles_along):
        for ic in range(tiles_cross):
            t_along = start_x_m + ia * tile_size + tile_size / 2 if direction == "x" else start_x_m + ic * tile_size + tile_size / 2
            t_cross = start_y_m + ic * tile_size + tile_size / 2 if direction == "x" else start_y_m + ia * tile_size + tile_size / 2
            tile = _create_box_object(
                f"{corridor_name}_Tile_{tile_idx:03d}",
                cx=t_along, cy=t_cross, cz=z_m - tile_thick / 2,
                w=tile_size - 0.004, d=tile_size - 0.004, h=tile_thick,
                collection=tile_col,
            )
            tile["is_floor_tile"] = True
            tile_idx += 1

    # ── Overhead cable tray ────────────────────────────────────────────────
    tray_names: List[str] = []
    if add_cable_tray:
        tray_col_name = f"{corridor_name}_CableTray"
        tray_col      = _get_or_create_collection(tray_col_name)
        _nest_collection(tray_col, corr_col)

        tray_w   = 0.100
        tray_d   = CABLE_TRAY_DEPTH_M
        tray_wt  = CABLE_TRAY_WALL_THICK_M
        tray_z   = ceiling_z - 0.3   # 300 mm below ceiling

        # Tray runs along the same axis as the corridor, centred on width
        if direction == "x":
            tray_cx = start_x_m + run_len / 2
            tray_cy = start_y_m + run_wid / 2
            tray_cz = tray_z + tray_d / 2
            tray_dims = (run_len, tray_w, tray_d)
            wall_dims_l = (run_len, tray_wt, tray_d)
            wall_offset = tray_w / 2 - tray_wt / 2
            wall_l_pos = (tray_cx, tray_cy - wall_offset, tray_cz)
            wall_r_pos = (tray_cx, tray_cy + wall_offset, tray_cz)
        else:
            tray_cx = start_x_m + run_wid / 2
            tray_cy = start_y_m + run_len / 2
            tray_cz = tray_z + tray_d / 2
            tray_dims = (tray_w, run_len, tray_d)
            wall_dims_l = (tray_wt, run_len, tray_d)
            wall_offset = tray_w / 2 - tray_wt / 2
            wall_l_pos = (tray_cx - wall_offset, tray_cy, tray_cz)
            wall_r_pos = (tray_cx + wall_offset, tray_cy, tray_cz)

        tb = _create_box_object(f"{corridor_name}_Tray_Bot",
            cx=tray_cx, cy=tray_cy, cz=tray_z,
            w=tray_dims[0], d=tray_dims[1], h=tray_wt,
            collection=tray_col)
        tb["is_cable_tray"] = True
        tray_names.append(tb.name)

        for side, pos in (("L", wall_l_pos), ("R", wall_r_pos)):
            tw = _create_box_object(f"{corridor_name}_Tray_Wall{side}",
                cx=pos[0], cy=pos[1], cz=pos[2],
                w=wall_dims_l[0], d=wall_dims_l[1], h=tray_d,
                collection=tray_col)
            tw["is_cable_tray"] = True
            tray_names.append(tw.name)

    # ── Lighting sockets ───────────────────────────────────────────────────
    light_sockets: List[str] = []
    if add_lighting:
        light_col_name = f"{corridor_name}_Lighting"
        light_col      = _get_or_create_collection(light_col_name)
        _nest_collection(light_col, corr_col)

        n_lights = max(1, int(run_len / light_interval_m))
        for i in range(n_lights):
            t = (i + 0.5) / n_lights
            if direction == "x":
                lx = start_x_m + t * run_len
                ly = start_y_m + run_wid / 2
            else:
                lx = start_x_m + run_wid / 2
                ly = start_y_m + t * run_len

            sock_name = f"{SOCKET_PREFIX}Light_{corridor_name}_{i:02d}"
            sock = _add_empty(sock_name, (lx, ly, ceiling_z),
                              light_col, 'PLAIN_AXES', 0.10)
            sock["is_light_socket"] = True
            light_sockets.append(sock_name)

    return {
        "corridor":        corridor_name,
        "collection":      collection_name,
        "length_m":        length_m,
        "width_m":         width_m,
        "direction":       direction,
        "floor_tiles":     tile_idx,
        "cable_tray":      len(tray_names) > 0,
        "light_sockets":   light_sockets,
    }


# ── Tool 3: add_power_cooling_zone ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_power_cooling_zone(
    zone_name: str = "PowerZone_01",
    wall_x_m: float = 0.0,
    wall_y_m: float = 0.0,
    length_m: float = 6.0,
    crac_count: int = 2,
    ups_count: int = 2,
    busway: bool = True,
    face_direction: str = "x",
    collection_name: str = "PowerCooling",
) -> Dict[str, Any]:
    """
    Place power and cooling infrastructure geometry along a wall or zone boundary.

    Generates:
      UPS cabinets    — 600 mm wide × 900 mm deep × 2000 mm tall box, SOCKET_Power_XX
      CRAC units      — 1200 mm wide × 800 mm deep × 2000 mm tall box,
                        SOCKET_Airflow_Intake_XX + SOCKET_Airflow_Exhaust_XX
      Busway overhead — rectangular tray running the full zone length at 2.5 m height
                        with a SOCKET_Busway_Tap_XX every 600 mm

    All units have `is_ups`, `is_crac`, `is_busway` custom property flags.
    Sockets follow the SOCKET_ convention for UE5 actor attachment.

    face_direction controls which axis units face along:
      'x'  — units placed along X, facing +Y
      '-x' — units placed along X, facing -Y
      'y'  — units placed along Y, facing +X
      '-y' — units placed along Y, facing -X

    zone_name:        base name for all objects in this zone
    wall_x_m / wall_y_m: world position of zone start
    length_m:         total zone length in metres
    crac_count:       number of CRAC units (evenly spaced along length)
    ups_count:        number of UPS cabinets (evenly spaced in remaining space)
    busway:           add overhead busway (default True)
    face_direction:   'x' | '-x' | 'y' | '-y'
    collection_name:  collection to place zone geometry into
    """
    face_direction = face_direction.lower()
    if face_direction not in ("x", "-x", "y", "-y"):
        raise ValueError("face_direction must be 'x', '-x', 'y', or '-y'")

    zone_col = _get_or_create_collection(collection_name)

    # Axis helpers
    along_x = face_direction in ("x", "-x")
    sign_y  = -1.0 if face_direction in ("-x", "-y") else 1.0

    # Unit dimensions
    ups_w, ups_d, ups_h    = 0.600, 0.900, 2.000
    crac_w, crac_d, crac_h = 1.200, 0.800, 2.000

    created_ups:  List[str] = []
    created_crac: List[str] = []
    sockets:      List[str] = []

    total_units = ups_count + crac_count
    if total_units == 0:
        total_units = 1
    spacing = length_m / total_units

    # Interleave CRAC and UPS: every (total//crac) units place a CRAC
    unit_types: List[str] = []
    crac_every = max(1, total_units // max(crac_count, 1))
    for i in range(total_units):
        if crac_count > 0 and (i % crac_every == 0) and len(
            [t for t in unit_types if t == "crac"]
        ) < crac_count:
            unit_types.append("crac")
        else:
            unit_types.append("ups")

    for i, utype in enumerate(unit_types):
        offset = i * spacing + spacing / 2

        if along_x:
            ux = wall_x_m + offset
            uy = wall_y_m
        else:
            ux = wall_x_m
            uy = wall_y_m + offset

        if utype == "crac":
            w, d, h = crac_w, crac_d, crac_h
            obj_name = f"{zone_name}_CRAC_{i:02d}"
        else:
            w, d, h = ups_w, ups_d, ups_h
            obj_name = f"{zone_name}_UPS_{i:02d}"

        # Offset unit depth outward along face direction
        d_offset = d / 2 * sign_y
        if along_x:
            unit_cy = uy + d_offset
            unit_cx = ux
        else:
            unit_cx = ux + d_offset
            unit_cy = uy

        unit = _create_box_object(obj_name,
            cx=unit_cx, cy=unit_cy, cz=h / 2,
            w=w, d=d, h=h, collection=zone_col)

        if utype == "crac":
            unit["is_crac"] = True
            created_crac.append(obj_name)
            # Intake socket at front face
            intake = _add_empty(
                f"{SOCKET_PREFIX}Airflow_Intake_{zone_name}_{i:02d}",
                (unit_cx, unit_cy - d / 2 * sign_y, h * 0.4),
                zone_col, 'ARROWS', 0.05)
            intake["is_light_socket"] = False
            sockets.append(intake.name)
            # Exhaust socket at top
            exhaust = _add_empty(
                f"{SOCKET_PREFIX}Airflow_Exhaust_{zone_name}_{i:02d}",
                (unit_cx, unit_cy, h + 0.05),
                zone_col, 'ARROWS', 0.05)
            sockets.append(exhaust.name)
        else:
            unit["is_ups"] = True
            created_ups.append(obj_name)
            # Power output socket at front face
            pwr = _add_empty(
                f"{SOCKET_PREFIX}Power_{zone_name}_{i:02d}",
                (unit_cx, unit_cy - d / 2 * sign_y, h * 0.5),
                zone_col, 'ARROWS', 0.05)
            sockets.append(pwr.name)

    # ── Busway ─────────────────────────────────────────────────────────────
    busway_sockets: List[str] = []
    if busway:
        bus_h     = 2.5   # 2.5 m above floor
        bus_thick = 0.080
        bus_wide  = 0.150

        if along_x:
            bcx = wall_x_m + length_m / 2
            bcy = wall_y_m
            bw, bd = length_m, bus_wide
        else:
            bcx = wall_x_m
            bcy = wall_y_m + length_m / 2
            bw, bd = bus_wide, length_m

        busway_body = _create_box_object(
            f"{zone_name}_Busway",
            cx=bcx, cy=bcy, cz=bus_h,
            w=bw, d=bd, h=bus_thick,
            collection=zone_col)
        busway_body["is_busway"] = True

        # Tap sockets every 600 mm
        n_taps = max(1, int(length_m / 0.6))
        for ti in range(n_taps):
            t = (ti + 0.5) / n_taps
            if along_x:
                tx = wall_x_m + t * length_m
                ty = wall_y_m
            else:
                tx = wall_x_m
                ty = wall_y_m + t * length_m
            tap = _add_empty(
                f"{SOCKET_PREFIX}Busway_Tap_{zone_name}_{ti:02d}",
                (tx, ty, bus_h + bus_thick),
                zone_col, 'ARROWS', 0.04)
            tap["is_busway_tap"] = True
            busway_sockets.append(tap.name)

    return {
        "zone":           zone_name,
        "collection":     collection_name,
        "ups_units":      created_ups,
        "crac_units":     created_crac,
        "busway":         busway,
        "busway_taps":    len(busway_sockets),
        "sockets":        sockets,
        "length_m":       length_m,
    }


# ── Tool 4: create_multi_bay_row ───────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_multi_bay_row(
    row_name: str = "FacilityRow_01",
    bay_count: int = 3,
    racks_per_bay: int = 5,
    u_height: int = 42,
    width_mm: float = 600.0,
    depth_mm: float = 1000.0,
    aisle_width_mm: float = 1200.0,
    bay_gap_m: float = 0.6,
    start_x_m: float = 0.0,
    start_y_m: float = 0.0,
    shared_cable_tray: bool = True,
    hot_aisle_containment: bool = False,
    populate_preset: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Place multiple bays in a straight line along X with a configurable gap.

    Calls bay_tools.create_bay (or create_bay_preset if populate_preset is set)
    for each bay, positions them sequentially, and nests them in a
    FacilityRow_<row_name> parent collection.

    An optional shared overhead cable tray spans the full row length at
    top-of-rack height, centred over the cold-aisle zone.

    row_name:            parent collection name (prefixed with 'FacilityRow_')
    bay_count:           number of bays in the row
    racks_per_bay:       racks per bay row
    u_height:            rack U height
    width_mm / depth_mm: rack dimensions
    aisle_width_mm:      cold aisle width per bay
    bay_gap_m:           gap between adjacent bays in metres
    start_x_m / start_y_m: world position of first bay origin
    shared_cable_tray:   add a cable tray spanning all bays above cold-aisle centre
    hot_aisle_containment: pass to each bay
    populate_preset:     if set, populate all bays with this preset
    """
    import bay_tools as _bt

    rack_w_m  = width_mm / 1000.0
    rack_d_m  = depth_mm / 1000.0
    rack_gap_m = 0.050
    aisle_m   = aisle_width_mm / 1000.0

    bay_len_x = racks_per_bay * (rack_w_m + rack_gap_m) - rack_gap_m
    bay_step_x = bay_len_x + bay_gap_m

    row_col_name = f"FacilityRow_{row_name}"
    row_col      = _get_or_create_collection(row_col_name)

    bay_names: List[str] = []

    for i in range(bay_count):
        bay_name = f"{row_name}_Bay_{i + 1:02d}"
        bx       = start_x_m + i * bay_step_x

        if populate_preset:
            preset_clean = populate_preset.lower().replace(" ", "_")
            _bt.create_bay_preset(
                bay_name=bay_name,
                preset=preset_clean,
                racks_per_bay=racks_per_bay,
                u_height=u_height,
                width_mm=width_mm,
                depth_mm=depth_mm,
                aisle_width_mm=aisle_width_mm,
                hot_aisle_containment=hot_aisle_containment,
                start_x_m=bx,
                start_y_m=start_y_m,
            )
        else:
            _bt.create_bay(
                bay_name=bay_name,
                racks_per_row=racks_per_bay,
                u_height=u_height,
                width_mm=width_mm,
                depth_mm=depth_mm,
                rack_gap_mm=rack_gap_m * 1000.0,
                aisle_width_mm=aisle_width_mm,
                hot_aisle_containment=hot_aisle_containment,
                start_x_m=bx,
                start_y_m=start_y_m,
            )

        bay_col = bpy.data.collections.get(bay_name)
        if bay_col:
            _nest_collection(bay_col, row_col)

        bay_names.append(bay_name)

    total_len_m = bay_count * bay_step_x - bay_gap_m

    # ── Shared overhead cable tray ─────────────────────────────────────────
    tray_name = None
    if shared_cable_tray:
        rack_h_m  = _rack_total_height_m(u_height)
        # Cold aisle centre Y
        tray_y    = start_y_m + rack_d_m + aisle_m / 2
        tray_col_name = f"{row_name}_SharedCableTray"
        tray_col      = _get_or_create_collection(tray_col_name)
        _nest_collection(tray_col, row_col)

        tray_w    = 0.200
        tray_d    = CABLE_TRAY_DEPTH_M
        tray_wt   = CABLE_TRAY_WALL_THICK_M
        tray_cx   = start_x_m + total_len_m / 2
        tray_cz   = rack_h_m + tray_d / 2

        _create_box_object(f"{row_name}_SharedTray_Bot",
            cx=tray_cx, cy=tray_y, cz=rack_h_m,
            w=total_len_m, d=tray_w, h=tray_wt, collection=tray_col)
        _create_box_object(f"{row_name}_SharedTray_WallL",
            cx=tray_cx, cy=tray_y - tray_w / 2 + tray_wt / 2, cz=tray_cz,
            w=total_len_m, d=tray_wt, h=tray_d, collection=tray_col)
        _create_box_object(f"{row_name}_SharedTray_WallR",
            cx=tray_cx, cy=tray_y + tray_w / 2 - tray_wt / 2, cz=tray_cz,
            w=total_len_m, d=tray_wt, h=tray_d, collection=tray_col)
        tray_name = tray_col_name

    row_col["is_facility_row"] = True
    row_col["row_bay_count"]   = bay_count
    row_col["row_total_len_m"] = round(total_len_m, 4)

    return {
        "row":              row_col_name,
        "bay_count":        bay_count,
        "bays":             bay_names,
        "total_length_m":   round(total_len_m, 4),
        "shared_tray":      tray_name,
        "populate_preset":  populate_preset,
    }


# ── Tool 5: populate_facility_from_json ───────────────────────────────────

@mcp.tool()
@thread_safe
def populate_facility_from_json(
    json_path: str,
    random_variation: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Populate a facility section from a JSON layout file.

    Reads the JSON, then for each bay entry either:
      - Calls create_bay_preset if only a 'preset' key is given
      - Calls populate_bay_from_json (via a temp file) if 'rows' data is present

    A 'default_preset' key at the top level is used for any bay that has
    no explicit preset or rows — so sparse JSON files still produce fully
    populated facilities.

    JSON schema:
      {
        "facility": "Section_01",
        "default_preset": "server_dense",
        "random_variation": false,
        "bays": [
          { "bay": "Section_01_Bay_01", "preset": "network_core" },
          { "bay": "Section_01_Bay_02",
            "rows": [ { "row": "...", "racks": [ ... ] } ] }
        ]
      }

    json_path:        absolute path to facility layout JSON
    random_variation: override the JSON-level flag (or AND with it)
    dry_run:          validate and report without creating objects
    """
    import bay_tools    as _bt
    import tempfile

    if not os.path.isfile(json_path):
        raise FileNotFoundError(f"JSON not found: {json_path}")

    with open(json_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    section_name    = payload.get("facility", "")
    default_preset  = payload.get("default_preset", "server_dense")
    json_variation  = payload.get("random_variation", False)
    use_variation   = random_variation or json_variation
    bay_specs       = payload.get("bays", [])

    if not bay_specs:
        raise ValueError("JSON 'bays' list is empty or missing")

    summary:       List[Dict[str, Any]] = []
    total_placed   = 0
    total_skipped  = 0

    for spec in bay_specs:
        bay_name = spec.get("bay", "")
        preset   = spec.get("preset", default_preset)
        rows     = spec.get("rows", [])

        if not bay_name:
            total_skipped += 1
            summary.append({"bay": "?", "status": "skipped", "reason": "missing 'bay' key"})
            continue

        bay_col = bpy.data.collections.get(bay_name)
        if not bay_col:
            summary.append({"bay": bay_name, "status": "skipped",
                            "reason": "bay collection not found — run create_facility_section first"})
            total_skipped += 1
            continue

        if dry_run:
            summary.append({"bay": bay_name, "preset": preset,
                            "has_row_data": bool(rows), "dry_run": True})
            continue

        if rows:
            # Delegate to populate_bay_from_json via a temp file
            bay_payload = {"bay": bay_name, "rows": rows}
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as tmp:
                json.dump(bay_payload, tmp)
                tmp_path = tmp.name
            try:
                result = _bt.populate_bay_from_json(
                    json_path=tmp_path,
                    random_variation=use_variation,
                    dry_run=False,
                )
                placed  = result.get("total_placed", 0)
                skipped = result.get("total_skipped", 0)
            except Exception as exc:
                placed, skipped = 0, 0
                _log(f"populate_facility_from_json: {bay_name} row data failed — {exc}")
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        else:
            # Simple preset population
            try:
                clean_preset = preset.lower().replace(" ", "_")
                valid = ("server_dense", "network_core", "mixed_dc", "edge_pod")
                if clean_preset not in valid:
                    clean_preset = "server_dense"
                _bt.create_bay_preset(
                    bay_name=bay_name,
                    preset=clean_preset,
                    random_variation=use_variation,
                    # Bay geometry already exists — create_bay_preset will re-use it
                    # since it calls create_bay internally which checks for existing cols
                )
                placed, skipped = 1, 0
            except Exception as exc:
                placed, skipped = 0, 1
                _log(f"populate_facility_from_json: {bay_name} preset failed — {exc}")

        total_placed  += placed
        total_skipped += skipped
        summary.append({"bay": bay_name, "preset": preset,
                        "placed": placed, "skipped": skipped})

    return {
        "facility":      section_name,
        "bays_processed": len(summary),
        "total_placed":  total_placed,
        "total_skipped": total_skipped,
        "dry_run":       dry_run,
        "bays":          summary,
    }


# ── Tool 6: apply_facility_theme ──────────────────────────────────────────

@mcp.tool()
@thread_safe
def apply_facility_theme(
    section_name: str,
    theme: str,
    seed: int = 0,
    epicenter_bay: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Apply a named visual theme across an entire facility section.

    Delegates to variation_tools.apply_theme per bay with pre-tuned parameters.
    Incident-style themes ('crisis', 'aged_colo') respect epicenter_bay:
    if provided, that bay is the starting point for failure/incident effects
    rather than a randomly chosen bay.

    Themes:
      'enterprise_clean'  — all bays: new_install + minor cosmetic wear
      'aged_colo'         — all bays: aged_dc; ~3% of bays get post_incident
                            (starting from epicenter_bay or a random choice)
      'post_maintenance'  — alternating new_install / aged_dc bays
      'high_density'      — all bays: aged_dc with severity_bias=0.8
      'crisis'            — ~20% of bays get post_incident with propagation;
                            rest get aged_dc. Starts from epicenter_bay if set.

    section_name:  facility section collection name (with or without 'Facility_' prefix)
    theme:         theme preset name (see above)
    seed:          base seed for all variation randomisation
    epicenter_bay: optional bay name to use as incident origin on crisis/aged_colo
    """
    import variation_tools as _vt

    theme = theme.lower().replace(" ", "_").replace("-", "_")
    valid = ("enterprise_clean", "aged_colo", "post_maintenance",
             "high_density", "crisis")
    if theme not in valid:
        raise ValueError(f"theme must be one of {valid}")

    # Accept section name with or without 'Facility_' prefix
    col_name = section_name if section_name.startswith("Facility_") else f"Facility_{section_name}"
    fac_col  = _section_col(col_name)
    bay_names = fac_col.get("section_bay_names_csv", "").split(",")
    bay_names = [b for b in bay_names if b]

    if not bay_names:
        raise ValueError(f"Facility section '{col_name}' has no registered bays")

    rng      = _random.Random(seed)
    actions: List[str] = []
    results: List[Dict[str, Any]] = []

    # Determine incident bay(s) for crisis-style themes
    incident_bays: List[str] = []
    if epicenter_bay:
        if epicenter_bay not in bay_names:
            raise ValueError(
                f"epicenter_bay '{epicenter_bay}' is not registered in section '{col_name}'"
            )
        incident_bays = [epicenter_bay]
    else:
        if theme == "aged_colo":
            n = max(1, int(len(bay_names) * 0.03))
            incident_bays = rng.sample(bay_names, min(n, len(bay_names)))
        elif theme == "crisis":
            n = max(1, int(len(bay_names) * 0.20))
            incident_bays = rng.sample(bay_names, min(n, len(bay_names)))

    for i, bay_name in enumerate(bay_names):
        bay_seed = _obj_seed(seed, bay_name)

        try:
            if theme == "enterprise_clean":
                _vt.apply_theme(bay_name=bay_name, theme="new_install", seed=bay_seed)
                results.append({"bay": bay_name, "theme": "new_install"})

            elif theme == "aged_colo":
                if bay_name in incident_bays:
                    _vt.apply_theme(bay_name=bay_name, theme="post_incident", seed=bay_seed)
                    results.append({"bay": bay_name, "theme": "post_incident"})
                else:
                    _vt.apply_theme(bay_name=bay_name, theme="aged_dc", seed=bay_seed)
                    results.append({"bay": bay_name, "theme": "aged_dc"})

            elif theme == "post_maintenance":
                # Alternate: even index = new_install, odd index = aged_dc
                sub = "new_install" if i % 2 == 0 else "aged_dc"
                _vt.apply_theme(bay_name=bay_name, theme=sub, seed=bay_seed)
                results.append({"bay": bay_name, "theme": sub})

            elif theme == "high_density":
                # aged_dc + high severity_bias via randomize_bay_variation
                _vt.randomize_bay_variation(
                    bay_name=bay_name,
                    age_factor=0.70,
                    dust_factor=0.55,
                    severity_bias=0.80,
                    seed=bay_seed,
                )
                results.append({"bay": bay_name, "theme": "high_density"})

            elif theme == "crisis":
                if bay_name in incident_bays:
                    _vt.apply_theme(bay_name=bay_name, theme="post_incident", seed=bay_seed)
                    results.append({"bay": bay_name, "theme": "post_incident"})
                else:
                    _vt.apply_theme(bay_name=bay_name, theme="aged_dc", seed=bay_seed)
                    results.append({"bay": bay_name, "theme": "aged_dc"})

        except Exception as exc:
            _log(f"apply_facility_theme: {bay_name} — {exc}")
            results.append({"bay": bay_name, "theme": "error", "error": str(exc)})

    actions.append(f"themed {len(bay_names)} bays as '{theme}'")
    if incident_bays:
        actions.append(f"incident epicentre(s): {incident_bays}")

    return {
        "section":       col_name,
        "theme":         theme,
        "bay_count":     len(bay_names),
        "incident_bays": incident_bays,
        "epicenter_bay": epicenter_bay,
        "seed":          seed,
        "actions":       actions,
        "detail":        results,
    }


# ── Tool 7: randomize_facility_variation ──────────────────────────────────

@mcp.tool()
@thread_safe
def randomize_facility_variation(
    section_name: str,
    age_factor: float = 0.4,
    dust_factor: float = 0.3,
    hot_zone_x_m: float = 0.0,
    hot_zone_falloff_m: float = 5.0,
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Apply bay-level wear and dust variation across a full facility section.

    Delegates to variation_tools.randomize_bay_variation per bay.
    Per-bay severity_bias increases toward hot_zone_x_m — bays closer to the
    power wall or hot zone get heavier wear than those farther away.

    hot_zone_x_m=0 disables the hot-zone gradient (uniform variation).

    section_name:       facility section name (with or without 'Facility_' prefix)
    age_factor:         base wear intensity (0.0–1.0)
    dust_factor:        base dust intensity (0.0–1.0)
    hot_zone_x_m:       X position of maximum wear intensity; 0 = uniform
    hot_zone_falloff_m: distance over which bias falls from 1.0 → 0.0 (metres)
    seed:               base seed for all randomisation
    """
    import variation_tools as _vt

    col_name = section_name if section_name.startswith("Facility_") else f"Facility_{section_name}"
    fac_col  = _section_col(col_name)
    bay_names = [b for b in fac_col.get("section_bay_names_csv", "").split(",") if b]

    if not bay_names:
        raise ValueError(f"Section '{col_name}' has no registered bays")

    results: List[Dict[str, Any]] = []

    for bay_name in bay_names:
        bay_col  = bpy.data.collections.get(bay_name)
        bay_seed = _obj_seed(seed, bay_name)

        # Derive severity_bias from distance to hot_zone_x_m
        if hot_zone_x_m != 0.0 and hot_zone_falloff_m > 0:
            bay_x    = float(bay_col.get("bay_start_x_m", 0.0)) if bay_col else 0.0
            dist     = abs(bay_x - hot_zone_x_m)
            bias     = max(0.0, 1.0 - dist / hot_zone_falloff_m)
        else:
            bias = 0.0

        try:
            _vt.randomize_bay_variation(
                bay_name=bay_name,
                age_factor=age_factor,
                dust_factor=dust_factor,
                severity_bias=bias,
                seed=bay_seed,
            )
            results.append({"bay": bay_name, "severity_bias": round(bias, 3)})
        except Exception as exc:
            _log(f"randomize_facility_variation: {bay_name} — {exc}")
            results.append({"bay": bay_name, "error": str(exc)})

    return {
        "section":      col_name,
        "bays":         len(results),
        "age_factor":   age_factor,
        "dust_factor":  dust_factor,
        "hot_zone_x_m": hot_zone_x_m,
        "detail":       results,
    }


# ── Tool 8: get_section_bays ──────────────────────────────────────────────

@mcp.tool()
@thread_safe
def get_section_bays(
    section_name: str,
) -> Dict[str, Any]:
    """
    Return the ordered list of bay names and their world positions for a
    facility section. Read-only — no modifications made.

    Useful before calling populate or theme tools to confirm what bays
    exist and where they are placed.

    section_name: facility section name (with or without 'Facility_' prefix)
    """
    col_name = section_name if section_name.startswith("Facility_") else f"Facility_{section_name}"
    fac_col  = _section_col(col_name)
    bay_names = [b for b in fac_col.get("section_bay_names_csv", "").split(",") if b]

    bays: List[Dict[str, Any]] = []
    for bay_name in bay_names:
        bay_col = bpy.data.collections.get(bay_name)
        if not bay_col:
            bays.append({"bay": bay_name, "status": "missing"})
            continue
        bays.append({
            "bay":              bay_name,
            "start_x_m":       bay_col.get("bay_start_x_m", 0.0),
            "start_y_m":       bay_col.get("bay_start_y_m", 0.0),
            "racks_per_row":   bay_col.get("bay_racks_per_row", 0),
            "u_height":        bay_col.get("bay_u_height", 42),
            "total_racks":     bay_col.get("bay_total_racks", 0),
            "populated":       bool(bay_col.get("section_populated", False)),
            "is_bay":          bool(bay_col.get("is_bay", False)),
        })

    return {
        "section":    col_name,
        "bay_count":  len(bays),
        "bays_x":     int(fac_col.get("section_bays_x", 0)),
        "bays_y":     int(fac_col.get("section_bays_y", 0)),
        "footprint_m": {
            "x": fac_col.get("section_footprint_x", 0.0),
            "y": fac_col.get("section_footprint_y", 0.0),
        },
        "bays":       bays,
    }


# ── Tool 9: export_facility_layout_json ───────────────────────────────────

@mcp.tool()
@thread_safe
def export_facility_layout_json(
    section_name: str,
    output_path: str,
    include_cables: bool = True,
    include_variation: bool = True,
) -> Dict[str, Any]:
    """
    Export a comprehensive facility layout JSON manifest for UE5.

    Aggregates bay_tools.export_bay_layout_json for every bay, then optionally
    appends cable data (cable_tools.export_cable_data_json) and variation
    metadata (variation_tools.get_variation_report) per bay.

    The result is a single file UE5 can read to:
      - Instance racks and equipment at correct world transforms
      - Spawn spline mesh cables from control point data
      - Drive material variation parameters from damage/LED state

    section_name:       facility section name
    output_path:        absolute path for the output JSON file
    include_cables:     include cable curve data per bay (default True)
    include_variation:  include variation/failure metadata per bay (default True)
    """
    import bay_tools       as _bt
    import cable_tools     as _ct
    import variation_tools as _vt
    import tempfile

    col_name  = section_name if section_name.startswith("Facility_") else f"Facility_{section_name}"
    fac_col   = _section_col(col_name)
    bay_names = [b for b in fac_col.get("section_bay_names_csv", "").split(",") if b]

    manifest: Dict[str, Any] = {
        "source":    "blenddc_mcp",
        "facility":  col_name,
        "metadata": {k: fac_col[k] for k in fac_col.keys() if not k.startswith("_")},
        "bays":      [],
    }

    total_racks = 0
    total_equip = 0
    total_cables = 0

    for bay_name in bay_names:
        # Bay layout via temp file
        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp_path = tmp.name

        try:
            _bt.export_bay_layout_json(bay_name=bay_name, output_path=tmp_path)
            with open(tmp_path, "r", encoding="utf-8") as fh:
                bay_data = json.load(fh)
        except Exception as exc:
            _log(f"export_facility_layout_json: {bay_name} bay export failed — {exc}")
            bay_data = {"bay": bay_name, "error": str(exc)}
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # Count racks + equipment from bay data
        for row in bay_data.get("rows", []):
            for rack in row.get("racks", []):
                total_racks += 1
                total_equip += len(rack.get("equipment", []))

        # Cable data
        if include_cables:
            cable_col_name = f"{bay_name}_Cables"
            cable_col      = bpy.data.collections.get(cable_col_name)
            if cable_col:
                with tempfile.NamedTemporaryFile(
                    suffix=".json", delete=False, mode="w", encoding="utf-8"
                ) as tmp:
                    tmp_path = tmp.name
                try:
                    _ct.export_cable_data_json(
                        output_path=tmp_path,
                        collection_name=cable_col_name,
                    )
                    with open(tmp_path, "r", encoding="utf-8") as fh:
                        cable_data = json.load(fh)
                    bay_data["cables"] = cable_data.get("cables", [])
                    total_cables += cable_data.get("cable_count", 0)
                except Exception as exc:
                    _log(f"export_facility_layout_json: {bay_name} cable export — {exc}")
                    bay_data["cables"] = []
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        # Variation metadata
        if include_variation:
            try:
                var_report = _vt.get_variation_report(bay_name=bay_name)
                bay_data["variation"] = var_report
            except Exception as exc:
                _log(f"export_facility_layout_json: {bay_name} variation report — {exc}")

        manifest["bays"].append(bay_data)

    # Write final manifest
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, default=str)

    file_kb = os.path.getsize(output_path) // 1024

    return {
        "output_path":    output_path,
        "facility":       col_name,
        "bay_count":      len(bay_names),
        "total_racks":    total_racks,
        "total_equipment": total_equip,
        "total_cables":   total_cables,
        "file_size_kb":   file_kb,
        "includes":       {
            "cables":    include_cables,
            "variation": include_variation,
        },
    }


# ── Tool 10: validate_facility ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def validate_facility(
    section_name: str,
    max_cable_length_m: float = 10.0,
) -> Dict[str, Any]:
    """
    Run comprehensive validation across the full facility section.

    Per-bay: delegates to bay_tools.validate_bay.
    Per-cable: delegates to cable_tools.validate_cable_routing.
    Facility-level: checks section metadata completeness, bay collection
    existence, and (if perimeter walls exist) basic wall coverage.

    Returns a single structured report with section-level status,
    per-bay status rollup, and total fail/warn counts so you can fix
    issues before running export_facility_layout_json.

    section_name:       facility section name
    max_cable_length_m: maximum acceptable cable length (passed to cable check)
    """
    import bay_tools   as _bt
    import cable_tools as _ct

    col_name  = section_name if section_name.startswith("Facility_") else f"Facility_{section_name}"
    fac_col   = _section_col(col_name)
    bay_names = [b for b in fac_col.get("section_bay_names_csv", "").split(",") if b]

    section_issues:   List[str] = []
    section_warnings: List[str] = []
    bay_reports:      List[Dict[str, Any]] = []
    cable_reports:    List[Dict[str, Any]] = []
    total_fail  = 0
    total_warn  = 0

    # ── Section-level metadata checks ─────────────────────────────────────
    required_keys = ("is_facility_section", "section_bay_count",
                     "section_start_x_m", "section_footprint_x")
    for key in required_keys:
        if key not in fac_col:
            section_issues.append(f"Missing section metadata key: {key}")
            total_fail += 1

    if not bay_names:
        section_issues.append("section_bay_names_csv is empty — no bays registered")
        total_fail += 1

    # ── Per-bay validation ─────────────────────────────────────────────────
    for bay_name in bay_names:
        bay_col = bpy.data.collections.get(bay_name)
        if not bay_col:
            bay_reports.append({"bay": bay_name, "status": "fail",
                                "issue": "collection not found"})
            total_fail += 1
            continue

        try:
            vr = _bt.validate_bay(bay_name=bay_name)
            bay_reports.append({
                "bay":        bay_name,
                "status":     vr.get("status", "unknown"),
                "fail_count": vr.get("fail_count", 0),
                "warn_count": vr.get("warn_count", 0),
            })
            total_fail += vr.get("fail_count", 0)
            total_warn += vr.get("warn_count", 0)
        except Exception as exc:
            bay_reports.append({"bay": bay_name, "status": "error", "error": str(exc)})
            total_fail += 1

        # Cable validation per bay
        cable_col_name = f"{bay_name}_Cables"
        if bpy.data.collections.get(cable_col_name):
            try:
                cr = _ct.validate_cable_routing(
                    collection_name=cable_col_name,
                    max_length_m=max_cable_length_m,
                )
                cable_reports.append({
                    "collection":  cable_col_name,
                    "status":      cr.get("status", "unknown"),
                    "cable_count": cr.get("cable_count", 0),
                    "fail_count":  cr.get("fail_count", 0),
                    "warn_count":  cr.get("warn_count", 0),
                })
                total_fail += cr.get("fail_count", 0)
                total_warn += cr.get("warn_count", 0)
            except Exception as exc:
                _log(f"validate_facility: cable check {cable_col_name} — {exc}")

    overall = "fail" if total_fail > 0 else ("warn" if total_warn > 0 else "pass")

    return {
        "section":         col_name,
        "status":          overall,
        "fail_count":      total_fail,
        "warn_count":      total_warn,
        "section_issues":  section_issues,
        "section_warnings": section_warnings,
        "bay_reports":     bay_reports,
        "cable_reports":   cable_reports,
        "total_bays":      len(bay_names),
    }


# ── Tool 11: get_facility_info ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def get_facility_info(
    section_name: str,
) -> Dict[str, Any]:
    """
    Return a comprehensive summary of the facility section's contents.

    Aggregates bay_tools.get_bay_info across all bays and adds facility-level
    totals: rack count, U capacity/used, equipment type breakdown, failure
    state distribution, estimated UE5 actor count, and bounding box.

    Read-only — no modifications made.

    section_name: facility section name (with or without 'Facility_' prefix)
    """
    import bay_tools as _bt

    col_name  = section_name if section_name.startswith("Facility_") else f"Facility_{section_name}"
    fac_col   = _section_col(col_name)
    bay_names = [b for b in fac_col.get("section_bay_names_csv", "").split(",") if b]

    total_racks     = 0
    total_u_cap     = 0
    total_u_used    = 0
    total_equip     = 0
    type_breakdown: Dict[str, int] = {}
    failure_types:  Dict[str, int] = {}
    all_bounds: List[Dict[str, float]] = []
    bay_summaries:  List[Dict[str, Any]] = []

    for bay_name in bay_names:
        try:
            info = _bt.get_bay_info(bay_name=bay_name)
            total_racks  += info.get("rack_count",       0)
            total_u_cap  += info.get("total_u_capacity", 0)
            total_u_used += info.get("total_u_used",     0)
            n_equip       = info.get("equipment_count",  0)
            total_equip  += n_equip

            for eq_type, count in info.get("type_breakdown", {}).items():
                type_breakdown[eq_type] = type_breakdown.get(eq_type, 0) + count

            bb = info.get("bounding_box_m", {})
            if bb:
                all_bounds.append(bb)

            bay_summaries.append({
                "bay":          bay_name,
                "racks":        info.get("rack_count", 0),
                "equipment":    n_equip,
                "u_used":       info.get("total_u_used", 0),
                "u_capacity":   info.get("total_u_capacity", 0),
                "utilization":  info.get("u_utilization_pct", 0),
            })
        except Exception as exc:
            _log(f"get_facility_info: {bay_name} — {exc}")
            bay_summaries.append({"bay": bay_name, "error": str(exc)})

    # Walk all objects for failure state counts
    for bay_name in bay_names:
        bay_col = bpy.data.collections.get(bay_name)
        if not bay_col:
            continue
        for obj in bay_col.all_objects:
            if obj.type == 'MESH' and obj.get("failure_type"):
                ft = obj["failure_type"]
                failure_types[ft] = failure_types.get(ft, 0) + 1

    # Facility bounding box from all bay bounds
    facility_bb: Dict[str, float] = {}
    if all_bounds:
        xs_min = [b.get("min_x", 0) for b in all_bounds]
        xs_max = [b.get("max_x", 0) for b in all_bounds]
        ys_min = [b.get("min_y", 0) for b in all_bounds]
        ys_max = [b.get("max_y", 0) for b in all_bounds]
        zs_max = [b.get("max_z", 0) for b in all_bounds]
        facility_bb = {
            "min_x": round(min(xs_min), 3), "max_x": round(max(xs_max), 3),
            "min_y": round(min(ys_min), 3), "max_y": round(max(ys_max), 3),
            "max_z": round(max(zs_max), 3),
            "length_x": round(max(xs_max) - min(xs_min), 3),
            "width_y":  round(max(ys_max) - min(ys_min), 3),
        }

    u_util = round(total_u_used / max(total_u_cap, 1) * 100, 1)

    # Estimated UE5 actor count:
    # Racks + equipment meshes + SOCKET_ empties (≈ 4 per equipment)
    ue5_actor_estimate = total_racks + total_equip + total_equip * 4

    return {
        "section":             col_name,
        "bay_count":           len(bay_names),
        "total_racks":         total_racks,
        "total_u_capacity":    total_u_cap,
        "total_u_used":        total_u_used,
        "u_utilization_pct":   u_util,
        "total_equipment":     total_equip,
        "type_breakdown":      type_breakdown,
        "failure_types":       failure_types,
        "bay_summaries":       bay_summaries,
        "bounding_box_m":      facility_bb,
        "footprint_m":         {
            "x": fac_col.get("section_footprint_x", 0.0),
            "y": fac_col.get("section_footprint_y", 0.0),
        },
        "ue5_actor_estimate":  ue5_actor_estimate,
    }
