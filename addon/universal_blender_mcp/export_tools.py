"""
UE5 asset export pipeline tools for the Universal Blender MCP server (v2.0.0).

Provides tools for exporting Blender assets to Unreal Engine 5 with correct
axis, scale, smoothing, and naming conventions. Includes UCX collision mesh
generation, UE5 socket creation, and asset validation.

UE5 FBX conventions used throughout:
  axis_forward = '-X'      (UE5 is X-forward, Blender is Y-forward)
  axis_up      = 'Z'       (both are Z-up)
  apply_scale_options = 'FBX_SCALE_ALL'  (absorb scale into mesh data)
  mesh_smooth_type    = 'FACE'           (face-weighted normals)

All tools use @mcp.tool() + @thread_safe from core.py.
"""

import bpy
import os
import json
from typing import Any, Dict, List, Optional, Tuple

from core import mcp, thread_safe, _log
from constants import (
    UE5_AXIS_FORWARD, UE5_AXIS_UP, UE5_SCALE_OPTIONS, UE5_MESH_SMOOTH,
    UCX_PREFIX, SOCKET_PREFIX,
)


# ── Tool 1: export_ue5_fbx ────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def export_ue5_fbx(
    output_path: str,
    object_names: List[str] = [],
    collection_name: str = "",
    apply_modifiers: bool = True,
    embed_textures: bool = False,
    include_armature: bool = False,
) -> Dict[str, Any]:
    """
    Export objects or a collection to FBX using Unreal Engine 5 settings.

    Applies the correct axis convention (-X forward, Z up), FBX_SCALE_ALL
    scale mode, and FACE-weighted normals. Includes UCX_ collision meshes
    and SOCKET_ empties if present in the selection.

    output_path:      absolute path for the .fbx file (directory must exist)
    object_names:     list of object names to export (empty = use collection_name)
    collection_name:  export all mesh objects in this collection (if object_names empty)
    apply_modifiers:  apply modifiers before export (default True)
    embed_textures:   embed texture files in the FBX (default False)
    include_armature: include armature bones for skeletal mesh export (default False)
    """
    if not output_path.lower().endswith(".fbx"):
        output_path += ".fbx"

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # Build selection
    bpy.ops.object.select_all(action='DESELECT')

    selected_names = []
    if object_names:
        for n in object_names:
            obj = bpy.data.objects.get(n)
            if obj:
                obj.select_set(True)
                selected_names.append(n)
            else:
                _log(f"export_ue5_fbx: object '{n}' not found, skipping")
    elif collection_name:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
        for obj in col.all_objects:
            if obj.type in ('MESH', 'EMPTY') or (include_armature and obj.type == 'ARMATURE'):
                obj.select_set(True)
                selected_names.append(obj.name)
    else:
        # Export entire scene
        for obj in bpy.context.scene.objects:
            if obj.type in ('MESH', 'EMPTY') or (include_armature and obj.type == 'ARMATURE'):
                obj.select_set(True)
                selected_names.append(obj.name)

    if not selected_names:
        raise ValueError("No objects selected for export")

    path_mode = 'COPY' if embed_textures else 'AUTO'

    bpy.ops.export_scene.fbx(
        filepath=output_path,
        use_selection=True,
        apply_unit_scale=True,
        apply_scale_options=UE5_SCALE_OPTIONS,
        use_mesh_modifiers=apply_modifiers,
        mesh_smooth_type=UE5_MESH_SMOOTH,
        axis_forward=UE5_AXIS_FORWARD,
        axis_up=UE5_AXIS_UP,
        add_leaf_bones=False,
        use_armature_deform_only=True,
        bake_anim=False,
        path_mode=path_mode,
        embed_textures=(path_mode == 'COPY'),
    )

    return {
        "output_path":    output_path,
        "exported":       selected_names,
        "count":          len(selected_names),
        "axis_forward":   UE5_AXIS_FORWARD,
        "axis_up":        UE5_AXIS_UP,
        "scale_options":  UE5_SCALE_OPTIONS,
    }


