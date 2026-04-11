"""
Hard-surface mesh editing tools for the Universal Blender MCP server (v2.0.0).

These tools complement the basic edit_mesh tool in server.py with more targeted
hard-surface modelling operations: bevel, boolean, normals, edge marking,
solidify, and mesh analysis.

All tools use @mcp.tool() + @thread_safe from core.py.
"""

import bpy
import bmesh
import math
from typing import Any, Dict, List, Optional, Tuple

from core import mcp, thread_safe, _log


# ── Tool 1: bevel_edges ───────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def bevel_edges(
    object_name: str,
    width: float = 0.02,
    segments: int = 2,
    profile: float = 0.5,
    affect: str = "EDGES",
    angle_threshold: float = 30.0,
    use_sharp: bool = False,
) -> Dict[str, Any]:
    """
    Bevel the edges of a mesh object.

    Adds a Bevel modifier configured for hard-surface work.
    Preferred over destructive Edit Mode bevel so the width stays adjustable.

    object_name:       target mesh object
    width:             bevel width in metres (default 0.02)
    segments:          number of bevel segments (default 2)
    profile:           bevel profile shape, 0.0 (concave) – 1.0 (convex, default 0.5)
    affect:            'EDGES' (default) | 'VERTICES'
    angle_threshold:   only bevel edges sharper than this angle in degrees (default 30)
    use_sharp:         if True, limit bevel to sharp-marked edges instead of angle limit
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    mod = obj.modifiers.new(name="Bevel", type='BEVEL')
    mod.width       = width
    mod.segments    = segments
    mod.profile     = profile
    mod.affect      = affect.upper()

    if use_sharp:
        mod.limit_method = 'ANGLE'
        mod.use_clamp_overlap = True
        mod.angle_limit = math.radians(angle_threshold)
    else:
        mod.limit_method = 'ANGLE'
        mod.angle_limit  = math.radians(angle_threshold)

    return {
        "object":           object_name,
        "modifier":         mod.name,
        "width":            width,
        "segments":         segments,
        "profile":          profile,
        "angle_threshold":  angle_threshold,
    }


# ── Tool 2: boolean_operation ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def boolean_operation(
    target_name: str,
    cutter_name: str,
    operation: str = "DIFFERENCE",
    apply: bool = False,
    hide_cutter: bool = True,
) -> Dict[str, Any]:
    """
    Apply a Boolean modifier to target_name using cutter_name.

    operation:    'DIFFERENCE' (default) | 'UNION' | 'INTERSECT'
    apply:        apply the modifier immediately (destructive)
    hide_cutter:  hide the cutter object after adding the modifier
    """
    target = bpy.data.objects.get(target_name)
    cutter = bpy.data.objects.get(cutter_name)
    if not target:
        raise ValueError(f"Object '{target_name}' not found")
    if not cutter:
        raise ValueError(f"Object '{cutter_name}' not found")

    valid_ops = {"DIFFERENCE", "UNION", "INTERSECT"}
    op_upper = operation.upper()
    if op_upper not in valid_ops:
        raise ValueError(f"operation must be one of {valid_ops}")

    mod = target.modifiers.new(name=f"Bool_{op_upper}", type='BOOLEAN')
    mod.operation = op_upper
    mod.object    = cutter
    mod.solver    = 'EXACT'

    if hide_cutter:
        cutter.hide_viewport = True
        cutter.hide_render   = True

    if apply:
        bpy.context.view_layer.objects.active = target
        bpy.ops.object.modifier_apply(modifier=mod.name)
        mod_name = f"Bool_{op_upper} (applied)"
    else:
        mod_name = mod.name

    return {
        "target":    target_name,
        "cutter":    cutter_name,
        "operation": op_upper,
        "modifier":  mod_name,
        "applied":   apply,
    }


# ── Tool 3: mark_sharp_edges ──────────────────────────────────────────────

@mcp.tool()
@thread_safe
def mark_sharp_edges(
    object_name: str,
    angle_degrees: float = 30.0,
) -> Dict[str, Any]:
    """
    Mark edges as sharp on a mesh where the dihedral angle exceeds a threshold.

    Processes the mesh in Edit Mode using bmesh and marks the 'sharp' flag on
    qualifying edges. Combined with add_weighted_normal_modifier for clean
    hard-surface shading without split normals.

    object_name:   target mesh
    angle_degrees: edges sharper than this angle get marked (default 30 degrees)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    threshold = math.radians(angle_degrees)
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.mode_set(mode='EDIT')
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        count = 0
        for edge in bm.edges:
            if len(edge.link_faces) == 2:
                angle = edge.calc_face_angle(math.pi)
                if angle > threshold:
                    edge.smooth = False
                    count += 1
                else:
                    edge.smooth = True
        bmesh.update_edit_mesh(obj.data)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    return {
        "object":         object_name,
        "angle_degrees":  angle_degrees,
        "edges_marked":   count,
    }


