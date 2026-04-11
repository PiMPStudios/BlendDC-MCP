"""
FastMCP tool implementations for the Universal Blender MCP server (v2.0.0).

All tool functions are dispatched to Blender's main thread via bpy.app.timers
(thread_safe decorator) so they are safe to call from uvicorn's worker threads.

Core infrastructure (mcp instance, thread_safe, logging, middleware, get_app)
lives in core.py. New tool modules are imported at the bottom of this file for
side-effect registration.
"""

import bpy
import sys
import os
import ast as _ast
import collections as _collections
import io as _io
import contextlib as _contextlib
import json
import tempfile
import threading
import time as _time
import functools
from typing import List, Dict, Any, Tuple, Optional

from core import mcp, thread_safe, _log, get_app  # noqa: F401


# ── Tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def list_objects(limit: int = 200) -> Dict[str, Any]:
    """
    Return the names of all objects in the current scene.

    limit: maximum number of object names to return (default 200).
           Capped to prevent context window overflow on large scenes.
    """
    all_names = [obj.name for obj in bpy.data.objects]
    total = len(all_names)
    truncated = total > limit
    return {
        "objects":   all_names[:limit],
        "count":     min(total, limit),
        "total":     total,
        "truncated": truncated,
    }


@mcp.tool()
@thread_safe
def get_object_info(name: str) -> Dict[str, Any]:
    """Get location, rotation, scale, type and dimensions for an object."""
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    return {
        "name": obj.name,
        "type": obj.type,
        "location": list(obj.location),
        "rotation_euler": list(obj.rotation_euler),
        "scale": list(obj.scale),
        "dimensions": list(obj.dimensions),
        "visible": obj.visible_get(),
    }


@mcp.tool()
@thread_safe
def create_object(
    primitive: str = "cube",
    name: str = "",
    location: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    size: float = 2.0,
) -> Dict[str, Any]:
    """
    Create a mesh primitive and return its name and location.

    primitive: cube | sphere | cylinder | plane | cone | torus
    """
    ops = {
        "cube":     lambda: bpy.ops.mesh.primitive_cube_add(size=size, location=location),
        "sphere":   lambda: bpy.ops.mesh.primitive_uv_sphere_add(radius=size / 2, location=location),
        "cylinder": lambda: bpy.ops.mesh.primitive_cylinder_add(radius=size / 2, location=location),
        "plane":    lambda: bpy.ops.mesh.primitive_plane_add(size=size, location=location),
        "cone":     lambda: bpy.ops.mesh.primitive_cone_add(radius1=size / 2, location=location),
        "torus":    lambda: bpy.ops.mesh.primitive_torus_add(location=location),
    }
    key = primitive.lower()
    if key not in ops:
        raise ValueError(f"Unknown primitive '{primitive}'. Choose from: {list(ops)}")
    ops[key]()
    obj = bpy.context.active_object
    if name:
        obj.name = name
    return {"name": obj.name, "location": list(obj.location)}


@mcp.tool()
@thread_safe
def delete_object(name: str) -> str:
    """Delete an object by name."""
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    bpy.data.objects.remove(obj, do_unlink=True)
    return f"Deleted '{name}'"


@mcp.tool()
@thread_safe
def move_object(
    name: str,
    location: Tuple[float, float, float],
) -> Dict[str, Any]:
    """Move an object to an absolute world-space location."""
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    obj.location = location
    return {"name": name, "location": list(obj.location)}


@mcp.tool()
@thread_safe
def rotate_object(
    name: str,
    rotation_euler: Tuple[float, float, float],
) -> Dict[str, Any]:
    """Set object rotation in radians (XYZ Euler)."""
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    obj.rotation_euler = rotation_euler
    return {"name": name, "rotation_euler": list(obj.rotation_euler)}


@mcp.tool()
@thread_safe
def scale_object(
    name: str,
    scale: Tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> Dict[str, Any]:
    """Scale an object per-axis."""
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    obj.scale = scale
    return {"name": name, "scale": list(obj.scale)}


@mcp.tool()
@thread_safe
def duplicate_object(name: str, new_name: str = "") -> Dict[str, Any]:
    """Duplicate an object and return the new object's info."""
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    new_obj = obj.copy()
    if obj.data:
        new_obj.data = obj.data.copy()
    if new_name:
        new_obj.name = new_name
    bpy.context.collection.objects.link(new_obj)
    return {"name": new_obj.name, "location": list(new_obj.location)}


@mcp.tool()
@thread_safe
def set_object_visibility(name: str, visible: bool) -> str:
    """Show or hide an object in the viewport."""
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    obj.hide_viewport = not visible
    obj.hide_render = not visible
    return f"'{name}' {'shown' if visible else 'hidden'}"


@mcp.tool()
@thread_safe
def set_active_object(name: str) -> str:
    """Set the active (selected) object by name."""
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return f"Active object set to '{name}'"


@mcp.tool()
@thread_safe
def assign_material(
    object_name: str,
    material_name: str,
    color: Tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0),
) -> str:
    """
    Assign a material to an object, creating it if needed.

    color: RGBA in linear space, each value 0.0-1.0.
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    mat = bpy.data.materials.get(material_name) or bpy.data.materials.new(name=material_name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = color
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return f"Assigned '{material_name}' to '{object_name}'"


@mcp.tool()
@thread_safe
def set_material_color(
    material_name: str,
    color: Tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0),
) -> str:
    """Set the Base Color of an existing material's Principled BSDF node."""
    mat = bpy.data.materials.get(material_name)
    if not mat:
        raise ValueError(f"Material '{material_name}' not found")
    if not mat.use_nodes:
        mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if not bsdf:
        raise ValueError(f"Material '{material_name}' has no Principled BSDF node")
    bsdf.inputs["Base Color"].default_value = color
    return f"Color of '{material_name}' updated"