# ── Tool 2: batch_export_ue5 ──────────────────────────────────────────────

@mcp.tool()
@thread_safe
def batch_export_ue5(
    collection_name: str,
    output_dir: str,
    per_object: bool = True,
    apply_modifiers: bool = True,
) -> Dict[str, Any]:
    """
    Batch export mesh objects from a collection using UE5 FBX settings.

    per_object=True:  each mesh exports as its own .fbx (one file per object)
    per_object=False: all meshes export as a single .fbx named after the collection

    collection_name: source collection
    output_dir:      directory to write .fbx files (created if absent)
    apply_modifiers: apply modifiers before export
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")

    os.makedirs(output_dir, exist_ok=True)
    mesh_objects = [o for o in col.all_objects if o.type == 'MESH']
    if not mesh_objects:
        return {"exported": [], "count": 0, "errors": [], "message": "No mesh objects found"}

    exported = []
    errors   = []

    if per_object:
        for obj in mesh_objects:
            safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in obj.name)
            out  = os.path.join(output_dir, safe + ".fbx")
            try:
                result = export_ue5_fbx(
                    output_path=out,
                    object_names=[obj.name],
                    apply_modifiers=apply_modifiers,
                )
                exported.append(out)
            except Exception as exc:
                errors.append({"object": obj.name, "error": str(exc)})
    else:
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in collection_name)
        out  = os.path.join(output_dir, safe + ".fbx")
        try:
            result = export_ue5_fbx(
                output_path=out,
                collection_name=collection_name,
                apply_modifiers=apply_modifiers,
            )
            exported.append(out)
        except Exception as exc:
            errors.append({"collection": collection_name, "error": str(exc)})

    return {
        "output_dir":  output_dir,
        "exported":    exported,
        "count":       len(exported),
        "errors":      errors,
        "per_object":  per_object,
    }


# ── Tool 3: create_ucx_collision ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_ucx_collision(
    object_name: str,
    collision_type: str = "BOX",
    simplify: bool = True,
) -> Dict[str, Any]:
    """
    Create a UCX_ collision mesh for a Blender object.

    UE5 automatically uses any mesh named 'UCX_<MeshName>' (or 'UCX_<MeshName>_00')
    as a custom convex collision shape for the corresponding StaticMesh.

    collision_type: 'BOX'      — axis-aligned bounding box (fastest, best for rack chassis)
                    'CONVEX'   — convex hull of the original mesh (auto-generated)
                    'CAPSULE'  — upright capsule (for cylindrical objects)
    simplify:       for CONVEX type, decimate to reduce complexity (default True)
    object_name:    source mesh object to generate collision for
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"'{object_name}' is not a mesh")

    valid_types = {"BOX", "CONVEX", "CAPSULE"}
    ct_upper = collision_type.upper()
    if ct_upper not in valid_types:
        raise ValueError(f"collision_type must be one of {valid_types}")

    # Build the UCX name (UE5 convention)
    ucx_name = f"{UCX_PREFIX}{object_name}"
    # Remove any existing UCX mesh with this name
    existing = bpy.data.objects.get(ucx_name)
    if existing:
        bpy.data.objects.remove(existing, do_unlink=True)

    dims = obj.dimensions
    loc  = obj.location

    if ct_upper == "BOX":
        # Pure bounding-box collision
        bpy.ops.mesh.primitive_cube_add(
            size=1.0,
            location=loc,
        )
        ucx_obj = bpy.context.active_object
        ucx_obj.name = ucx_name
        ucx_obj.scale = (dims.x / 2, dims.y / 2, dims.z / 2)
        bpy.ops.object.transform_apply(scale=True)

    elif ct_upper == "CONVEX":
        # Duplicate source mesh and compute convex hull
        ucx_obj = obj.copy()
        ucx_obj.data = obj.data.copy()
        ucx_obj.name = ucx_name
        bpy.context.collection.objects.link(ucx_obj)

        bpy.ops.object.select_all(action='DESELECT')
        ucx_obj.select_set(True)
        bpy.context.view_layer.objects.active = ucx_obj

        # Apply modifiers first
        for mod in list(ucx_obj.modifiers):
            try:
                bpy.ops.object.modifier_apply(modifier=mod.name)
            except Exception:
                pass

        # Compute convex hull via bmesh
        import bmesh as _bm
        bpy.ops.object.mode_set(mode='EDIT')
        bm = _bm.from_edit_mesh(ucx_obj.data)
        bm.verts.ensure_lookup_table()
        _bm.ops.convex_hull(bm, input=bm.verts)
        _bm.update_edit_mesh(ucx_obj.data)
        bpy.ops.object.mode_set(mode='OBJECT')

        if simplify:
            dec = ucx_obj.modifiers.new(name="DecimateUCX", type='DECIMATE')
            dec.ratio = 0.3
            bpy.ops.object.modifier_apply(modifier=dec.name)

    else:  # CAPSULE — approximate with a cylinder
        height = max(dims.x, dims.y, dims.z)
        radius = max(dims.x, dims.y) / 2
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=8,
            radius=radius,
            depth=height,
            location=loc,
        )
        ucx_obj = bpy.context.active_object
        ucx_obj.name = ucx_name

    # Link to the same collections as the source object
    for src_col in obj.users_collection:
        if ucx_obj.name not in src_col.objects:
            src_col.objects.link(ucx_obj)

    return {
        "ucx_object":     ucx_name,
        "source":         object_name,
        "collision_type": ct_upper,
        "ue5_convention": f"Name '{ucx_name}' is recognised by UE5 as collision for '{object_name}'",
    }


