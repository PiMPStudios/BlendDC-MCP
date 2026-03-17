bl_info = {
    "name": "Universal Blender MCP",
    "author": "Da Hoodie Guy",
    "version": (1, 4, 0),
    "blender": (4, 0, 0),
    "location": "View3D > N-Panel > MCP",
    "description": "MCP server for Blender — works with Claude, Cursor, Continue, LM Studio, Open WebUI",
    "category": "Development",
}

import bpy
import sys
import os
import time
import threading
import subprocess
from pathlib import Path

# Ensure terminal output is not buffered when Blender is launched from a terminal
os.environ.setdefault("PYTHONUNBUFFERED", "1")

PORT = 8400

_server_thread = None
_uvicorn_server = None
_server_running = False


# ── Dependency management ──────────────────────────────────────────────────

def _pip_install(*packages: str) -> None:
    """Install packages into Blender's Python using pip."""
    subprocess.check_call(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        stderr=subprocess.DEVNULL,
    )
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade"] + list(packages)
    )


def _ensure_dependencies() -> bool:
    """Return True if all required packages are available (installing if needed)."""
    missing = []
    for pkg in ("mcp", "uvicorn"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return True

    print(f"[BlenderMCP] Installing: {missing}")
    try:
        _pip_install("mcp[cli]", "uvicorn[standard]")
        print("[BlenderMCP] Dependencies installed.")
        return True
    except Exception as exc:
        print(f"[BlenderMCP] ERROR installing dependencies: {exc}")
        return False


# ── Server management ──────────────────────────────────────────────────────

def _get_server_app():
    """Import the bundled server module, pre-warm schema cache, and return the ASGI app."""
    addon_dir = str(Path(__file__).parent)
    if addon_dir not in sys.path:
        sys.path.insert(0, addon_dir)

    import importlib

    # Reload helper modules first so server.py picks up fresh code
    for _mod_name in ("discovery", "rag_store"):
        if _mod_name in sys.modules:
            importlib.reload(sys.modules[_mod_name])

    import server as _srv
    importlib.reload(_srv)
    app = _srv.get_app()

    # Pre-warm Pydantic schema generation for all tools synchronously.
    # Without this, the first tools/list call from the client takes 15+ seconds
    # (cold Pydantic schema generation for 54 tools), which exceeds LM Studio's
    # timeout and causes it to cancel the request and retry forever.
    print("[BlenderMCP] Pre-warming tool schema cache...", flush=True)
    try:
        import asyncio
        import time as _time
        t0 = _time.perf_counter()
        # Call the exact async path FastMCP uses for a real tools/list request.
        # This forces Pydantic to build and cache every schema up front so the
        # first client request returns in milliseconds, not 15+ seconds.
        tools = asyncio.run(_srv.mcp.list_tools())
        elapsed = _time.perf_counter() - t0
        print(f"[BlenderMCP] Schema cache ready — {len(tools)} tools in {elapsed:.2f}s", flush=True)
    except Exception as exc:
        print(f"[BlenderMCP] Schema pre-warm failed (non-fatal): {exc}", flush=True)

    return app


# ── Operators ─────────────────────────────────────────────────────────────

class MCP_OT_start_server(bpy.types.Operator):
    bl_idname = "mcp.start_server"
    bl_label = "Start MCP Server"
    bl_description = "Start the MCP HTTP server so LLM frontends can connect"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _server_running, _server_thread, _uvicorn_server

        if _server_running:
            self.report({'WARNING'}, "Server already running")
            return {'CANCELLED'}

        if not _ensure_dependencies():
            self.report({'ERROR'}, "Failed to install dependencies — check System Console")
            return {'CANCELLED'}

        try:
            import uvicorn
            app = _get_server_app()

            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=PORT,
                log_level="info",
                loop="asyncio",
            )
            _uvicorn_server = uvicorn.Server(config)

            def _run():
                _uvicorn_server.run()

            _server_thread = threading.Thread(target=_run, daemon=True)
            _server_thread.start()

            # Brief wait so the port is bound before we report success
            time.sleep(0.8)

            _server_running = True
            print(f"[BlenderMCP] Server running — http://127.0.0.1:{PORT}/mcp", flush=True)
            self.report({'INFO'}, f"MCP Server running at http://127.0.0.1:{PORT}/mcp")
            return {'FINISHED'}

        except Exception as exc:
            self.report({'ERROR'}, f"Failed to start server: {exc}")
            return {'CANCELLED'}


class MCP_OT_stop_server(bpy.types.Operator):
    bl_idname = "mcp.stop_server"
    bl_label = "Stop MCP Server"
    bl_description = "Stop the running MCP server"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _server_running, _uvicorn_server, _server_thread

        if _uvicorn_server:
            _uvicorn_server.should_exit = True

        if _server_thread and _server_thread.is_alive():
            _server_thread.join(timeout=5.0)

        _server_running = False
        _uvicorn_server = None
        _server_thread = None

        self.report({'INFO'}, "MCP Server stopped")
        return {'FINISHED'}


# ── Panel ──────────────────────────────────────────────────────────────────

class MCP_PT_panel(bpy.types.Panel):
    bl_label = "Universal MCP"
    bl_idname = "MCP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MCP"

    def draw(self, context):
        layout = self.layout

        v = bl_info["version"]
        layout.label(text=f"Universal Blender MCP  v{v[0]}.{v[1]}.{v[2]}", icon='TOOL_SETTINGS')
        layout.separator()

        if _server_running:
            layout.label(text="Status: RUNNING", icon='CHECKMARK')
            row = layout.row()
            row.alert = True
            row.operator("mcp.stop_server", icon='CANCEL')
        else:
            layout.label(text="Status: Stopped", icon='RADIOBUT_OFF')
            layout.operator("mcp.start_server", icon='PLAY')

        layout.separator()
        box = layout.box()
        col = box.column(align=True)
        col.label(text="MCP Endpoint:", icon='LINKED')
        col.label(text=f"http://127.0.0.1:{PORT}/mcp")

        layout.separator()
        layout.label(text="Compatible clients:")
        col = layout.column(align=True)
        col.label(text="  Claude / Cursor / Continue.dev")
        col.label(text="  LM Studio / Open WebUI")
        col.label(text="  Any MCP-compatible frontend")


# ── Registration ───────────────────────────────────────────────────────────

_classes = (MCP_OT_start_server, MCP_OT_stop_server, MCP_PT_panel)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    global _server_running, _uvicorn_server

    if _server_running and _uvicorn_server:
        _uvicorn_server.should_exit = True
        _server_running = False

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
