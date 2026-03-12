"""
FastMCP server with thread-safe Blender API tools.

All tool functions are dispatched to Blender's main thread via bpy.app.timers
so they are safe to call from uvicorn's worker threads.
"""

import bpy
import sys
import os
import json
import tempfile
import threading
import functools
from typing import List, Dict, Any, Tuple

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

mcp = FastMCP("blender-universal")

_LOG_PREFIX = "[BlenderMCP]"
_MAX_LOG_LEN = 400  # truncate long payloads in terminal output


def _log(msg: str) -> None:
    import time as _time
    ts = _time.strftime("%H:%M:%S")
    print(f"{_LOG_PREFIX} {ts} {msg}", flush=True)


def _parse_sse(raw: bytes) -> list:
    """Extract JSON objects from an SSE-formatted byte string (data: ... lines)."""
    results = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                results.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return results


def _fmt_json(obj, max_len: int = _MAX_LOG_LEN) -> str:
    s = json.dumps(obj)
    return s if len(s) <= max_len else s[:max_len] + " …"


def _log_rpc(direction: str, data: dict) -> None:
    """Log a single parsed JSON-RPC object."""
    if direction == ">>":
        method = data.get("method", "?")
        params = data.get("params") or {}
        req_id = data.get("id", "–")
        if method == "tools/call":
            tool_name = params.get("name", "?")
            args = params.get("arguments", {})
            _log(f"MCP >> [{req_id}] tools/call  tool={tool_name}  args={_fmt_json(args)}")
        elif method == "tools/list":
            _log(f"MCP >> [{req_id}] tools/list")
        elif method == "initialize":
            client = (params.get("clientInfo") or {})
            _log(f"MCP >> [{req_id}] initialize  client={client.get('name','?')} {client.get('version','')}")
        elif method and method.startswith("notifications/"):
            reason = params.get("reason", "")
            cancelled_id = params.get("requestId", "")
            _log(f"MCP >> NOTIFY  {method}  requestId={cancelled_id}  reason={reason}")
        else:
            _log(f"MCP >> [{req_id}] {method}  {_fmt_json(params)}")
    else:
        req_id = data.get("id", "–")
        if "error" in data:
            _log(f"MCP << [{req_id}] ERROR  {_fmt_json(data['error'])}")
        elif "result" in data:
            result = data["result"]
            # Summarise tools/list specially to avoid flooding
            if isinstance(result, dict) and "tools" in result:
                names = [t.get("name", "?") for t in result["tools"]]
                _log(f"MCP << [{req_id}] tools/list  ({len(names)} tools): {', '.join(names)}")
            else:
                _log(f"MCP << [{req_id}] result  {_fmt_json(result)}")
        else:
            _log(f"MCP << [{req_id}] {_fmt_json(data)}")


def _log_body(direction: str, raw: bytes) -> None:
    """Parse and pretty-print an MCP request/response body (JSON or SSE)."""
    if not raw or not raw.strip():
        return

    # Try plain JSON first
    try:
        data = json.loads(raw)
        _log_rpc(direction, data)
        return
    except json.JSONDecodeError:
        pass

    # Try SSE (event: message / data: {...} format)
    sse_objects = _parse_sse(raw)
    if sse_objects:
        for obj in sse_objects:
            _log_rpc(direction, obj)
        return

    # Fallback: show truncated raw text
    text = raw.decode("utf-8", errors="replace").strip()
    _log(f"MCP {direction} (unparsed): {text[:_MAX_LOG_LEN]}")


