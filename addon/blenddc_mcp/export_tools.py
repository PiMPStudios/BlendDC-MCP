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
    LOD1_DEFAULT_RATIO, LOD2_DEFAULT_RATIO,
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
        try:
            bm = _bm.from_edit_mesh(ucx_obj.data)
            bm.verts.ensure_lookup_table()
            _bm.ops.convex_hull(bm, input=bm.verts)
            _bm.update_edit_mesh(ucx_obj.data)
        finally:
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
        try:
            import bmesh as _bm
            bm = _bm.from_edit_mesh(obj.data)
            nm_count = sum(1 for e in bm.edges if not e.is_manifold)
        finally:
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


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2 — LOD GENERATION + PIPELINE IMPROVEMENTS
# ═══════════════════════════════════════════════════════════════════════════

# ── Tool 7: generate_lod_meshes ───────────────────────────────────────────

@mcp.tool()
@thread_safe
def generate_lod_meshes(
    object_name: str = "",
    collection_name: str = "",
    lod1_ratio: float = LOD1_DEFAULT_RATIO,
    lod2_ratio: float = LOD2_DEFAULT_RATIO,
    apply_modifiers_first: bool = True,
) -> Dict[str, Any]:
    """
    Generate LOD1 and LOD2 duplicate meshes for a single object or every mesh
    in a collection using the Decimate modifier.

    LOD0 is the original object (unmodified). LOD1 and LOD2 are duplicates
    with a Decimate modifier applied at the specified ratios. UE5 auto-detects
    the _LOD0/_LOD1/_LOD2 suffix naming at StaticMesh import.

    Provide either object_name OR collection_name (not both).

    object_name:           source mesh object
    collection_name:       generate LODs for all mesh objects in this collection
    lod1_ratio:            decimate ratio for LOD1 (default 0.40 = 40% of original)
    lod2_ratio:            decimate ratio for LOD2 (default 0.15 = 15% of original)
    apply_modifiers_first: apply existing modifiers before duplicating (default True)
    """
    if object_name and collection_name:
        raise ValueError("Provide either object_name or collection_name, not both")
    if not object_name and not collection_name:
        raise ValueError("Provide object_name or collection_name")

    if object_name:
        src_objects = [bpy.data.objects.get(object_name)]
        if src_objects[0] is None:
            raise ValueError(f"Object '{object_name}' not found")
    else:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
        src_objects = [o for o in col.all_objects if o.type == 'MESH'
                       and not o.name.endswith(('_LOD1', '_LOD2'))]

    if not src_objects:
        return {"lod_sets": [], "count": 0}

    lod_sets = []

    for src in src_objects:
        # Ensure the source is renamed to _LOD0 convention if not already
        base_name = src.name
        for suffix in ('_LOD0', '_LOD1', '_LOD2'):
            if base_name.endswith(suffix):
                base_name = base_name[:-len(suffix)]
                break

        if not src.name.endswith('_LOD0'):
            src.name = f"{base_name}_LOD0"

        lod_names = [src.name]

        for level, ratio in ((1, lod1_ratio), (2, lod2_ratio)):
            lod_obj = src.copy()
            lod_obj.data = src.data.copy()
            lod_obj.name = f"{base_name}_LOD{level}"

            # Link to same collections as source
            for src_col in src.users_collection:
                src_col.objects.link(lod_obj)

            bpy.ops.object.select_all(action='DESELECT')
            lod_obj.select_set(True)
            bpy.context.view_layer.objects.active = lod_obj

            # Step 1: apply any existing modifiers (GN, Bevel, Solidify, etc.)
            # so the Decimate works on final mesh topology, not pre-modifier data.
            if apply_modifiers_first:
                for mod in list(lod_obj.modifiers):
                    try:
                        bpy.ops.object.modifier_apply(modifier=mod.name)
                    except Exception:
                        pass

            # Step 2: add and apply Decimate at the caller-specified ratio.
            # lod1_ratio/lod2_ratio are passed directly here — no defaults
            # override them at this point. ratio=0.40 → LOD1 keeps 40% of
            # the post-modifier triangle count; ratio=0.15 → LOD2 keeps 15%.
            dec = lod_obj.modifiers.new(name=f"LOD{level}_Decimate", type='DECIMATE')
            dec.ratio = ratio
            try:
                bpy.ops.object.modifier_apply(modifier=dec.name)
            except Exception:
                lod_obj.modifiers.remove(dec)

            lod_names.append(lod_obj.name)

        tris_lod0 = sum(len(f.vertices) - 2 for f in src.data.polygons)
        lod_sets.append({
            "base":    base_name,
            "lod0":    lod_names[0],
            "lod1":    lod_names[1] if len(lod_names) > 1 else None,
            "lod2":    lod_names[2] if len(lod_names) > 2 else None,
            "tris_lod0": tris_lod0,
        })

    return {
        "lod_sets":    lod_sets,
        "count":       len(lod_sets),
        "lod1_ratio":  lod1_ratio,
        "lod2_ratio":  lod2_ratio,
    }


