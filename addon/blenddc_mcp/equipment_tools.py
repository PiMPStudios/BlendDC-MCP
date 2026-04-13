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

    # ── Chassis body ───────────────────────────────────────────────────────
    chassis = _create_box_object(
        f"{name}_chassis",
        cx=0.0, cy=d / 2, cz=h / 2,
        w=w, d=d, h=h, collection=col,
    )
    parts.append(chassis)

    if qf["bezel"]:
        # ── Bezel frame strips (proud of chassis face — negative y) ───────
        # Origin is front-face-bottom-centre: y=0 is the front face, negative y
        # is proud (toward the aisle), positive y goes into the chassis.
        bz_y = -st / 2   # centre 1 mm proud of face
        bz_d = st         # 2 mm deep

        # Top border strip
        bz_top = _create_box_object(
            f"{name}_bz_top",
            cx=0.0, cy=bz_y, cz=h - h * 0.08,
            w=w - 0.006, d=bz_d, h=h * 0.12,
            collection=col,
        )
        parts.append(bz_top)

        # Bottom border strip
        bz_bot = _create_box_object(
            f"{name}_bz_bot",
            cx=0.0, cy=bz_y, cz=h * 0.08,
            w=w - 0.006, d=bz_d, h=h * 0.12,
            collection=col,
        )
        parts.append(bz_bot)

        # Right panel (LED / controls area — rightmost 14% of width)
        bz_right = _create_box_object(
            f"{name}_bz_right",
            cx=w * 0.39, cy=bz_y, cz=h / 2,
            w=w * 0.14, d=bz_d, h=h - 0.004,
            collection=col,
        )
        parts.append(bz_right)

    actual_bays = 0
    if qf["server_bays"]:
        # ── Drive bay assemblies (left 58% of bezel face) ─────────────────
        actual_bays = drive_bays
        if random_variation and drive_bays > 1:
            actual_bays = max(1, drive_bays + _random.randint(-1, 1))

        bay_area_w = w * 0.58
        bay_x0     = -(w * 0.5) + bay_area_w / 2 - 0.01
        # ultra: 10 mm deep 3D housing; high/medium: 6 mm flat surround
        bay_hsg_depth = 0.010 if qf["bay_3d"] else 0.006

        for i in range(actual_bays):
            bx = bay_x0 - bay_area_w / 2 + (bay_area_w / actual_bays) * (i + 0.5)
            bx = _jitter(bx, 0.001, random_variation)
            bay_w_single = (bay_area_w / actual_bays) - 0.002
            bay_h_dim    = h * 0.62
            bay_cz       = h / 2

            # Bay outer surround
            bay_hsg = _create_box_object(
                f"{name}_bay_hsg_{i:02d}",
                cx=bx, cy=-bay_hsg_depth / 2, cz=bay_cz,
                w=bay_w_single, d=bay_hsg_depth, h=bay_h_dim,
                collection=col,
            )
            parts.append(bay_hsg)

            # Bay inner tray face — recessed inside housing
            bay_tray = _create_box_object(
                f"{name}_bay_tray_{i:02d}",
                cx=bx, cy=-bay_hsg_depth * 0.375, cz=bay_cz,
                w=bay_w_single * 0.84, d=bay_hsg_depth * 0.5, h=bay_h_dim * 0.80,
                collection=col,
            )
            parts.append(bay_tray)

            if qf["server_bays"]:
                # Eject handle: prominent on ultra, visible tab on high/medium
                hdl_d = 0.005 if qf["bay_3d"] else 0.003
                bay_hdl = _create_box_object(
                    f"{name}_bay_hdl_{i:02d}",
                    cx=bx, cy=-bay_hsg_depth - hdl_d / 2,
                    cz=bay_cz - bay_h_dim * 0.40,
                    w=bay_w_single * 0.56, d=hdl_d, h=h * 0.052,
                    collection=col,
                )
                parts.append(bay_hdl)
                # Shadow-edge lip: thin overhang at handle top casts a crisp
                # shadow line on the handle face under directional light
                if qf["bezel"]:
                    hdl_top_z = bay_cz - bay_h_dim * 0.40 + h * 0.026
                    parts.append(_create_box_object(
                        f"{name}_bay_hdl_lip_{i:02d}",
                        cx=bx, cy=-bay_hsg_depth - hdl_d - 0.0006,
                        cz=hdl_top_z,
                        w=bay_w_single * 0.58, d=0.0015, h=0.0015,
                        collection=col,
                    ))

            if qf["bezel"]:
                # Per-bay activity LED above each bay housing
                parts.append(_create_box_object(
                    f"{name}_bay_led_{i:02d}",
                    cx=bx, cy=-bay_hsg_depth * 0.5, cz=bay_cz + bay_h_dim * 0.47,
                    w=0.004, d=0.002, h=0.004,
                    collection=col,
                ))

    if qf["bezel"]:
        # ── Top cable management bar + return channel ─────────────────────
        # Horizontal cross-bar sits proud of face; return flange creates channel
        parts.append(_create_box_object(
            f"{name}_cable_bar",
            cx=0.0, cy=-0.003, cz=h - 0.0045,
            w=w - 0.008, d=0.007, h=0.005,
            collection=col,
        ))
        parts.append(_create_box_object(
            f"{name}_cable_return",
            cx=0.0, cy=-0.007, cz=h - 0.0015,
            w=w * 0.55, d=0.003, h=0.002,
            collection=col,
        ))

    # ── Mounting ears — always present at all quality levels ───────────────
    # Total panel = 482.6 mm; body = 446 mm; each ear = (482.6 - 446) / 2 = 18.3 mm
    ear_w = (EIA_RAIL_SPAN_M - EIA_EQUIPMENT_BODY_M) / 2   # 18.3 mm
    ear_d = 0.007   # 7 mm deep
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
            # ultra: two visible screw-head bumps on ear face (M6 cap screws)
            for screw_frac in (0.30, 0.70):
                screw = _create_box_object(
                    f"{name}_ear_screw_{side_label}_{int(screw_frac*10)}",
                    cx=ear_cx, cy=-ear_d + 0.0015, cz=h * screw_frac,
                    w=0.006, d=0.002, h=0.006,
                    collection=col,
                )
                parts.append(screw)

    if qf["vents"]:
        # ── Side ventilation slots (horizontal louvre strips) ─────────────
        vent_count  = 4 if u_size == 1 else 6
        vent_h_dim  = max(0.003, h * 0.038)
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

    if qf["bezel"]:
        # ── Status LED (proud of right panel) ─────────────────────────────
        led_x = _jitter(w * 0.41, 0.003, random_variation)
        led_z = _jitter(h * 0.65, 0.005, random_variation)
        led = _create_box_object(
            f"{name}_led",
            cx=led_x, cy=-0.003, cz=led_z,
            w=0.005, d=0.003, h=0.005,
            collection=col,
        )
        parts.append(led)

        # ── Power button (proud of right panel) ───────────────────────────
        btn_z = _jitter(h * 0.35, 0.004, random_variation)
        btn = _create_box_object(
            f"{name}_pwr",
            cx=w * 0.43, cy=-0.003, cz=btn_z,
            w=0.009, d=0.003, h=0.009,
            collection=col,
        )
        parts.append(btn)

    if qf["grille"]:
        # ── Rear exhaust grille (raised tile grid on rear face) ────────────
        grille_rows = 3 if u_size == 1 else 4
        grille_cols = 8
        grille_w    = w * 0.52
        grille_h    = h * 0.52
        tile_w      = (grille_w / grille_cols) * 0.58
        tile_h      = (grille_h / grille_rows) * 0.58
        tile_d      = 0.0015

        for row in range(grille_rows):
            for col_i in range(grille_cols):
                gx = -grille_w / 2 + (col_i + 0.5) * (grille_w / grille_cols)
                gz = (h - grille_h) / 2 + (row + 0.5) * (grille_h / grille_rows)
                tile = _create_box_object(
                    f"{name}_exh_{row}_{col_i}",
                    cx=gx, cy=d + tile_d / 2, cz=gz,
                    w=tile_w, d=tile_d, h=tile_h,
                    collection=col,
                )
                parts.append(tile)

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
) -> Dict[str, Any]:
    """
    Create a 1U/2U network switch with detailed front-face geometry.

    Builds a chassis, proud bezel frame (top/bottom strips + right control
    panel), port clusters with group dividers, a status LED strip above ports,
    SFP uplink ports, management port, EIA mounting ears with screw slots,
    side ventilation louvres on both sides, and a rear exhaust tile grille.

    Depth illusion via proud geometry (negative-Y): bezel strips, port tiles,
    and LED bumps all project toward the aisle so directional light creates
    visible layering without Boolean cuts.

    name:             base name
    u_size:           rack unit height (1 or 2)
    port_count:       front-face data ports (24 or 48)
    collection_name:  Blender collection
    random_variation: randomize LED/port positions and per-unit material
    """
    # ── Quality flags ─────────────────────────────────────────────────────
    qf = QUALITY_TIERS.get(quality, QUALITY_TIERS["high"])

    h  = u_size * RACK_U_M
    w  = EIA_EQUIPMENT_BODY_M   # 446 mm body
    d  = 0.300
    st = RACK_SHEET_THICK_M

    col   = _get_or_create_collection(collection_name)
    parts: List[bpy.types.Object] = []
    rv    = random_variation

    # ── Chassis body ──────────────────────────────────────────────────────
    chassis = _create_box_object(
        f"{name}_chassis",
        cx=0.0, cy=d / 2, cz=h / 2,
        w=w, d=d, h=h, collection=col,
    )
    parts.append(chassis)

    if qf["bezel"]:
        bz_y = -st / 2
        bz_d = st
        parts.append(_create_box_object(f"{name}_bz_top", cx=0.0, cy=bz_y,
            cz=h - h * 0.10, w=w - 0.006, d=bz_d, h=h * 0.16, collection=col))
        parts.append(_create_box_object(f"{name}_bz_bot", cx=0.0, cy=bz_y,
            cz=h * 0.10, w=w - 0.006, d=bz_d, h=h * 0.16, collection=col))
        parts.append(_create_box_object(f"{name}_bz_right", cx=w * 0.40, cy=bz_y,
            cz=h / 2, w=w * 0.14, d=bz_d, h=h - 0.004, collection=col))

    if qf["server_bays"]:
        # ── Port clusters ─────────────────────────────────────────────────
        ports_per_row = min(port_count, 24)
        rows          = max(1, (port_count + ports_per_row - 1) // ports_per_row)
        port_area_w   = w * 0.74
        port_area_h   = h * 0.46
        port_w        = port_area_w / ports_per_row
        port_h        = port_area_h / rows
        port_cz_base  = h * 0.28

        for row in range(rows):
            for p in range(ports_per_row):
                idx = row * ports_per_row + p
                if idx >= port_count:
                    break
                px = -(port_area_w / 2) + p * port_w + port_w / 2
                pz = port_cz_base + row * (port_h + 0.002)
                px = _jitter(px, 0.0003, rv)
                pz = _jitter(pz, 0.0003, rv)
                parts.append(_create_box_object(f"{name}_port_{row}_{p:02d}",
                    cx=px, cy=-0.002, cz=pz, w=port_w * 0.65, d=0.004,
                    h=port_h * 0.72, collection=col))

        # ── Port group dividers ───────────────────────────────────────────
        group_size = 12 if port_count >= 48 else 8
        num_groups = ports_per_row // group_size
        for g in range(1, num_groups):
            sep_x = -(port_area_w / 2) + g * group_size * port_w
            parts.append(_create_box_object(f"{name}_sep_{g}",
                cx=sep_x, cy=-0.0015, cz=h * 0.50,
                w=0.002, d=0.004, h=h * 0.72, collection=col))

        # ── Per-port status LEDs ───────────────────────────────────────────
        # 48-port: every other port (stride 2) + slightly smaller to avoid clutter
        # 24-port: one per port
        led_row_z  = port_cz_base + rows * (port_h + 0.002) + h * 0.035
        led_stride = 2 if port_count >= 48 else 1
        led_w_fac  = 0.38 if port_count >= 48 else 0.45
        for p in range(0, ports_per_row, led_stride):
            lx = _jitter(-(port_area_w / 2) + p * port_w + port_w / 2, 0.0002, rv)
            lz = _jitter(led_row_z, 0.0003, rv)
            parts.append(_create_box_object(f"{name}_led_{p:02d}",
                cx=lx, cy=-0.0025, cz=lz,
                w=port_w * led_w_fac, d=0.0025, h=h * 0.05,
                collection=col))

        # ── SFP uplink ports ──────────────────────────────────────────────
        sfp_base_x = w * 0.38
        sfp_w_dim  = 0.011
        sfp_h_dim  = h * 0.34
        for i in range(4):
            sx = sfp_base_x - i * (sfp_w_dim + 0.003)
            parts.append(_create_box_object(f"{name}_sfp_{i}",
                cx=sx, cy=-0.002, cz=h * 0.60,
                w=sfp_w_dim, d=0.005, h=sfp_h_dim, collection=col))

        # ── SFP cage surround (raised frame around the 4-port cluster) ────
        sfp_ctr_x  = sfp_base_x - 1.5 * (sfp_w_dim + 0.003)
        sfp_span   = 4 * sfp_w_dim + 3 * 0.003
        sfp_margin = 0.003
        # Top rail
        parts.append(_create_box_object(f"{name}_sfp_top",
            cx=sfp_ctr_x, cy=-0.001,
            cz=h * 0.60 + sfp_h_dim / 2 + sfp_margin / 2,
            w=sfp_span + sfp_margin * 2, d=0.002, h=sfp_margin, collection=col))
        # Bottom rail
        parts.append(_create_box_object(f"{name}_sfp_bot",
            cx=sfp_ctr_x, cy=-0.001,
            cz=h * 0.60 - sfp_h_dim / 2 - sfp_margin / 2,
            w=sfp_span + sfp_margin * 2, d=0.002, h=sfp_margin, collection=col))

        # ── Management port + power LED ───────────────────────────────────
        parts.append(_create_box_object(f"{name}_mgmt",
            cx=w * 0.43, cy=-0.002, cz=h * 0.28,
            w=0.010, d=0.004, h=0.008, collection=col))
        parts.append(_create_box_object(f"{name}_pwr_led",
            cx=_jitter(w * 0.44, 0.002, rv), cy=-0.003,
            cz=_jitter(h * 0.15, 0.003, rv),
            w=0.006, d=0.003, h=0.006, collection=col))

        # ── Right-panel management zone texture ───────────────────────────
        # Horizontal divider splits the right bezel into upper status / lower port
        mgmt_cx = w * 0.40
        mgmt_hw = w * 0.065   # half-width of the right panel
        parts.append(_create_box_object(f"{name}_mgmt_div",
            cx=mgmt_cx, cy=bz_y - 0.0005, cz=h * 0.50,
            w=mgmt_hw * 2, d=bz_d + 0.001, h=0.0012, collection=col))
        # Small status display rectangle (set slightly behind face — recessed look)
        parts.append(_create_box_object(f"{name}_mgmt_disp",
            cx=mgmt_cx, cy=0.0004, cz=h * 0.70,
            w=mgmt_hw * 1.6, d=0.001, h=h * 0.16, collection=col))
        # USB-A console port indicator
        parts.append(_create_box_object(f"{name}_mgmt_usb",
            cx=mgmt_cx, cy=-0.002, cz=h * 0.38,
            w=0.009, d=0.003, h=0.006, collection=col))

    # ── Mounting ears — always present ────────────────────────────────────
    ear_w = (EIA_RAIL_SPAN_M - EIA_EQUIPMENT_BODY_M) / 2
    ear_d = 0.007
    ear_h = h * 0.68
    for side_sign in (-1, 1):
        side_label = 'L' if side_sign < 0 else 'R'
        ear_cx = side_sign * (w / 2 + ear_w / 2)
        parts.append(_create_box_object(f"{name}_ear_{side_label}",
            cx=ear_cx, cy=-ear_d / 2, cz=h / 2,
            w=ear_w, d=ear_d, h=ear_h, collection=col))
        if qf["bezel"]:
            parts.append(_create_box_object(f"{name}_ear_slot_{side_label}",
                cx=ear_cx, cy=-ear_d + 0.001, cz=h * 0.50,
                w=ear_w * 0.30, d=0.001, h=h * 0.22, collection=col))

    if qf["vents"]:
        # ── Side ventilation slots ────────────────────────────────────────
        vent_count   = 4 if u_size == 1 else 6
        vent_h_dim   = max(0.003, h * 0.040)
        vent_d_len   = d * 0.55
        vent_cy      = d * 0.30 + vent_d_len / 2
        vent_z_start = h * 0.20
        vent_z_span  = h * 0.60
        vent_thick   = 0.0025
        v_denom      = vent_count - 1 if vent_count > 1 else 1
        for side_sign in (-1, 1):
            side_label = 'L' if side_sign < 0 else 'R'
            vent_cx = side_sign * (w / 2 + vent_thick / 2)
            for i in range(vent_count):
                vz = _jitter(vent_z_start + i * (vent_z_span / v_denom), 0.001, rv)
                parts.append(_create_box_object(f"{name}_vent_{side_label}_{i}",
                    cx=vent_cx, cy=vent_cy, cz=vz,
                    w=vent_thick, d=vent_d_len, h=vent_h_dim, collection=col))

    if qf["grille"]:
        # ── Rear exhaust tile grille ──────────────────────────────────────
        ex_rows = 2 if u_size == 1 else 3
        ex_cols = 6
        ex_w    = w * 0.48
        ex_h    = h * 0.50
        t_w     = (ex_w / ex_cols) * 0.58
        t_h     = (ex_h / ex_rows) * 0.58
        t_d     = 0.0015
        for row in range(ex_rows):
            for ci in range(ex_cols):
                gx = -ex_w / 2 + (ci + 0.5) * (ex_w / ex_cols)
                gz = (h - ex_h) / 2 + (row + 0.5) * (ex_h / ex_rows)
                parts.append(_create_box_object(f"{name}_exh_{row}_{ci}",
                    cx=gx, cy=d + t_d / 2, cz=gz,
                    w=t_w, d=t_d, h=t_h, collection=col))

    # ── Join + origin ─────────────────────────────────────────────────────
    joined = _join_parts(parts, name)

    # ── Per-switch material variation ─────────────────────────────────────
    _var_mat = bpy.data.materials.new(f"{name}_var")
    _var_mat.use_nodes = True
    _var_bsdf = _var_mat.node_tree.nodes.get("Principled BSDF")
    if _var_bsdf:
        if random_variation:
            _base = 0.05 + _random.uniform(-0.015, 0.020)
            _var_bsdf.inputs["Base Color"].default_value = (
                max(0.03, _base + _random.uniform(-0.008, 0.008)),
                max(0.03, _base + _random.uniform(-0.008, 0.008)),
                max(0.03, _base + _random.uniform(-0.008, 0.015)),
                1.0,
            )
            _var_bsdf.inputs["Roughness"].default_value = max(0.30, min(0.70,
                0.45 + _random.uniform(-0.10, 0.15)))
            _var_bsdf.inputs["Metallic"].default_value = max(0.45, min(0.80,
                0.65 + _random.uniform(-0.10, 0.08)))
        else:
            _dirt = _random.uniform(0.0, 0.018)
            _var_bsdf.inputs["Base Color"].default_value = (
                max(0.02, 0.050 - _dirt),
                max(0.02, 0.050 - _dirt),
                max(0.02, 0.055 - _dirt * 0.5),
                1.0,
            )
            _var_bsdf.inputs["Roughness"].default_value = max(0.30, min(0.55,
                0.42 + _random.uniform(-0.05, 0.07)))
            _var_bsdf.inputs["Metallic"].default_value = max(0.53, min(0.78,
                0.63 + _random.uniform(-0.05, 0.05))
            )
    if joined.data.materials:
        joined.data.materials[0] = _var_mat
    else:
        joined.data.materials.append(_var_mat)

    # ── SOCKET_ empties ────────────────────────────────────────────────────
    sockets_created: List[str] = []
    for i in range(2):
        ux = _jitter(w * 0.35 + i * 0.025, 0.003, rv)
        up = _add_socket_empty(
            f"{name}_Uplink_{i:02d}",
            location=(ux, 0.0, h * 0.50),
            parent=joined, collection=col,
        )
        sockets_created.append(up.name)

    pwr = _add_socket_empty(
        f"{name}_Power",
        location=(w * 0.40, d, h * 0.50),
        parent=joined, collection=col,
    )
    sockets_created.append(pwr.name)

    joined["equipment_type"]   = "switch"
    joined["u_size"]           = u_size
    joined["port_count"]       = port_count
    joined["random_variation"] = random_variation
    joined["quality"]          = quality

    return {
        "object":      name,
        "collection":  collection_name,
        "u_size":      u_size,
        "port_count":  port_count,
        "sockets":     sockets_created,
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

        for row in range(rows):
            for p in range(ports_per_row):
                idx = row * ports_per_row + p
                if idx >= port_count:
                    break
                px = -(port_area_w / 2) + p * port_w + port_w / 2
                pz = port_cz_base + row * (port_h + 0.002)
                px = _jitter(px, 0.0003, rv)
                pz = _jitter(pz, 0.0003, rv)
                parts.append(_create_box_object(f"{name}_port_{idx:02d}",
                    cx=px, cy=-0.002, cz=pz,
                    w=port_w * 0.68, d=0.004, h=port_h * 0.74, collection=col))
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
    ear_d = 0.007
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
    outlet_count: int = 12,
    collection_name: str = "Equipment",
    random_variation: bool = False,
) -> Dict[str, Any]:
    """
    Create a Power Distribution Unit for rack mounting.

    pdu_type='0U':  Vertical side-mounted strip spanning the full rack interior
                    height (RACK_INTERIOR_HEIGHT_M = 1866.9 mm at 42U). Width
                    = 60 mm. Mounts on the exterior side post. u_size ignored.
    pdu_type='1U':  Horizontal shelf unit at standard u_size U height.

    Outlet tiles are arranged along the body face. SOCKET_Outlet_00..N empties
    are placed at each outlet for UE5 power cable endpoint assignment.

    name:             base name
    pdu_type:         '0U' (vertical) | '1U' (horizontal shelf)
    u_size:           U height for 1U type only (default 1)
    outlet_count:     number of outlets
    collection_name:  Blender collection
    random_variation: slightly randomize outlet spacing for visual variety
    """
    pdu_type = pdu_type.upper()
    if pdu_type not in ("0U", "1U"):
        raise ValueError("pdu_type must be '0U' or '1U'")

    col   = _get_or_create_collection(collection_name)
    parts: List[bpy.types.Object] = []
    sockets_created: List[str] = []

    if pdu_type == "0U":
        # Vertical strip — 60 mm wide × 30 mm deep × full interior height
        w_pdu = 0.060
        d_pdu = 0.030
        h_pdu = RACK_INTERIOR_HEIGHT_M

        body = _create_box_object(
            f"{name}_body",
            cx=0.0, cy=d_pdu / 2, cz=h_pdu / 2,
            w=w_pdu, d=d_pdu, h=h_pdu, collection=col,
        )
        parts.append(body)

        outlet_spacing = h_pdu / (outlet_count + 1)
        for i in range(outlet_count):
            oz = _jitter(outlet_spacing * (i + 1), 0.003, random_variation)
            ox = _jitter(0.0,                      0.002, random_variation)
            outlet = _create_box_object(
                f"{name}_outlet_{i:02d}",
                cx=ox, cy=0.004, cz=oz,
                w=0.020, d=0.005, h=0.020, collection=col,
            )
            parts.append(outlet)

        joined = _join_parts(parts, name)

        for i in range(outlet_count):
            oz = outlet_spacing * (i + 1)
            s = _add_socket_empty(
                f"{name}_Outlet_{i:02d}",
                location=(0.0, d_pdu, oz),
                parent=joined, collection=col,
            )
            sockets_created.append(s.name)

    else:  # 1U horizontal
        h_pdu = u_size * RACK_U_M
        w_pdu = EIA_EQUIPMENT_BODY_M   # 446 mm body
        d_pdu = 0.200

        body = _create_box_object(
            f"{name}_body",
            cx=0.0, cy=d_pdu / 2, cz=h_pdu / 2,
            w=w_pdu, d=d_pdu, h=h_pdu, collection=col,
        )
        parts.append(body)

        outlet_w = (w_pdu * 0.85) / outlet_count
        for i in range(outlet_count):
            ox = -(w_pdu * 0.85 / 2) + i * outlet_w + outlet_w / 2
            ox = _jitter(ox, 0.002, random_variation)
            outlet = _create_box_object(
                f"{name}_outlet_{i:02d}",
                cx=ox, cy=0.004, cz=h_pdu / 2,
                w=outlet_w * 0.70, d=0.005, h=h_pdu * 0.55, collection=col,
            )
            parts.append(outlet)

        joined = _join_parts(parts, name)

        for i in range(outlet_count):
            ox = -(w_pdu * 0.85 / 2) + i * outlet_w + outlet_w / 2
            s = _add_socket_empty(
                f"{name}_Outlet_{i:02d}",
                location=(ox, d_pdu, h_pdu / 2),
                parent=joined, collection=col,
            )
            sockets_created.append(s.name)

    joined["equipment_type"] = "pdu"
    joined["pdu_type"]       = pdu_type
    joined["outlet_count"]   = outlet_count

    return {
        "object":       name,
        "collection":   collection_name,
        "pdu_type":     pdu_type,
        "outlet_count": outlet_count,
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
    "pdu":         {"pdu_type": "1U", "outlet_count": 12},
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
            "outlet_count": 12         // pdu only (optional, default 12)
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
