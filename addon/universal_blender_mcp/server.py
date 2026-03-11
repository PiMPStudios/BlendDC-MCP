"""
FastMCP server with thread-safe Blender API tools.

All tool functions are dispatched to Blender's main thread via bpy.app.timers
so they are safe to call from uvicorn's worker threads.
"""

import bpy
import sys
import os
import tempfile
import threading
import functools
from typing import List, Dict, Any, Tuple

from fastmcp import FastMCP

mcp = FastMCP("blender-universal")


# ── Thread safety ──────────────────────────────────────────────────────────

def thread_safe(func):
    """Run a function on Blender's main thread and return its result."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            return func(*args, **kwargs)

        result = [None]
        error = [None]
        done = threading.Event()

        def _run():
            try:
                result[0] = func(*args, **kwargs)
            except Exception as exc:
                error[0] = exc
            finally:
                done.set()

        bpy.app.timers.register(_run, first_interval=0.0)

        if not done.wait(timeout=10.0):
            raise TimeoutError(f"Blender main thread timeout in {func.__name__}")

        if error[0] is not None:
            raise error[0]

        return result[0]
    return wrapper


def get_app():
    """Return the FastMCP ASGI application."""
    return mcp.http_app(stateless_http=True)


# ── Tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
@thread_safe
def list_objects() -> List[str]:
    """Return the names of all objects in the current scene."""
    return [obj.name for obj in bpy.data.objects]


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


@mcp.tool()
@thread_safe
def execute_python(code: str) -> str:
    """
    Execute arbitrary Python code in Blender's environment and return stdout.

    Use with care — this has full access to the Blender API.
    """
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, {"bpy": bpy, "sys": sys})  # noqa: S102
    return buf.getvalue() or "OK"