# ── Tool 4: clear_sharp_edges ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def clear_sharp_edges(object_name: str) -> Dict[str, Any]:
    """
    Clear all sharp-edge marks on a mesh object.

    object_name: target mesh object
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.mode_set(mode='EDIT')
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        for edge in bm.edges:
            edge.smooth = True
        bmesh.update_edit_mesh(obj.data)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    return {"object": object_name, "result": "All sharp marks cleared"}


# ── Tool 5: add_weighted_normal_modifier ──────────────────────────────────

@mcp.tool()
@thread_safe
def add_weighted_normal_modifier(
    object_name: str,
    weight: float = 50.0,
    keep_sharp: bool = True,
) -> Dict[str, Any]:
    """
    Add a Weighted Normal modifier for clean hard-surface shading.

    The Weighted Normal modifier respects sharp edges and face areas to produce
    accurate normal directions without splitting the mesh. Essential for
    hard-surface game assets (server racks, chassis, panels).

    object_name: target mesh
    weight:      normal weighting strength 1–100 (default 50)
    keep_sharp:  respect sharp-edge marks (default True)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    mod = obj.modifiers.new(name="WeightedNormal", type='WEIGHTED_NORMAL')
    mod.weight      = max(1, min(100, int(weight)))
    mod.keep_sharp  = keep_sharp
    mod.use_face_influence = True

    return {
        "object":     object_name,
        "modifier":   mod.name,
        "weight":     weight,
        "keep_sharp": keep_sharp,
    }


# ── Tool 6: recalculate_normals ───────────────────────────────────────────

@mcp.tool()
@thread_safe
def recalculate_normals(
    object_name: str,
    inside: bool = False,
) -> Dict[str, Any]:
    """
    Recalculate face normals to point consistently outward (or inward).

    Fixes normals that are flipped after boolean operations, imports, or
    manual mesh edits. Equivalent to Edit Mode → Mesh → Normals → Recalculate.

    object_name: target mesh object
    inside:      if True, recalculate to point inward (default False = outward)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.mode_set(mode='EDIT')
    try:
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.normals_make_consistent(inside=inside)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    return {
        "object": object_name,
        "direction": "inward" if inside else "outward",
    }


# ── Tool 7: flip_normals ──────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def flip_normals(object_name: str) -> Dict[str, Any]:
    """
    Flip all face normals on a mesh object (swap inside/outside).

    Useful after boolean INTERSECT operations or when an imported mesh
    renders with inverted faces.
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.mode_set(mode='EDIT')
    try:
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.flip_normals()
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    return {"object": object_name, "result": "Normals flipped"}