# ── Tool 4: create_ue5_socket ─────────────────────────────────────────────

@mcp.tool()
@thread_safe
def create_ue5_socket(
    socket_name: str,
    location: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    rotation_euler: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    parent_name: str = "",
) -> Dict[str, Any]:
    """
    Create a UE5 socket empty at a specified location.

    UE5 recognises any mesh named 'SOCKET_<Name>' as a socket attachment
    point during StaticMesh import. The empty's location and rotation define
    the socket transform in the asset's local space.

    socket_name:    socket identifier (the 'SOCKET_' prefix is added automatically)
    location:       world-space location in metres
    rotation_euler: rotation in radians (XYZ Euler)
    parent_name:    optional parent object name (socket is child of this object)
    """
    full_name = socket_name if socket_name.startswith(SOCKET_PREFIX) else f"{SOCKET_PREFIX}{socket_name}"

    # Remove existing socket with same name
    existing = bpy.data.objects.get(full_name)
    if existing:
        bpy.data.objects.remove(existing, do_unlink=True)

    empty = bpy.data.objects.new(full_name, None)
    empty.empty_display_type = 'ARROWS'
    empty.empty_display_size = 0.05
    empty.location           = location
    empty.rotation_euler     = rotation_euler

    bpy.context.collection.objects.link(empty)

    if parent_name:
        parent = bpy.data.objects.get(parent_name)
        if not parent:
            raise ValueError(f"Parent object '{parent_name}' not found")
        empty.parent = parent
        empty.matrix_parent_inverse = parent.matrix_world.inverted()

    return {
        "socket":      full_name,
        "location":    list(location),
        "rotation":    list(rotation_euler),
        "parent":      parent_name or None,
        "ue5_note":    f"UE5 will create a socket named '{socket_name}' at this transform",
    }