@mcp.tool()
@thread_safe
def add_light(
    light_type: str = "POINT",
    name: str = "Light",
    location: Tuple[float, float, float] = (0.0, 0.0, 5.0),
    energy: float = 1000.0,
    color: Tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> Dict[str, Any]:
    """
    Add a light to the scene.

    light_type: POINT | SUN | SPOT | AREA
    """
    valid = {"POINT", "SUN", "SPOT", "AREA"}
    if light_type.upper() not in valid:
        raise ValueError(f"light_type must be one of {valid}")
    light_data = bpy.data.lights.new(name=name, type=light_type.upper())
    light_data.energy = energy
    light_data.color = color
    light_obj = bpy.data.objects.new(name=name, object_data=light_data)
    light_obj.location = location
    bpy.context.collection.objects.link(light_obj)
    return {"name": light_obj.name, "type": light_type, "location": list(light_obj.location)}


@mcp.tool()
@thread_safe
def add_modifier(
    object_name: str,
    modifier_type: str,
    modifier_name: str = "",
) -> str:
    """
    Add a modifier to an object.

    modifier_type examples: SUBSURF, BEVEL, SOLIDIFY, MIRROR, ARRAY, BOOLEAN, DECIMATE
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    mod_name = modifier_name or modifier_type.title()
    obj.modifiers.new(name=mod_name, type=modifier_type.upper())
    return f"Added '{modifier_type}' modifier to '{object_name}'"


@mcp.tool()
@thread_safe
def apply_modifier(object_name: str, modifier_name: str) -> str:
    """Apply a modifier by name, collapsing it into the mesh."""
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=modifier_name)
    return f"Applied modifier '{modifier_name}' on '{object_name}'"


@mcp.tool()
@thread_safe
def get_scene_info() -> Dict[str, Any]:
    """Return scene-level information: name, frame range, FPS, object count."""
    scene = bpy.context.scene
    return {
        "name": scene.name,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame_current": scene.frame_current,
        "fps": scene.render.fps,
        "object_count": len(bpy.data.objects),
        "render_engine": scene.render.engine,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
    }


@mcp.tool()
@thread_safe
def set_scene_frame(frame: int) -> int:
    """Set the current animation frame. Returns the new frame number."""
    bpy.context.scene.frame_set(frame)
    return bpy.context.scene.frame_current


@mcp.tool()
@thread_safe
def render_preview(resolution_x: int = 512, resolution_y: int = 512) -> str:
    """Render a viewport preview and return the file path of the saved PNG."""
    scene = bpy.context.scene
    orig_x, orig_y = scene.render.resolution_x, scene.render.resolution_y
    scene.render.resolution_x = resolution_x
    scene.render.resolution_y = resolution_y
    filepath = os.path.join(tempfile.gettempdir(), "blender_mcp_preview.png")
    scene.render.filepath = filepath
    bpy.ops.render.opengl(write_still=True)
    scene.render.resolution_x = orig_x
    scene.render.resolution_y = orig_y
    return filepath


@mcp.tool()
@thread_safe
def clear_scene(keep_cameras: bool = True, keep_lights: bool = True) -> int:
    """
    Remove objects from the scene.

    Returns the number of objects deleted.
    """
    skip_types = set()
    if keep_cameras:
        skip_types.add("CAMERA")
    if keep_lights:
        skip_types.add("LIGHT")
    to_remove = [o for o in bpy.data.objects if o.type not in skip_types]
    for obj in to_remove:
        bpy.data.objects.remove(obj, do_unlink=True)
    return len(to_remove)


@mcp.tool()
@thread_safe
def save_file(filepath: str = "") -> str:
    """
    Save the current .blend file.

    If filepath is empty, saves over the existing file.
    """
    if filepath:
        bpy.ops.wm.save_as_mainfile(filepath=filepath)
        return f"Saved to '{filepath}'"
    bpy.ops.wm.save_mainfile()
    return f"Saved '{bpy.data.filepath}'"


# ── Selection & organisation ───────────────────────────────────────────────

@mcp.tool()
@thread_safe
def get_selected_objects() -> List[str]:
    """Return the names of all currently selected objects."""
    return [obj.name for obj in bpy.context.selected_objects]


@mcp.tool()
@thread_safe
def select_objects(names: List[str], deselect_others: bool = True) -> List[str]:
    """
    Select objects by name.

    deselect_others: if True, clear existing selection first.
    Returns the list of names that were successfully selected.
    """
    if deselect_others:
        bpy.ops.object.select_all(action='DESELECT')
    selected = []
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj:
            obj.select_set(True)
            selected.append(name)
    return selected


@mcp.tool()
@thread_safe
def rename_object(old_name: str, new_name: str) -> str:
    """Rename an object."""
    obj = bpy.data.objects.get(old_name)
    if not obj:
        raise ValueError(f"Object '{old_name}' not found")
    obj.name = new_name
    return f"Renamed '{old_name}' → '{obj.name}'"


@mcp.tool()
@thread_safe
def join_objects(names: List[str]) -> str:
    """
    Join a list of objects into one mesh.

    The first name in the list becomes the active (target) object.
    All objects must be meshes.
    """
    if len(names) < 2:
        raise ValueError("Need at least two object names to join")
    bpy.ops.object.select_all(action='DESELECT')
    for name in names:
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object '{name}' not found")
        obj.select_set(True)
    bpy.context.view_layer.objects.active = bpy.data.objects[names[0]]
    bpy.ops.object.join()
    return f"Joined into '{bpy.context.active_object.name}'"


@mcp.tool()
@thread_safe
def parent_objects(child_name: str, parent_name: str, keep_transform: bool = True) -> str:
    """Set parent_name as the parent of child_name."""
    child = bpy.data.objects.get(child_name)
    parent = bpy.data.objects.get(parent_name)
    if not child:
        raise ValueError(f"Object '{child_name}' not found")
    if not parent:
        raise ValueError(f"Object '{parent_name}' not found")
    if keep_transform:
        child.parent = parent
        child.matrix_parent_inverse = parent.matrix_world.inverted()
    else:
        child.parent = parent
    return f"'{child_name}' is now parented to '{parent_name}'"


@mcp.tool()
@thread_safe
def apply_transforms(
    name: str,
    location: bool = True,
    rotation: bool = True,
    scale: bool = True,
) -> str:
    """Apply location / rotation / scale transforms to an object's mesh data."""
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=location, rotation=rotation, scale=scale)
    return f"Applied transforms on '{name}'"