class _StripOutputSchemaMiddleware:
    """
    ASGI middleware that removes 'outputSchema' from tools/list SSE responses.

    MCP spec 2025-06-18 added outputSchema, but many clients (LM Studio, etc.)
    don't implement it and immediately cancel the session when they see it.
    Stripping it makes the tools/list response look like 2024-11-05 format,
    which every client understands.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def _send(msg):
            if msg.get("type") == "http.response.body":
                msg = dict(msg)
                msg["body"] = self._strip(msg.get("body", b""))
            await send(msg)

        await self.app(scope, receive, _send)

    @staticmethod
    def _strip(chunk: bytes) -> bytes:
        """Remove outputSchema from every SSE data frame in a chunk."""
        if b"outputSchema" not in chunk:
            return chunk
        lines = []
        for line in chunk.decode("utf-8", errors="replace").splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith("data:"):
                payload = stripped[5:].strip()
                try:
                    data = json.loads(payload)
                    result = (data.get("result") or {})
                    tools = result.get("tools")
                    if isinstance(tools, list):
                        for tool in tools:
                            tool.pop("outputSchema", None)
                        line = f"data: {json.dumps(data)}\n"
                except (json.JSONDecodeError, AttributeError):
                    pass
            lines.append(line)
        return "".join(lines).encode("utf-8")


class _SSEToJSONMiddleware:
    """
    ASGI middleware that converts single-message SSE responses to plain JSON.

    FastMCP always returns responses as SSE (event: message / data: {...}) even
    for simple request/response pairs.  Many MCP clients (LM Studio, etc.) send
    the POST and expect a plain application/json body back — they can't parse the
    SSE envelope, abort the connection, and never progress to tools/call.

    If the response contains exactly one SSE data frame we unwrap it to JSON.
    Multi-frame SSE streams are passed through unchanged.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start_msg: dict = {}
        body_chunks: list[bytes] = []

        async def _send(msg):
            if msg["type"] == "http.response.start":
                # Hold the start message until we know the full body
                start_msg.update(msg)
            elif msg["type"] == "http.response.body":
                body_chunks.append(msg.get("body", b""))
                if not msg.get("more_body", False):
                    await self._flush(start_msg, body_chunks, send)

        await self.app(scope, receive, _send)

    @staticmethod
    async def _flush(start_msg: dict, body_chunks: list[bytes], send) -> None:
        full_body = b"".join(body_chunks)
        sse_objects = _parse_sse(full_body)

        if len(sse_objects) == 1:
            # Single SSE frame → unwrap to plain JSON
            json_body = json.dumps(sse_objects[0]).encode("utf-8")
            headers = [
                (k, v) for k, v in start_msg.get("headers", [])
                if k.lower() not in (b"content-type", b"content-length", b"transfer-encoding")
            ]
            headers.extend([
                (b"content-type", b"application/json"),
                (b"content-length", str(len(json_body)).encode()),
            ])
            await send({"type": "http.response.start", "status": start_msg["status"], "headers": headers})
            await send({"type": "http.response.body", "body": json_body, "more_body": False})
        else:
            # Multi-frame or non-SSE → pass through unchanged
            await send(start_msg)
            await send({"type": "http.response.body", "body": full_body, "more_body": False})


class _MCPDebugMiddleware:
    """
    ASGI middleware that logs MCP JSON-RPC request/response bodies in real time.

    Requests are logged when fully received.
    Responses are logged per-chunk so SSE events show their actual send timestamp,
    not the timestamp when the connection eventually closes.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # ── buffer request body (requests arrive in one shot) ────────────
        req_chunks: list[bytes] = []

        async def _receive():
            msg = await receive()
            if msg.get("type") == "http.request":
                req_chunks.append(msg.get("body", b""))
                if not msg.get("more_body", False):
                    _log_body(">>", b"".join(req_chunks))
            return msg

        # ── log each response chunk immediately ──────────────────────────
        # For SSE connections each chunk is one event; logging per-chunk
        # gives accurate timestamps for when data actually leaves the server.
        async def _send(msg):
            if msg.get("type") == "http.response.body":
                chunk = msg.get("body", b"")
                if chunk and chunk.strip():
                    _log_body("<<", chunk)
            await send(msg)

        await self.app(scope, _receive, _send)


# ── Thread safety ──────────────────────────────────────────────────────────

def thread_safe(func):
    """Run a function on Blender's main thread and return its result."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        arg_summary = ", ".join(
            [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
        )
        _log(f"TOOL >> {func.__name__}({arg_summary})")

        if threading.current_thread() is threading.main_thread():
            try:
                result = func(*args, **kwargs)
                _log(f"TOOL << {func.__name__} OK: {result!r}")
                return result
            except Exception as exc:
                _log(f"TOOL << {func.__name__} ERROR: {exc}")
                raise

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
            _log(f"TOOL << {func.__name__} TIMEOUT")
            raise TimeoutError(f"Blender main thread timeout in {func.__name__}")

        if error[0] is not None:
            _log(f"TOOL << {func.__name__} ERROR: {error[0]}")
            raise error[0]

        _log(f"TOOL << {func.__name__} OK: {result[0]!r}")
        return result[0]
    return wrapper


def get_app():
    """
    Return the FastMCP ASGI application with compatibility fixes and debug logging.

    Stack (outermost → innermost):
      _MCPDebugMiddleware          – real-time request/response logging
      _StripOutputSchemaMiddleware – removes outputSchema from tools/list
      mcp.sse_app()                – SSE transport (GET /sse + POST /messages/)
    """
    return _MCPDebugMiddleware(_StripOutputSchemaMiddleware(mcp.sse_app()))


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