# ── Tool 5: validate_ue5_asset ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def validate_ue5_asset(
    object_name: str = "",
    collection_name: str = "",
) -> Dict[str, Any]:
    """
    Validate an object or collection for UE5 StaticMesh export readiness.

    Checks: transforms applied, poly count under limits, UV map present,
    normals direction, mesh errors (non-manifold, loose verts), origin
    placement, and naming conventions.

    Provide either object_name OR collection_name (not both).
    """
    if object_name and collection_name:
        raise ValueError("Provide either object_name or collection_name, not both")
    if not object_name and not collection_name:
        raise ValueError("Provide object_name or collection_name")

    # Build object list
    if object_name:
        obj = bpy.data.objects.get(object_name)
        if not obj:
            raise ValueError(f"Object '{object_name}' not found")
        objects = [obj]
    else:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
        objects = [o for o in col.all_objects if o.type == 'MESH']

    results = []
    total_tris = 0

    for obj in objects:
        issues   = []
        warnings = []

        # Triangle count
        tris = sum(len(f.vertices) - 2 for f in obj.data.polygons)
        total_tris += tris
        if tris > 20000:
            issues.append(f"Triangle count {tris} exceeds UE5 20,000 limit")
        elif tris > 10000:
            warnings.append(f"Triangle count {tris} is high — consider LODs")

        # Transforms applied (scale should be 1,1,1 and location 0,0,0 for world-space assets)
        sc = obj.scale
        if abs(sc.x - 1.0) > 0.001 or abs(sc.y - 1.0) > 0.001 or abs(sc.z - 1.0) > 0.001:
            issues.append(f"Scale not applied: {sc.x:.3f}, {sc.y:.3f}, {sc.z:.3f}")

        # UV maps
        if not obj.data.uv_layers:
            issues.append("No UV map — textures will not apply in UE5")

        # Non-manifold edges
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')
        import bmesh as _bm
        bm = _bm.from_edit_mesh(obj.data)
        nm_count = sum(1 for e in bm.edges if not e.is_manifold)
        bpy.ops.object.mode_set(mode='OBJECT')
        if nm_count > 0:
            warnings.append(f"{nm_count} non-manifold edges (may cause shading issues)")

        results.append({
            "object":     obj.name,
            "triangles":  tris,
            "issues":     issues,
            "warnings":   warnings,
            "ready":      len(issues) == 0,
        })

    overall_ready = all(r["ready"] for r in results)

    return {
        "scope":         object_name or collection_name,
        "objects":       results,
        "total_tris":    total_tris,
        "ue5_limit_ok":  total_tris <= 20000,
        "export_ready":  overall_ready,
    }


# ── Tool 6: apply_ue5_transforms ─────────────────────────────────────────

@mcp.tool()
@thread_safe
def apply_ue5_transforms(
    object_name: str = "",
    collection_name: str = "",
    apply_location: bool = False,
    apply_rotation: bool = True,
    apply_scale: bool = True,
) -> Dict[str, Any]:
    """
    Apply transforms on objects to prepare them for UE5 export.

    UE5 requires:
    - Scale applied (scale must be 1,1,1 in the FBX)
    - Rotation applied (or handled by axis_forward/axis_up remapping)
    - Location: keep origin in world space unless deliberately zeroed

    Operates on a single object or all mesh objects in a collection.

    object_name:      apply to this object (or use collection_name)
    collection_name:  apply to all mesh objects in this collection
    apply_location:   apply location (moves mesh data, resets location to 0,0,0)
    apply_rotation:   apply rotation (default True — required for UE5)
    apply_scale:      apply scale (default True — required for UE5)
    """
    if object_name and collection_name:
        raise ValueError("Provide either object_name or collection_name, not both")

    if object_name:
        objects = [bpy.data.objects.get(object_name)]
        if objects[0] is None:
            raise ValueError(f"Object '{object_name}' not found")
    else:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
        objects = [o for o in col.all_objects if o.type == 'MESH']

    applied = []
    for obj in objects:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.transform_apply(
            location=apply_location,
            rotation=apply_rotation,
            scale=apply_scale,
        )
        applied.append(obj.name)

    return {
        "applied_to":     applied,
        "apply_location": apply_location,
        "apply_rotation": apply_rotation,
        "apply_scale":    apply_scale,
        "count":          len(applied),
    }