@mcp.tool()
@thread_safe
def set_origin(name: str, origin_type: str = "ORIGIN_GEOMETRY") -> str:
    """
    Set the origin of an object.

    origin_type: ORIGIN_GEOMETRY | ORIGIN_CURSOR | ORIGIN_CENTER_OF_MASS |
                 ORIGIN_CENTER_OF_VOLUME | GEOMETRY_ORIGIN
    """
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object '{name}' not found")
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.origin_set(type=origin_type.upper())
    return f"Origin of '{name}' set to {origin_type}"


# ── Camera ─────────────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_camera(
    name: str = "Camera",
    location: Tuple[float, float, float] = (0.0, -8.0, 4.0),
    rotation_euler: Tuple[float, float, float] = (1.1, 0.0, 0.0),
) -> Dict[str, Any]:
    """Add a new camera to the scene."""
    cam_data = bpy.data.cameras.new(name=name)
    cam_obj = bpy.data.objects.new(name=name, object_data=cam_data)
    cam_obj.location = location
    cam_obj.rotation_euler = rotation_euler
    bpy.context.collection.objects.link(cam_obj)
    return {"name": cam_obj.name, "location": list(cam_obj.location)}


@mcp.tool()
@thread_safe
def set_active_camera(name: str) -> str:
    """Set which camera is used for rendering."""
    obj = bpy.data.objects.get(name)
    if not obj or obj.type != 'CAMERA':
        raise ValueError(f"Camera '{name}' not found")
    bpy.context.scene.camera = obj
    return f"Active camera set to '{name}'"


@mcp.tool()
@thread_safe
def set_camera_properties(
    name: str,
    focal_length: float = 0.0,
    clip_start: float = 0.0,
    clip_end: float = 0.0,
) -> Dict[str, Any]:
    """
    Update a camera's focal length and/or clip distances.

    Pass 0 for any value you don't want to change.
    """
    obj = bpy.data.objects.get(name)
    if not obj or obj.type != 'CAMERA':
        raise ValueError(f"Camera '{name}' not found")
    cam = obj.data
    if focal_length > 0:
        cam.lens = focal_length
    if clip_start > 0:
        cam.clip_start = clip_start
    if clip_end > 0:
        cam.clip_end = clip_end
    return {"name": name, "focal_length": cam.lens, "clip_start": cam.clip_start, "clip_end": cam.clip_end}


@mcp.tool()
@thread_safe
def point_camera_at(camera_name: str, target_name: str) -> str:
    """
    Add a Track-To constraint so a camera always faces a target object.
    """
    cam_obj = bpy.data.objects.get(camera_name)
    target = bpy.data.objects.get(target_name)
    if not cam_obj or cam_obj.type != 'CAMERA':
        raise ValueError(f"Camera '{camera_name}' not found")
    if not target:
        raise ValueError(f"Target '{target_name}' not found")
    constraint = cam_obj.constraints.new(type='TRACK_TO')
    constraint.target = target
    constraint.track_axis = 'TRACK_NEGATIVE_Z'
    constraint.up_axis = 'UP_Y'
    return f"Camera '{camera_name}' now tracks '{target_name}'"


# ── Materials ──────────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def list_materials() -> List[str]:
    """Return the names of all materials in the blend file."""
    return [mat.name for mat in bpy.data.materials]


@mcp.tool()
@thread_safe
def delete_material(name: str) -> str:
    """Remove a material from the blend file."""
    mat = bpy.data.materials.get(name)
    if not mat:
        raise ValueError(f"Material '{name}' not found")
    bpy.data.materials.remove(mat)
    return f"Deleted material '{name}'"


@mcp.tool()
@thread_safe
def set_material_property(
    material_name: str,
    property_name: str,
    value: float,
) -> str:
    """
    Set a numeric property on a material's Principled BSDF node.

    property_name options: Metallic | Roughness | Specular IOR Level |
                           Transmission Weight | Coat Weight | Emission Strength
    value: 0.0 – 1.0 (or higher for Emission Strength)
    """
    mat = bpy.data.materials.get(material_name)
    if not mat:
        raise ValueError(f"Material '{material_name}' not found")
    if not mat.use_nodes:
        mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if not bsdf:
        raise ValueError(f"Material '{material_name}' has no Principled BSDF node")
    if property_name not in bsdf.inputs:
        raise ValueError(f"Unknown property '{property_name}'")
    bsdf.inputs[property_name].default_value = value
    return f"Set '{property_name}' = {value} on '{material_name}'"


# ── Animation ─────────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def set_frame_range(start: int, end: int) -> Dict[str, int]:
    """Set the scene's animation frame range."""
    bpy.context.scene.frame_start = start
    bpy.context.scene.frame_end = end
    return {"frame_start": start, "frame_end": end}


@mcp.tool()
@thread_safe
def insert_keyframe(
    object_name: str,
    frame: int,
    data_path: str = "location",
) -> str:
    """
    Insert a keyframe for an object property at the given frame.

    data_path examples: location | rotation_euler | scale
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    bpy.context.scene.frame_set(frame)
    obj.keyframe_insert(data_path=data_path, frame=frame)
    return f"Keyframe inserted: '{object_name}'.{data_path} @ frame {frame}"


@mcp.tool()
@thread_safe
def get_keyframes(object_name: str) -> Dict[str, Any]:
    """
    Return all keyframe numbers grouped by data_path for an object.
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    result: Dict[str, list] = {}
    if obj.animation_data and obj.animation_data.action:
        for fcurve in obj.animation_data.action.fcurves:
            frames = sorted({int(kp.co.x) for kp in fcurve.keyframe_points})
            result.setdefault(fcurve.data_path, [])
            for f in frames:
                if f not in result[fcurve.data_path]:
                    result[fcurve.data_path].append(f)
    return result


# ── Rendering ─────────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def set_render_engine(engine: str) -> str:
    """
    Set the render engine.

    engine: BLENDER_EEVEE_NEXT | CYCLES | BLENDER_WORKBENCH
    """
    valid = {"BLENDER_EEVEE_NEXT", "CYCLES", "BLENDER_WORKBENCH", "BLENDER_EEVEE"}
    e = engine.upper()
    if e not in valid:
        raise ValueError(f"engine must be one of {valid}")
    bpy.context.scene.render.engine = e
    return f"Render engine set to '{e}'"