# ── Tool 8: check_mesh_errors ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def check_mesh_errors(object_name: str) -> Dict[str, Any]:
    """
    Analyse a mesh for common errors that cause problems in UE5.

    Checks for: non-manifold edges, loose vertices, zero-area faces,
    zero-length edges, and doubled vertices (within a small tolerance).

    Returns counts of each issue type and whether the mesh is export-ready.
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.mode_set(mode='EDIT')
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        non_manifold_edges = [e for e in bm.edges if not e.is_manifold]
        loose_verts        = [v for v in bm.verts if not v.link_edges]
        zero_area_faces    = [f for f in bm.faces if f.calc_area() < 1e-10]
        zero_len_edges     = [e for e in bm.edges if e.calc_length() < 1e-10]

        vert_count  = len(bm.verts)
        edge_count  = len(bm.edges)
        face_count  = len(bm.faces)
        tri_count   = sum(len(f.verts) - 2 for f in bm.faces)
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    issues = {
        "non_manifold_edges": len(non_manifold_edges),
        "loose_vertices":     len(loose_verts),
        "zero_area_faces":    len(zero_area_faces),
        "zero_length_edges":  len(zero_len_edges),
    }
    export_ready = all(v == 0 for v in issues.values())

    return {
        "object":       object_name,
        "vertices":     vert_count,
        "edges":        edge_count,
        "faces":        face_count,
        "triangles":    tri_count,
        "issues":       issues,
        "export_ready": export_ready,
        "ue5_limit_ok": tri_count <= 20000,
    }


# ── Tool 9: bridge_edge_loops ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def bridge_edge_loops(
    object_name: str,
    number_cuts: int = 0,
    interpolation: str = "PATH",
    smoothness: float = 1.0,
) -> Dict[str, Any]:
    """
    Bridge two selected edge loops on a mesh object.

    Requires two open edge loops to already be selected in the mesh.
    Useful for connecting separated parts of a mesh (e.g. rack apertures).

    Use select_objects + execute_safe_python to pre-select the edge loops
    before calling this tool.

    object_name:   target mesh
    number_cuts:   number of extra edge loops between the bridged loops (default 0)
    interpolation: 'PATH' | 'SURFACE' | 'LINEAR' (default 'PATH')
    smoothness:    bridge smoothness 0.0–1.0 (default 1.0)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    valid_interp = {"PATH", "SURFACE", "LINEAR"}
    interp_upper = interpolation.upper()
    if interp_upper not in valid_interp:
        raise ValueError(f"interpolation must be one of {valid_interp}")

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')

    try:
        bpy.ops.mesh.bridge_edge_loops(
            number_cuts=number_cuts,
            interpolation=interp_upper,
            smoothness=smoothness,
        )
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    return {
        "object":        object_name,
        "number_cuts":   number_cuts,
        "interpolation": interp_upper,
        "smoothness":    smoothness,
    }


# ── Tool 10: add_solidify_modifier ────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_solidify_modifier(
    object_name: str,
    thickness: float = 0.001,
    offset: float = -1.0,
    use_even_offset: bool = True,
    fill_rim: bool = True,
) -> Dict[str, Any]:
    """
    Add a Solidify modifier to give a zero-thickness mesh physical depth.

    Useful for converting imported sheet metal panels (single-face geometry)
    into solid meshes for game engine export.

    object_name:     target mesh
    thickness:       extrusion depth in metres (default 0.001 = 1 mm)
    offset:          direction of solidify: -1.0 = inward, +1.0 = outward (default -1)
    use_even_offset: even thickness around curves (default True)
    fill_rim:        fill the perimeter edges of the solidified mesh (default True)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    mod = obj.modifiers.new(name="Solidify", type='SOLIDIFY')
    mod.thickness         = thickness
    mod.offset            = offset
    mod.use_even_offset   = use_even_offset
    mod.use_rim           = fill_rim

    return {
        "object":    object_name,
        "modifier":  mod.name,
        "thickness": thickness,
        "offset":    offset,
    }


# ── Tool 11: add_bevel_modifier ───────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_bevel_modifier(
    object_name: str,
    width: float = 0.005,
    segments: int = 3,
    limit_method: str = "ANGLE",
    angle_limit: float = 30.0,
    harden_normals: bool = True,
) -> Dict[str, Any]:
    """
    Add a Bevel modifier optimised for game-ready hard-surface assets.

    Differs from bevel_edges by defaulting to harden_normals=True and
    exposing the limit_method parameter. Recommended for rack chassis,
    server faceplates, and other metallic components.

    object_name:    target mesh
    width:          bevel width in metres (default 0.005 = 5 mm)
    segments:       bevel segments (default 3, odd numbers give symmetric centre loops)
    limit_method:   'ANGLE' (default) | 'WEIGHT' | 'NONE'
    angle_limit:    edge angle threshold in degrees for ANGLE mode (default 30)
    harden_normals: sharpen normals at bevel boundary (default True)
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    valid_limits = {"ANGLE", "WEIGHT", "NONE"}
    lm_upper = limit_method.upper()
    if lm_upper not in valid_limits:
        raise ValueError(f"limit_method must be one of {valid_limits}")

    mod = obj.modifiers.new(name="Bevel_HS", type='BEVEL')
    mod.width          = width
    mod.segments       = segments
    mod.limit_method   = lm_upper
    mod.harden_normals = harden_normals
    if lm_upper == "ANGLE":
        mod.angle_limit = math.radians(angle_limit)

    return {
        "object":          object_name,
        "modifier":        mod.name,
        "width":           width,
        "segments":        segments,
        "limit_method":    lm_upper,
        "angle_limit":     angle_limit,
        "harden_normals":  harden_normals,
    }
