import bpy
import mathutils
import threading
from typing import List, Dict, Any, Optional, Tuple
from mcp.server.fastmcp import tool  # MCP decorator

# Thread-safe decorator (Blender API must run on main thread)
def thread_safe(func):
    def wrapper(*args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            return func(*args, **kwargs)
        else:
            # Queue to main thread if called from server thread
            result = [None]
            error = [None]

            def run():
                try:
                    result[0] = func(*args, **kwargs)
                except Exception as e:
                    error[0] = e

            bpy.app.timers.register(run, first_interval=0.01)
            while result[0] is None and error[0] is None:
                pass  # spin until done (simple for now)

            if error[0]:
                raise error[0]
            return result[0]

    return wrapper

# ────────────────────────────────────────────────
# CORE TOOLS (expandable)
# ────────────────────────────────────────────────

@tool()
@thread_safe
def list_objects() -> List[str]:
    """Return a list of all object names in the current scene."""
    return [obj.name for obj in bpy.data.objects]


@tool()
@thread_safe
def create_cube(name: str = "Cube", location: Tuple[float, float, float] = (0, 0, 0)) -> Dict[str, Any]:
    """Create a new cube at the specified location."""
    bpy.ops.mesh.primitive_cube_add(size=2, location=location)
    obj = bpy.context.active_object
    obj.name = name
    return {"name": obj.name, "location": obj.location}


@tool()
@thread_safe
def delete_object(name: str) -> str:
    """Delete an object by name."""
    obj = bpy.data.objects.get(name)
    if obj:
        bpy.data.objects.remove(obj, do_unlink=True)
        return f"Deleted {name}"
    return f"Object {name} not found"


@tool()
@thread_safe
def move_object(name: str, location: Tuple[float, float, float]) -> Dict[str, Any]:
    """Move an object to a new location."""
    obj = bpy.data.objects.get(name)
    if obj:
        obj.location = location
        return {"name": name, "new_location": obj.location}
    raise ValueError(f"Object {name} not found")


@tool()
@thread_safe
def scale_object(name: str, scale: Tuple[float, float, float] = (1, 1, 1)) -> Dict[str, Any]:
    """Scale an object uniformly or per axis."""
    obj = bpy.data.objects.get(name)
    if obj:
        obj.scale = scale
        return {"name": name, "new_scale": obj.scale}
    raise ValueError(f"Object {name} not found")


@tool()
@thread_safe
def get_object_info(name: str) -> Dict[str, Any]:
    """Get detailed info about an object: location, rotation, scale, type, dimensions."""
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object {name} not found")
    return {
        "name": obj.name,
        "type": obj.type,
        "location": tuple(obj.location),
        "rotation_euler": tuple(obj.rotation_euler),
        "scale": tuple(obj.scale),
        "dimensions": tuple(obj.dimensions),
    }


@tool()
@thread_safe
def add_material_to_object(object_name: str, material_name: str = "Material") -> str:
    """Create a simple material and assign it to an object."""
    obj = bpy.data.objects.get(object_name)
    if not obj:
        raise ValueError(f"Object {object_name} not found")

    mat = bpy.data.materials.get(material_name)
    if not mat:
        mat = bpy.data.materials.new(name=material_name)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes["Principled BSDF"]
        bsdf.inputs["Base Color"].default_value = (0.8, 0.2, 0.2, 1)  # red-ish

    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)

    return f"Assigned material '{material_name}' to '{object_name}'"


@tool()
@thread_safe
def set_scene_frame(frame: int) -> int:
    """Set the current animation frame."""
    bpy.context.scene.frame_set(frame)
    return bpy.context.scene.frame_current


@tool()
@thread_safe
def render_preview(resolution_x: int = 512, resolution_y: int = 512) -> str:
    """Quick viewport render and return path to saved image."""
    scene = bpy.context.scene
    original_res = (scene.render.resolution_x, scene.render.resolution_y)
    scene.render.resolution_x = resolution_x
    scene.render.resolution_y = resolution_y

    filepath = "/tmp/blender_mcp_preview.png"
    scene.render.filepath = filepath
    bpy.ops.render.opengl(write_still=True)

    scene.render.resolution_x, scene.render.resolution_y = original_res
    return filepath


@tool()
@thread_safe
def clear_scene(keep_cameras: bool = True, keep_lights: bool = True) -> int:
    """Clear all objects in the scene (optional keep cameras/lights)."""
    count = 0
    for obj in list(bpy.data.objects):
        if (keep_cameras and obj.type == 'CAMERA') or (keep_lights and obj.type == 'LIGHT'):
            continue
        bpy.data.objects.remove(obj)
        count += 1
    return count