@mcp.tool()
@thread_safe
def set_render_resolution(
    width: int,
    height: int,
    percentage: int = 100,
) -> Dict[str, int]:
    """Set the render resolution and optional percentage scale."""
    bpy.context.scene.render.resolution_x = width
    bpy.context.scene.render.resolution_y = height
    bpy.context.scene.render.resolution_percentage = percentage
    return {"width": width, "height": height, "percentage": percentage}


@mcp.tool()
@thread_safe
def set_render_output(filepath: str, file_format: str = "PNG") -> str:
    """
    Set the render output path and file format.

    file_format: PNG | JPEG | OPEN_EXR | TIFF | BMP
    """
    bpy.context.scene.render.filepath = filepath
    bpy.context.scene.render.image_settings.file_format = file_format.upper()
    return f"Render output: '{filepath}' ({file_format})"


@mcp.tool()
@thread_safe
def full_render(filepath: str = "") -> str:
    """
    Trigger a full render (CPU/GPU, not OpenGL preview) and save to filepath.

    If filepath is empty, uses the scene's existing output path.
    Returns the output file path.
    """
    scene = bpy.context.scene
    if filepath:
        scene.render.filepath = filepath
    bpy.ops.render.render(write_still=True)
    return scene.render.filepath


# ── Scene / World / Collections ───────────────────────────────────────────

@mcp.tool()
@thread_safe
def set_world_color(
    color: Tuple[float, float, float] = (0.05, 0.05, 0.05),
    strength: float = 1.0,
) -> str:
    """Set the world background to a solid color."""
    world = bpy.context.scene.world
    if not world:
        world = bpy.data.worlds.new("World")
        bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if not bg:
        bg = world.node_tree.nodes.new("ShaderNodeBackground")
    bg.inputs["Color"].default_value = (*color, 1.0)
    bg.inputs["Strength"].default_value = strength
    return f"World background set to {color} strength={strength}"


@mcp.tool()
@thread_safe
def list_collections() -> List[str]:
    """Return the names of all collections in the scene."""
    return [col.name for col in bpy.data.collections]


@mcp.tool()
@thread_safe
def create_collection(name: str, link_to_scene: bool = True) -> str:
    """Create a new collection and optionally link it to the active scene."""
    col = bpy.data.collections.new(name=name)
    if link_to_scene:
        bpy.context.scene.collection.children.link(col)
    return f"Created collection '{col.name}'"


@mcp.tool()
@thread_safe
def move_to_collection(object_name: str, collection_name: str) -> str:
    """Move an object to a specific collection (unlinks from all others)."""
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    target = bpy.data.collections.get(collection_name)
    if not target:
        raise ValueError(f"Collection '{collection_name}' not found")
    for col in list(obj.users_collection):
        col.objects.unlink(obj)
    target.objects.link(obj)
    return f"Moved '{object_name}' to collection '{collection_name}'"


# ── Modifiers ─────────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def list_modifiers(object_name: str) -> List[Dict[str, str]]:
    """Return all modifiers on an object as a list of {name, type} dicts."""
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    return [{"name": m.name, "type": m.type} for m in obj.modifiers]


@mcp.tool()
@thread_safe
def set_modifier_property(
    object_name: str,
    modifier_name: str,
    property_name: str,
    value: Any,
) -> str:
    """
    Set a property on a modifier by attribute name.

    Examples:
      object_name="Cube", modifier_name="Subdivision", property_name="levels", value=3
      object_name="Cube", modifier_name="Bevel", property_name="width", value=0.1
    """
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    mod = obj.modifiers.get(modifier_name)
    if not mod:
        raise ValueError(f"Modifier '{modifier_name}' not found on '{object_name}'")
    if not hasattr(mod, property_name):
        raise ValueError(f"Modifier has no property '{property_name}'")
    setattr(mod, property_name, value)
    return f"Set {modifier_name}.{property_name} = {value}"


# ── 3D Cursor ─────────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def get_cursor_location() -> List[float]:
    """Return the current 3D cursor location as [x, y, z]."""
    return list(bpy.context.scene.cursor.location)


@mcp.tool()
@thread_safe
def set_cursor_location(location: Tuple[float, float, float]) -> List[float]:
    """Move the 3D cursor to the given world-space location."""
    bpy.context.scene.cursor.location = location
    return list(bpy.context.scene.cursor.location)


# ── Text objects ───────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def add_text_object(
    text: str,
    name: str = "Text",
    location: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    size: float = 1.0,
    extrude: float = 0.0,
) -> Dict[str, Any]:
    """
    Add a 3D text object to the scene.

    extrude: depth of the 3D extrusion (0 = flat text).
    """
    font_curve = bpy.data.curves.new(name=name, type='FONT')
    font_curve.body = text
    font_curve.size = size
    font_curve.extrude = extrude
    obj = bpy.data.objects.new(name=name, object_data=font_curve)
    obj.location = location
    bpy.context.collection.objects.link(obj)
    return {"name": obj.name, "text": text, "location": list(obj.location)}


# ── Viewport ───────────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def set_viewport_shading(shading_type: str) -> str:
    """
    Set the 3D viewport shading mode.

    shading_type: WIREFRAME | SOLID | MATERIAL | RENDERED
    """
    valid = {"WIREFRAME", "SOLID", "MATERIAL", "RENDERED"}
    s = shading_type.upper()
    if s not in valid:
        raise ValueError(f"shading_type must be one of {valid}")
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.spaces.active.shading.type = s
    return f"Viewport shading set to '{s}'"