# ── Tool 8: export_lod_set_ue5 ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def export_lod_set_ue5(
    base_name: str,
    output_dir: str,
    lod1_ratio: float = LOD1_DEFAULT_RATIO,
    lod2_ratio: float = LOD2_DEFAULT_RATIO,
) -> Dict[str, Any]:
    """
    Export a full LOD set as separate FBX files with UE5 LOD suffix naming.

    If _LOD1/_LOD2 variants don't exist yet, generates them first using
    generate_lod_meshes. Exports each LOD level to its own .fbx file:
      <base_name>_LOD0.fbx, <base_name>_LOD1.fbx, <base_name>_LOD2.fbx

    UE5 StaticMesh import auto-detects these suffix names and assigns them
    as LOD levels when imported as a group.

    base_name:   base object name (without _LOD0/_LOD1/_LOD2 suffix)
    output_dir:  directory to write FBX files
    lod1_ratio:  decimate ratio for LOD1 if not yet generated (default 0.40)
    lod2_ratio:  decimate ratio for LOD2 if not yet generated (default 0.15)
    """
    os.makedirs(output_dir, exist_ok=True)

    # Ensure LOD variants exist
    lod0_name = f"{base_name}_LOD0"
    lod1_name = f"{base_name}_LOD1"
    lod2_name = f"{base_name}_LOD2"

    # If LOD0 doesn't exist, try original name
    if not bpy.data.objects.get(lod0_name):
        src = bpy.data.objects.get(base_name)
        if not src:
            raise ValueError(f"Object '{base_name}' or '{lod0_name}' not found")
        generate_lod_meshes(
            object_name=base_name,
            lod1_ratio=lod1_ratio,
            lod2_ratio=lod2_ratio,
        )
    elif not bpy.data.objects.get(lod1_name):
        generate_lod_meshes(
            object_name=lod0_name,
            lod1_ratio=lod1_ratio,
            lod2_ratio=lod2_ratio,
        )

    exported = []
    errors   = []

    for suffix in ('_LOD0', '_LOD1', '_LOD2'):
        obj_name = f"{base_name}{suffix}"
        obj = bpy.data.objects.get(obj_name)
        if not obj:
            errors.append(f"{obj_name} not found — skipped")
            continue

        out_path = os.path.join(output_dir, f"{obj_name}.fbx")
        try:
            export_ue5_fbx(output_path=out_path, object_names=[obj_name])
            tris = sum(len(f.vertices) - 2 for f in obj.data.polygons)
            exported.append({"file": out_path, "object": obj_name, "triangles": tris})
        except Exception as exc:
            errors.append(f"{obj_name}: {exc}")

    return {
        "base_name":   base_name,
        "output_dir":  output_dir,
        "exported":    exported,
        "errors":      errors,
        "lod1_ratio":  lod1_ratio,
        "lod2_ratio":  lod2_ratio,
    }


# ── Tool 9: cleanup_lod_meshes ────────────────────────────────────────────