# ── Import / Export ────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def import_file(filepath: str) -> str:
    """
    Import a 3D file into the scene. Supported formats:
    .obj, .fbx, .glb / .gltf, .stl, .ply, .abc, .usd / .usdc / .usda, .x3d
    """
    ext = os.path.splitext(filepath)[1].lower()
    importers = {
        ".obj":  lambda: bpy.ops.wm.obj_import(filepath=filepath),
        ".fbx":  lambda: bpy.ops.import_scene.fbx(filepath=filepath),
        ".glb":  lambda: bpy.ops.import_scene.gltf(filepath=filepath),
        ".gltf": lambda: bpy.ops.import_scene.gltf(filepath=filepath),
        ".stl":  lambda: bpy.ops.wm.stl_import(filepath=filepath),
        ".ply":  lambda: bpy.ops.wm.ply_import(filepath=filepath),
        ".abc":  lambda: bpy.ops.wm.alembic_import(filepath=filepath),
        ".usd":  lambda: bpy.ops.wm.usd_import(filepath=filepath),
        ".usdc": lambda: bpy.ops.wm.usd_import(filepath=filepath),
        ".usda": lambda: bpy.ops.wm.usd_import(filepath=filepath),
        ".x3d":  lambda: bpy.ops.import_scene.x3d(filepath=filepath),
    }
    if ext not in importers:
        raise ValueError(f"Unsupported format '{ext}'. Supported: {list(importers)}")
    importers[ext]()
    return f"Imported '{filepath}'"


@mcp.tool()
@thread_safe
def export_file(filepath: str, selected_only: bool = False) -> str:
    """
    Export the scene (or selection) to a file. Format is inferred from extension.

    Supported: .obj, .fbx, .glb / .gltf, .stl, .ply, .abc, .usd / .usdc, .x3d
    """
    ext = os.path.splitext(filepath)[1].lower()
    exporters = {
        ".obj":  lambda: bpy.ops.wm.obj_export(filepath=filepath, export_selected_objects=selected_only),
        ".fbx":  lambda: bpy.ops.export_scene.fbx(filepath=filepath, use_selection=selected_only),
        ".glb":  lambda: bpy.ops.export_scene.gltf(filepath=filepath, export_format='GLB', use_selection=selected_only),
        ".gltf": lambda: bpy.ops.export_scene.gltf(filepath=filepath, export_format='GLTF_SEPARATE', use_selection=selected_only),
        ".stl":  lambda: bpy.ops.wm.stl_export(filepath=filepath, export_selected_objects=selected_only),
        ".ply":  lambda: bpy.ops.wm.ply_export(filepath=filepath, export_selected_objects=selected_only),
        ".abc":  lambda: bpy.ops.wm.alembic_export(filepath=filepath, selected=selected_only),
        ".usd":  lambda: bpy.ops.wm.usd_export(filepath=filepath, selected_objects_only=selected_only),
        ".usdc": lambda: bpy.ops.wm.usd_export(filepath=filepath, selected_objects_only=selected_only),
        ".x3d":  lambda: bpy.ops.export_scene.x3d(filepath=filepath, use_selection=selected_only),
    }
    if ext not in exporters:
        raise ValueError(f"Unsupported format '{ext}'. Supported: {list(exporters)}")
    exporters[ext]()
    return f"Exported to '{filepath}'"


# ── Dynamic discovery ──────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def discover_api(
    query: str,
    category: str = "",
    max_results: int = 10,
    rebuild_cache: bool = False,
) -> List[Dict[str, Any]]:
    """
    Search Blender's live API for operators, types, and data members.

    Returns matching entries with names, docstrings, and call signatures.
    Use this to find the right bpy.ops / bpy.types / bpy.data entry before
    writing execute_safe_python code.

    query:         search term (e.g. "mirror", "bevel", "material", "bpy.ops.mesh")
    category:      filter — "ops" | "types" | "data" | sub-module like "mesh"
    max_results:   number of results to return (default 10, max 50)
    rebuild_cache: set True to force a full index rebuild (takes ~3-5 s)
    """
    import discovery
    if rebuild_cache:
        discovery.build_index(force=True)
    return discovery.search(
        query=query,
        category=category,
        max_results=min(max_results, 50),
    )


# ── RAG doc search ─────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def query_api_docs(
    query: str,
    max_results: int = 3,
) -> List[Dict[str, Any]]:
    """
    Search Blender's embedded API documentation using TF-IDF.

    Returns the top matching API entries with docstrings and usage examples.
    Chain with discover_api: discover the API → query docs for parameter
    details → call execute_safe_python.

    The index is built from live bpy docstrings on first call (cached to disk
    for subsequent calls).

    query:       natural language or code term (e.g. "how to add a bevel")
    max_results: number of doc chunks to return (default 3, max 10)
    """
    import rag_store
    store = rag_store.get_store()
    return store.query(query, top_k=min(max_results, 10))


# ── Safe Python execution ──────────────────────────────────────────────────

# Rate-limiting state: track timestamps of recent execute_safe_python calls
_exec_call_times: _collections.deque = _collections.deque(maxlen=20)
_RATE_WINDOW = 60.0   # seconds
_RATE_MAX    = 10     # max calls per window

# Patterns that trigger a warning (not a hard block — agents are trusted, but informed)
_DANGEROUS_CALLS = frozenset({
    "os.system", "os.popen", "os.exec",
    "subprocess", "shutil.rmtree", "shutil.rmdir",
})


def _ast_warnings(code: str) -> List[str]:
    """Return a list of safety warnings found via static AST analysis."""
    warnings: List[str] = []
    try:
        tree = _ast.parse(code)
    except SyntaxError as exc:
        return [f"SyntaxError: {exc}"]
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            if isinstance(node.func, _ast.Attribute):
                parent = getattr(node.func.value, "id", "")
                full   = f"{parent}.{node.func.attr}"
                for pat in _DANGEROUS_CALLS:
                    if pat in full:
                        warnings.append(f"Potentially dangerous call: {full}")
            elif isinstance(node.func, _ast.Name):
                if node.func.id in {"eval", "compile", "__import__"}:
                    warnings.append(f"Restricted built-in: {node.func.id}()")
    return warnings


def _sanitize_result(value: Any) -> Any:
    """Recursively convert non-JSON-serializable values (bpy objects, etc.) to strings."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _sanitize_result(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_result(v) for v in value]
    return repr(value)


@mcp.tool()
@thread_safe
def execute_safe_python(
    code: str,
    dry_run: bool = False,
    push_undo: bool = True,
) -> Dict[str, Any]:
    """
    Execute Python code in Blender's environment with undo support and safety checks.

    Enhancements over execute_python:
    • Pushes an undo step first so all changes are reversible via Ctrl-Z
    • Validates syntax and flags dangerous patterns before running
    • Captures both stdout and a 'result' variable from your code
    • Sanitizes output so no raw bpy objects leak into the response
    • dry_run=True returns a syntax + safety analysis without executing
    • Rate-limited to 10 calls per 60 seconds

    code:       Python source to execute (has access to bpy, sys, os, json)
    dry_run:    if True, validate only — do not execute
    push_undo:  push an undo step before executing (default True)

    Returns {result, stdout, warnings, elapsed_s} on success,
            {error, warnings, stdout} on failure,
         or {dry_run, syntax, warnings, code} when dry_run=True.
    """
    # ── Rate limit ────────────────────────────────────────────────────────
    now = _time.monotonic()
    while _exec_call_times and now - _exec_call_times[0] > _RATE_WINDOW:
        _exec_call_times.popleft()
    if len(_exec_call_times) >= _RATE_MAX:
        return {
            "error": f"Rate limit: max {_RATE_MAX} calls per {int(_RATE_WINDOW)} s — wait before retrying"
        }
    _exec_call_times.append(now)

    # ── Syntax check + static analysis ───────────────────────────────────
    try:
        _ast.parse(code)
    except SyntaxError as exc:
        return {"error": f"SyntaxError: {exc}", "warnings": []}

    warnings = _ast_warnings(code)

    if dry_run:
        return {
            "dry_run":  True,
            "syntax":   "OK",
            "warnings": warnings,
            "code":     code,
        }

    # ── Undo push ─────────────────────────────────────────────────────────
    if push_undo:
        try:
            bpy.ops.ed.undo_push(message="execute_safe_python")
        except Exception:
            pass  # Some contexts (render, modal ops) don't support undo

    # ── Execute ───────────────────────────────────────────────────────────
    namespace: Dict[str, Any] = {
        "bpy":    bpy,
        "sys":    sys,
        "os":     os,
        "json":   json,
        "result": None,
    }
    buf = _io.StringIO()
    t0  = _time.perf_counter()
    try:
        with _contextlib.redirect_stdout(buf):
            exec(code, namespace)  # noqa: S102
    except Exception as exc:
        return {
            "error":     f"{type(exc).__name__}: {exc}",
            "warnings":  warnings,
            "stdout":    buf.getvalue(),
            "elapsed_s": round(_time.perf_counter() - t0, 3),
        }

    return {
        "result":    _sanitize_result(namespace.get("result")),
        "stdout":    buf.getvalue(),
        "warnings":  warnings,
        "elapsed_s": round(_time.perf_counter() - t0, 3),
    }


# ── UV / Texture / Export / Bake / Mesh-edit ──────────────────────────────


@mcp.tool()
@thread_safe
def unwrap_uv(
    object_name: str,
    method: str = "smart_project",
    angle_limit: float = 66.0,
    island_margin: float = 0.02,
) -> Dict[str, Any]:
    """
    UV unwrap a mesh object and return the method used.

    object_name: name of the mesh object to unwrap
    method: smart_project (default) | unwrap | cube_project
    angle_limit: angle limit in degrees for smart_project (default 66.0)
    island_margin: margin between UV islands, 0.0–1.0 (default 0.02)

    Enters Edit Mode, selects all geometry, runs the chosen UV operator,
    then returns to Object Mode.  UV islands outside 0–1 space break
    Roblox textures, so smart_project is recommended for hard-surface props.
    """
    import math as _math

    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"Object '{object_name}' is not a mesh (type: {obj.type})")

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')

    method_lower = method.lower()
    try:
        if method_lower == "smart_project":
            bpy.ops.uv.smart_project(
                angle_limit=_math.radians(angle_limit),
                island_margin=island_margin,
            )
        elif method_lower == "unwrap":
            bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=island_margin)
        elif method_lower == "cube_project":
            bpy.ops.uv.cube_project(cube_size=1.0)
        else:
            raise ValueError(
                f"Unknown method '{method}'. Choose: smart_project, unwrap, cube_project"
            )
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    return {
        "object": object_name,
        "method": method_lower,
        "angle_limit_deg": angle_limit,
        "island_margin": island_margin,
    }


@mcp.tool()
@thread_safe
def set_material_texture(
    material_name: str,
    texture_path: str,
    channel: str = "BASE_COLOR",
) -> Dict[str, Any]:
    """
    Load an image file and wire it into an existing material's Principled BSDF.

    material_name: name of an existing Blender material
    texture_path: absolute path to the image file (PNG, JPEG, EXR, etc.)
    channel: BASE_COLOR | NORMAL | ROUGHNESS | METALLIC | EMISSION

    For NORMAL, a Normal Map node is automatically inserted between the
    texture and the BSDF.  Color-space is set to Non-Color for data channels
    (NORMAL, ROUGHNESS, METALLIC) and sRGB for color channels.
    Returns the material name and wired channel.
    """
    mat = bpy.data.materials.get(material_name)
    if not mat:
        raise ValueError(f"Material '{material_name}' not found")
    if not os.path.exists(texture_path):
        raise ValueError(f"Texture file not found: '{texture_path}'")

    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    bsdf = nodes.get("Principled BSDF")
    if not bsdf:
        raise ValueError(f"Material '{material_name}' has no Principled BSDF node")

    img = bpy.data.images.load(texture_path, check_existing=True)

    tex_node = nodes.new("ShaderNodeTexImage")
    tex_node.image = img

    channel_upper = channel.upper()
    if channel_upper == "BASE_COLOR":
        img.colorspace_settings.name = 'sRGB'
        links.new(tex_node.outputs["Color"], bsdf.inputs["Base Color"])
    elif channel_upper == "ROUGHNESS":
        img.colorspace_settings.name = 'Non-Color'
        links.new(tex_node.outputs["Color"], bsdf.inputs["Roughness"])
    elif channel_upper == "METALLIC":
        img.colorspace_settings.name = 'Non-Color'
        links.new(tex_node.outputs["Color"], bsdf.inputs["Metallic"])
    elif channel_upper == "EMISSION":
        img.colorspace_settings.name = 'sRGB'
        # Blender 4.x uses "Emission Color"; fall back to "Emission" for older builds
        emission_input = bsdf.inputs.get("Emission Color") or bsdf.inputs.get("Emission")
        if not emission_input:
            nodes.remove(tex_node)
            raise ValueError("Principled BSDF has no Emission input on this Blender build")
        links.new(tex_node.outputs["Color"], emission_input)
    elif channel_upper == "NORMAL":
        img.colorspace_settings.name = 'Non-Color'
        normal_map_node = nodes.new("ShaderNodeNormalMap")
        links.new(tex_node.outputs["Color"], normal_map_node.inputs["Color"])
        links.new(normal_map_node.outputs["Normal"], bsdf.inputs["Normal"])
    else:
        nodes.remove(tex_node)
        raise ValueError(
            f"Unknown channel '{channel}'. "
            "Choose: BASE_COLOR, NORMAL, ROUGHNESS, METALLIC, EMISSION"
        )

    return {
        "material": material_name,
        "channel": channel_upper,
        "texture": os.path.basename(texture_path),
    }


@mcp.tool()
@thread_safe
def batch_export(
    collection_name: str,
    output_dir: str,
    format: str = "FBX",
    apply_modifiers: bool = True,
    use_object_name: bool = True,
) -> Dict[str, Any]:
    """
    Export every mesh object in a collection as a separate file.

    collection_name: name of the source Blender collection
    output_dir: directory path where files will be written (created if absent)
    format: FBX (default) | GLB
    apply_modifiers: apply all modifiers before export (default True)
    use_object_name: use the object's Blender name as the output filename (default True)

    Applies transforms (location/rotation/scale) on each object before export.
    Returns a dict with the list of exported paths, a count, and any per-object errors.
    """
    col = bpy.data.collections.get(collection_name)
    if not col:
        raise ValueError(f"Collection '{collection_name}' not found")

    os.makedirs(output_dir, exist_ok=True)

    fmt_upper = format.upper()
    if fmt_upper not in ("FBX", "GLB"):
        raise ValueError(f"Unsupported format '{format}'. Choose: FBX, GLB")

    ext = ".fbx" if fmt_upper == "FBX" else ".glb"
    mesh_objects = [o for o in col.objects if o.type == 'MESH']
    if not mesh_objects:
        return {"exported": [], "count": 0, "errors": [], "message": "No mesh objects in collection"}

    exported_paths: List[str] = []
    errors: List[Dict[str, str]] = []

    for obj in mesh_objects:
        try:
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj

            bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

            safe_name = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in obj.name)
            out_path = os.path.join(output_dir, safe_name + ext)

            if fmt_upper == "FBX":
                bpy.ops.export_scene.fbx(
                    filepath=out_path,
                    use_selection=True,
                    apply_unit_scale=True,
                    apply_scale_options='FBX_SCALE_ALL',
                    use_mesh_modifiers=apply_modifiers,
                    mesh_smooth_type='FACE',
                    add_leaf_bones=False,
                    path_mode='COPY',
                )
            else:
                bpy.ops.export_scene.gltf(
                    filepath=out_path,
                    use_selection=True,
                    export_apply=apply_modifiers,
                    export_format='GLB',
                )

            exported_paths.append(out_path)
        except Exception as exc:
            errors.append({"object": obj.name, "error": str(exc)})

    return {
        "exported": exported_paths,
        "count": len(exported_paths),
        "errors": errors,
        "output_dir": output_dir,
    }


@mcp.tool()
@thread_safe
def bake_texture(
    high_poly: str,
    low_poly: str,
    bake_type: str = "NORMAL",
    resolution: int = 1024,
    output_path: str = "",
    margin: int = 16,
) -> Dict[str, Any]:
    """
    Bake from a high-poly object onto a low-poly object using Cycles.

    high_poly: name of the high-resolution source object
    low_poly: name of the low-resolution target object
    bake_type: NORMAL | AO | DIFFUSE | ROUGHNESS (default NORMAL)
    resolution: output image size in pixels, square (default 1024)
    output_path: absolute path to save the PNG; auto-generated in temp dir if empty
    margin: pixel margin around UV islands (default 16)

    Temporarily switches the render engine to CYCLES for the bake, restores
    it afterward.  Creates a new image datablock, adds an unconnected
    ShaderNodeTexImage to the low-poly material as the bake target, bakes,
    and saves as PNG.
    Returns the output path and bake parameters.
    """
    hp_obj = bpy.data.objects.get(high_poly)
    if not hp_obj:
        raise ValueError(f"High-poly object '{high_poly}' not found")
    lp_obj = bpy.data.objects.get(low_poly)
    if not lp_obj:
        raise ValueError(f"Low-poly object '{low_poly}' not found")

    bake_type_upper = bake_type.upper()
    valid_types = ("NORMAL", "AO", "DIFFUSE", "ROUGHNESS")
    if bake_type_upper not in valid_types:
        raise ValueError(f"Unknown bake_type '{bake_type}'. Choose: {', '.join(valid_types)}")

    prev_engine = bpy.context.scene.render.engine
    bpy.context.scene.render.engine = 'CYCLES'

    try:
        img_name = f"Bake_{low_poly}_{bake_type_upper}"
        if img_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[img_name])
        bake_img = bpy.data.images.new(img_name, width=resolution, height=resolution, alpha=False)

        # Ensure low-poly has a material with nodes
        if not lp_obj.data.materials:
            mat = bpy.data.materials.new(name=f"{low_poly}_BakeMat")
            mat.use_nodes = True
            lp_obj.data.materials.append(mat)
        else:
            mat = lp_obj.data.materials[0]
            mat.use_nodes = True

        nodes = mat.node_tree.nodes
        # Remove any previous bake target node
        for n in [n for n in nodes if n.name == "__bake_target__"]:
            nodes.remove(n)

        # Add unconnected texture node as the bake target
        tex_node = nodes.new("ShaderNodeTexImage")
        tex_node.name = "__bake_target__"
        tex_node.image = bake_img
        nodes.active = tex_node

        # Select high-poly, then set low-poly as active
        bpy.ops.object.select_all(action='DESELECT')
        hp_obj.select_set(True)
        lp_obj.select_set(True)
        bpy.context.view_layer.objects.active = lp_obj

        bpy.context.scene.render.bake.use_selected_to_active = True
        bpy.context.scene.render.bake.margin = margin

        bpy.ops.object.bake(type=bake_type_upper)

        if not output_path:
            output_path = os.path.join(
                tempfile.gettempdir(),
                f"{img_name}_{resolution}px.png",
            )

        bake_img.filepath_raw = output_path
        bake_img.file_format = 'PNG'
        bake_img.save()

        return {
            "output_path": output_path,
            "bake_type": bake_type_upper,
            "resolution": resolution,
            "high_poly": high_poly,
            "low_poly": low_poly,
            "image_name": img_name,
        }
    finally:
        bpy.context.scene.render.engine = prev_engine


@mcp.tool()
@thread_safe
def edit_mesh(
    object_name: str,
    operation: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Perform a basic mesh editing operation on a mesh object.

    object_name: name of the target mesh object
    operation: one of loop_cut | inset | extrude_region | merge_by_distance | set_smooth_shading
    params: optional dict of operation-specific parameters (see below)

    loop_cut params:
      cuts (int, default 1)          — number of loop cuts to insert
      edge_percent (float, default 0.0) — slide position along edge, -1.0 to 1.0

    inset params:
      thickness (float, default 0.1)   — inset amount
      depth (float, default 0.0)       — depth offset
      use_boundary (bool, default True) — inset boundary edges

    extrude_region params:
      direction (str 'X'|'Y'|'Z', default 'Z') — extrusion axis
      amount (float, default 1.0)               — extrusion distance

    merge_by_distance params:
      threshold (float, default 0.0001) — merge distance

    set_smooth_shading params:
      smooth (bool, default True)               — True=smooth, False=flat
      auto_smooth_angle (float, default 30.0)   — angle threshold in degrees

    Returns the object name, operation, and effective params used.
    """
    import bmesh as _bmesh
    import math as _math

    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object '{object_name}' not found")
    if obj.type != 'MESH':
        raise ValueError(f"Object '{object_name}' is not a mesh (type: {obj.type})")

    if params is None:
        params = {}

    op_lower = operation.lower()
    valid_ops = ("loop_cut", "inset", "extrude_region", "merge_by_distance", "set_smooth_shading")
    if op_lower not in valid_ops:
        raise ValueError(f"Unknown operation '{operation}'. Choose: {', '.join(valid_ops)}")

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    if op_lower == "set_smooth_shading":
        smooth = bool(params.get("smooth", True))
        auto_angle = float(params.get("auto_smooth_angle", 30.0))
        if smooth:
            bpy.ops.object.shade_smooth()
        else:
            bpy.ops.object.shade_flat()
        # Blender 4.1+ removed use_auto_smooth; handle both APIs gracefully
        try:
            obj.data.use_auto_smooth = smooth
            obj.data.auto_smooth_angle = _math.radians(auto_angle)
        except AttributeError:
            pass  # Blender 4.1+ uses Smooth by Angle modifier instead
        return {
            "object": object_name,
            "operation": op_lower,
            "smooth": smooth,
            "auto_smooth_angle_deg": auto_angle,
        }

    bpy.ops.object.mode_set(mode='EDIT')
    try:
        bm = _bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        # Select all geometry
        for v in bm.verts:
            v.select = True
        for e in bm.edges:
            e.select = True
        for f in bm.faces:
            f.select = True
        bm.select_flush_mode()

        if op_lower == "loop_cut":
            cuts = int(params.get("cuts", 1))
            edge_percent = float(params.get("edge_percent", 0.0))
            # Find the longest edge to determine the loop-cut axis
            longest = max(bm.edges, key=lambda e: e.calc_length())
            _bmesh.ops.subdivide_edges(
                bm,
                edges=[longest] + [e for e in bm.edges if e != longest and
                                    any(v in longest.verts for v in e.verts)],
                cuts=cuts,
                use_grid_fill=True,
            )
            _bmesh.update_edit_mesh(obj.data)
            effective_params = {"cuts": cuts, "edge_percent": edge_percent}

        elif op_lower == "inset":
            thickness = float(params.get("thickness", 0.1))
            depth = float(params.get("depth", 0.0))
            use_boundary = bool(params.get("use_boundary", True))
            _bmesh.ops.inset_region(
                bm,
                faces=bm.faces,
                thickness=thickness,
                depth=depth,
                use_boundary=use_boundary,
            )
            _bmesh.update_edit_mesh(obj.data)
            effective_params = {"thickness": thickness, "depth": depth, "use_boundary": use_boundary}

        elif op_lower == "extrude_region":
            direction = params.get("direction", "Z").upper()
            amount = float(params.get("amount", 1.0))
            vec_map = {"X": (amount, 0.0, 0.0), "Y": (0.0, amount, 0.0), "Z": (0.0, 0.0, amount)}
            if direction not in vec_map:
                raise ValueError(f"direction must be X, Y, or Z; got '{direction}'")
            import mathutils as _mu
            result = _bmesh.ops.extrude_face_region(bm, geom=bm.faces[:])
            new_verts = [e for e in result["geom"] if isinstance(e, _bmesh.types.BMVert)]
            _bmesh.ops.translate(bm, vec=_mu.Vector(vec_map[direction]), verts=new_verts)
            _bmesh.update_edit_mesh(obj.data)
            effective_params = {"direction": direction, "amount": amount}

        elif op_lower == "merge_by_distance":
            threshold = float(params.get("threshold", 0.0001))
            _bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=threshold)
            _bmesh.update_edit_mesh(obj.data)
            effective_params = {"threshold": threshold}

    finally:
        bpy.ops.object.mode_set(mode='OBJECT')

    return {"object": object_name, "operation": op_lower, "params": effective_params}


# ── Register additional tool modules (side-effect registration) ────────────
# Each import causes @mcp.tool() decorators to run, registering tools on the
# shared `mcp` instance from core.py.
import rack_tools    # noqa: F401, E402  — 14 rack cabinet tools
import mesh_tools    # noqa: F401, E402  — 11 hard-surface mesh tools
import gn_tools      # noqa: F401, E402  — 6 Geometry Nodes management tools
import export_tools  # noqa: F401, E402  — 6 UE5 export pipeline tools