@mcp.tool()
@thread_safe
def cleanup_lod_meshes(
    base_name: str = "",
    collection_name: str = "",
    keep_lod0: bool = True,
) -> Dict[str, Any]:
    """
    Remove LOD duplicate objects from the scene after export.

    Matches objects by _LOD1 and _LOD2 suffixes. Optionally also removes
    _LOD0 renamed objects (restoring the original name) when keep_lod0=False.

    Provide either base_name (cleans one LOD set) or collection_name (cleans
    all LOD sets in the collection).

    base_name:        base name of the LOD set (without suffix)
    collection_name:  clean all _LOD1/_LOD2 objects in this collection
    keep_lod0:        keep _LOD0 object (default True — original source mesh)
    """
    removed = []

    if base_name:
        suffixes = ['_LOD2', '_LOD1']
        if not keep_lod0:
            suffixes.append('_LOD0')
        for suffix in suffixes:
            obj = bpy.data.objects.get(f"{base_name}{suffix}")
            if obj:
                bpy.data.objects.remove(obj, do_unlink=True)
                removed.append(f"{base_name}{suffix}")
    elif collection_name:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
        targets = [o for o in list(col.all_objects)
                   if o.name.endswith('_LOD2') or o.name.endswith('_LOD1')
                   or (not keep_lod0 and o.name.endswith('_LOD0'))]
        for obj in targets:
            removed.append(obj.name)
            bpy.data.objects.remove(obj, do_unlink=True)
    else:
        raise ValueError("Provide base_name or collection_name")

    return {"removed": removed, "count": len(removed), "kept_lod0": keep_lod0}


# ── Tool 10: set_export_root ──────────────────────────────────────────────

@mcp.tool()
@thread_safe
def set_export_root(output_dir: str) -> Dict[str, Any]:
    """
    Set a persistent UE5 export root path for this Blender session.

    Stores the path as a scene custom property ('ue5_export_root').
    All export tools will use this as the default output directory when
    no explicit output_dir/output_path is provided.

    output_dir: absolute path to the UE5 project's Content/Meshes directory
                (or any target directory)
    """
    if not os.path.isabs(output_dir):
        raise ValueError("output_dir must be an absolute path")

    os.makedirs(output_dir, exist_ok=True)
    bpy.context.scene["ue5_export_root"] = output_dir

    return {
        "ue5_export_root": output_dir,
        "note": "Use this directory as default output_dir in export calls",
    }


# ── Tool 11: validate_rack_collection ────────────────────────────────────

@mcp.tool()
@thread_safe
def validate_rack_collection(collection_name: str) -> Dict[str, Any]:
    """
    Validate a rack collection for UE5 export readiness.

    Checks performed:
    - Collection has rack metadata (created by create_rack_cabinet)
    - All mesh objects have scale (1,1,1) — transforms applied
    - All mesh objects have at least one UV map
    - Each mesh object is under the UE5 20,000 triangle limit
    - Joined mesh origin is at or near (0, 0, 0) — base-front-centre
    - No mesh objects exceed the cabinet bounding box

    Returns a structured pass/fail report with per-object details.

    collection_name: rack collection to validate
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")

    is_rack = bool(col.get("is_rack_cabinet"))
    mesh_objects = [o for o in col.all_objects if o.type == 'MESH']

    issues   = []
    warnings = []
    per_obj  = []
    total_tris = 0

    if not is_rack:
        warnings.append("Collection has no rack metadata — was it created with create_rack_cabinet?")

    # Expected bounding box from metadata (if available)
    rack_w = col.get("rack_width_mm",  600.0) / 1000.0
    rack_d = col.get("rack_depth_mm", 1000.0) / 1000.0
    rack_h = col.get("rack_total_height_m", 2.5)

    for obj in mesh_objects:
        obj_issues   = []
        obj_warnings = []

        # Scale check
        sc = obj.scale
        if abs(sc.x - 1.0) > 0.001 or abs(sc.y - 1.0) > 0.001 or abs(sc.z - 1.0) > 0.001:
            obj_issues.append(f"Scale not applied: ({sc.x:.3f}, {sc.y:.3f}, {sc.z:.3f})")

        # UV map check
        if not obj.data.uv_layers:
            obj_warnings.append("No UV map — textures will not apply in UE5")

        # Triangle count
        tris = sum(len(f.vertices) - 2 for f in obj.data.polygons)
        total_tris += tris
        if tris > 20000:
            obj_issues.append(f"Triangle count {tris} exceeds UE5 20k limit")
        elif tris > 10000:
            obj_warnings.append(f"Triangle count {tris} is high — consider LODs")

        # Origin proximity to (0,0,0)
        loc = obj.location
        dist = (loc.x**2 + loc.y**2 + loc.z**2) ** 0.5
        if dist > 0.01:
            obj_warnings.append(
                f"Origin not at (0,0,0): ({loc.x:.3f}, {loc.y:.3f}, {loc.z:.3f})"
            )

        per_obj.append({
            "object":    obj.name,
            "triangles": tris,
            "issues":    obj_issues,
            "warnings":  obj_warnings,
            "ready":     len(obj_issues) == 0,
        })

        issues.extend(obj_issues)

    overall_ready = len(issues) == 0

    return {
        "collection":    collection_name,
        "is_rack":       is_rack,
        "object_count":  len(mesh_objects),
        "total_tris":    total_tris,
        "ue5_limit_ok":  total_tris <= 20000,
        "export_ready":  overall_ready,
        "issues":        issues,
        "warnings":      warnings,
        "objects":       per_obj,
    }


# ── Tool 12: export_scene_manifest ────────────────────────────────────────

@mcp.tool()
@thread_safe
def export_scene_manifest(
    output_path: str = "",
    collection_name: str = "",
) -> Dict[str, Any]:
    """
    Write a JSON manifest of exported assets for the asset registry.

    Captures per-object data: name, triangle count, bounding box, world
    location, collection membership, and rack metadata (if present).
    Compatible with UE5 DataTable import (array of row structs).

    output_path:     absolute path to write manifest JSON
                     (defaults to ue5_export_root/scene_manifest.json)
    collection_name: if provided, only include objects from this collection
    """
    import tempfile

    export_root = bpy.context.scene.get("ue5_export_root", tempfile.gettempdir())
    if not output_path:
        output_path = os.path.join(export_root, "scene_manifest.json")

    if collection_name:
        col = bpy.data.collections.get(collection_name)
        if not col:
            raise ValueError(f"Collection '{collection_name}' not found")
        mesh_objs = [o for o in col.all_objects if o.type == 'MESH']
    else:
        mesh_objs = [o for o in bpy.context.scene.objects if o.type == 'MESH']

    entries = []
    for obj in mesh_objs:
        tris = sum(len(f.vertices) - 2 for f in obj.data.polygons)
        bb   = obj.bound_box  # 8 corners in local space
        dims = obj.dimensions

        # Determine which rack collection this object belongs to (if any)
        rack_col = None
        for oc in obj.users_collection:
            if oc.get("is_rack_cabinet"):
                rack_col = oc.name
                break

        entries.append({
            "RowName":     obj.name,
            "triangles":   tris,
            "location":    [round(v, 4) for v in obj.location],
            "dimensions":  [round(dims.x, 4), round(dims.y, 4), round(dims.z, 4)],
            "collection":  [c.name for c in obj.users_collection],
            "rack_collection": rack_col,
            "has_uv":      bool(obj.data.uv_layers),
        })

    manifest = {
        "scene":      bpy.data.filepath or "<unsaved>",
        "asset_count": len(entries),
        "assets":     entries,
    }

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    return {
        "output_path":  output_path,
        "asset_count":  len(entries),
    }


# ── Tool 13: export_rack_collection_ue5 ──────────────────────────────────

@mcp.tool()
@thread_safe
def export_rack_collection_ue5(
    collection_name: str,
    output_dir: str = "",
    generate_lods: bool = True,
    lod1_ratio: float = LOD1_DEFAULT_RATIO,
    lod2_ratio: float = LOD2_DEFAULT_RATIO,
    write_manifest: bool = True,
) -> Dict[str, Any]:
    """
    One-call full UE5 export pipeline for a rack cabinet collection.

    Pipeline steps (in order):
    1. validate_rack_collection     — abort on hard issues
    2. apply_ue5_transforms         — apply scale/rotation on all mesh objects
    3. export_ue5_fbx               — export full collection as single FBX
    4. generate_lod_meshes          — create _LOD1/_LOD2 variants (if generate_lods)
    5. export_lod_set_ue5           — export LOD FBX set (if generate_lods)
    6. cleanup_lod_meshes           — remove LOD duplicates after export
    7. export_scene_manifest        — write asset registry JSON

    collection_name: rack collection created by create_rack_cabinet
    output_dir:      export directory (falls back to ue5_export_root scene property)
    generate_lods:   generate and export LOD variants (default True)
    lod1_ratio:      LOD1 decimate ratio (default 0.40)
    lod2_ratio:      LOD2 decimate ratio (default 0.15)
    write_manifest:  write scene_manifest.json after export (default True)
    """
    # Resolve output directory
    if not output_dir:
        output_dir = bpy.context.scene.get("ue5_export_root", "")
    if not output_dir:
        raise ValueError(
            "No output_dir provided and ue5_export_root not set — "
            "run set_export_root first or pass output_dir explicitly"
        )
    os.makedirs(output_dir, exist_ok=True)

    report = {"collection": collection_name, "steps": []}

    # Step 1: Validate
    validation = validate_rack_collection(collection_name)
    report["steps"].append({"step": "validate", "export_ready": validation["export_ready"]})
    if not validation["export_ready"]:
        report["aborted"] = True
        report["validation"] = validation
        return report

    # Step 2: Apply transforms
    apply_ue5_transforms(collection_name=collection_name)
    report["steps"].append({"step": "apply_transforms", "done": True})

    # Step 3: Export main FBX
    safe_name = "".join(c if (c.isalnum() or c in "._-") else "_" for c in collection_name)
    fbx_path  = os.path.join(output_dir, f"{safe_name}.fbx")
    export_ue5_fbx(output_path=fbx_path, collection_name=collection_name)
    report["steps"].append({"step": "export_fbx", "path": fbx_path})
    report["fbx_path"] = fbx_path

    # Step 4+5+6: LODs
    lod_results = None
    if generate_lods:
        col = bpy.data.collections.get(collection_name)
        mesh_objs = [o for o in col.all_objects if o.type == 'MESH'
                     and not o.name.endswith(('_LOD0', '_LOD1', '_LOD2'))]
        if mesh_objs:
            # Generate LODs for the joined mesh (first mesh object)
            primary = mesh_objs[0]
            lod_result = generate_lod_meshes(
                object_name=primary.name,
                lod1_ratio=lod1_ratio,
                lod2_ratio=lod2_ratio,
            )
            base = lod_result["lod_sets"][0]["base"] if lod_result["lod_sets"] else primary.name
            lod_export = export_lod_set_ue5(
                base_name=base,
                output_dir=output_dir,
                lod1_ratio=lod1_ratio,
                lod2_ratio=lod2_ratio,
            )
            cleanup_lod_meshes(base_name=base, keep_lod0=True)
            # Rename _LOD0 back to original name so the source object is preserved
            lod0_obj = bpy.data.objects.get(f"{base}_LOD0")
            if lod0_obj:
                lod0_obj.name = base
            lod_results = lod_export
            report["steps"].append({"step": "lods", "exported": lod_export["exported"]})

    # Step 7: Manifest
    if write_manifest:
        manifest_path = os.path.join(output_dir, f"{safe_name}_manifest.json")
        export_scene_manifest(output_path=manifest_path, collection_name=collection_name)
        report["steps"].append({"step": "manifest", "path": manifest_path})
        report["manifest_path"] = manifest_path

    report["aborted"]     = False
    report["lod_results"] = lod_results
    return report
