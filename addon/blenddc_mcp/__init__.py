bl_info = {
    "name": "BlendDC-MCP - Datacenter Asset Factory for UPTIME",
    "author": "DaRealDaHoodie",
    "version": (3, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > N-Panel > BlendDC-MCP",
    "description": "Complete production pipeline for realistic datacenter racks, equipment, cabling, materials, variation, failure states, and full facility sections. Built specifically for the UE5 game UPTIME.",
    "warning": "",
    "doc_url": "https://github.com/DaRealDaHoodie/BlendDC-MCP",
    "category": "Add Mesh",
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

# Packages are installed here — inside the addon folder, always writable, no admin needed.
_LIB_DIR = str(Path(__file__).parent / "lib")


def _ensure_lib_on_path() -> None:
    """Add the addon's lib directory to sys.path so installed packages are importable."""
    if _LIB_DIR not in sys.path:
        sys.path.insert(0, _LIB_DIR)
    # pywin32 splits itself across win32/ and win32/lib/ — .pth files aren't
    # processed for --target installs so we add both manually.
    for subdir in ("win32", os.path.join("win32", "lib")):
        full = str(Path(_LIB_DIR) / subdir)
        if os.path.isdir(full) and full not in sys.path:
            sys.path.insert(0, full)
    # Register the DLL directory so Windows can find pywintypes313.dll.
    # os.add_dll_directory is Windows-only (Python 3.8+).
    if hasattr(os, "add_dll_directory"):
        dll_dir = str(Path(_LIB_DIR) / "pywin32_system32")
        if os.path.isdir(dll_dir):
            os.add_dll_directory(dll_dir)


def _pip_install(*packages: str) -> None:
    """Install packages into the addon's lib/ directory."""
    os.makedirs(_LIB_DIR, exist_ok=True)
    subprocess.check_call(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        stderr=subprocess.DEVNULL,
    )
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--target", _LIB_DIR] + list(packages)
    )


def _ensure_dependencies() -> bool:
    """Return True if all required packages are available (installing if needed)."""
    _ensure_lib_on_path()

    missing = []
    for pkg in ("mcp", "uvicorn"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return True

    print(f"[BlendDC-MCP] Installing: {missing}")
    try:
        # On Windows: use plain mcp (not mcp[cli]) to avoid pulling in prompt_toolkit
        # and rich which require pywin32; use plain uvicorn to avoid uvloop (no Windows wheels).
        if sys.platform == "win32":
            _pip_install("mcp", "uvicorn")
        else:
            _pip_install("mcp[cli]", "uvicorn[standard]")
        _ensure_lib_on_path()
        print("[BlendDC-MCP] Dependencies installed.")
        return True
    except Exception as exc:
        print(f"[BlendDC-MCP] ERROR installing dependencies: {exc}")
        return False


# ── Server management ──────────────────────────────────────────────────────

def _get_server_app():
    """Import the bundled server module, pre-warm schema cache, and return the ASGI app."""
    addon_dir = str(Path(__file__).parent)
    # Insert at position 0 so the addon's bare module names always win over
    # any same-named system modules (e.g. 'core', 'constants', 'server').
    if addon_dir not in sys.path:
        sys.path.insert(0, addon_dir)
    else:
        # Move to front if it slipped back during a previous reload
        sys.path.remove(addon_dir)
        sys.path.insert(0, addon_dir)

    import importlib

    # Reload modules in dependency order so each picks up fresh code.
    # CRITICAL: core must reload FIRST — it creates the shared FastMCP instance.
    # All tool modules must reload AFTER core so their @mcp.tool() decorators
    # register onto the fresh mcp instance, not a stale one from a prior reload.
    for _mod_name in (
        "constants",
        "core",
        "discovery",
        "rag_store",
        "rack_tools",
        "mesh_tools",
        "gn_tools",
        "export_tools",
        "equipment_tools",
        "material_tools",
        "bay_tools",
        "cable_tools",
        "variation_tools",
        "facility_tools",
        "polish_tools",
    ):
        if _mod_name in sys.modules:
            importlib.reload(sys.modules[_mod_name])

    _server_was_loaded = "server" in sys.modules
    import server as _srv
    if _server_was_loaded:
        importlib.reload(_srv)
    app = _srv.get_app()

    # Pre-warm Pydantic schema generation for all tools synchronously.
    # Without this, the first tools/list call from the client takes 15+ seconds
    # (cold Pydantic schema generation for 54 tools), which exceeds LM Studio's
    # timeout and causes it to cancel the request and retry forever.
    print("[BlendDC-MCP] Pre-warming tool schema cache...", flush=True)
    try:
        import asyncio
        import time as _time
        t0 = _time.perf_counter()
        # Call the exact async path FastMCP uses for a real tools/list request.
        # This forces Pydantic to build and cache every schema up front so the
        # first client request returns in milliseconds, not 15+ seconds.
        tools = asyncio.run(_srv.mcp.list_tools())
        elapsed = _time.perf_counter() - t0
        print(f"[BlendDC-MCP] Schema cache ready — {len(tools)} tools in {elapsed:.2f}s", flush=True)
    except Exception as exc:
        print(f"[BlendDC-MCP] Schema pre-warm failed (non-fatal): {exc}", flush=True)

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
            print(f"[BlendDC-MCP] Server running — http://127.0.0.1:{PORT}/mcp", flush=True)
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
    bl_label = "BlendDC-MCP"
    bl_idname = "MCP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "BlendDC"

    def draw(self, context):
        layout = self.layout

        v = bl_info["version"]
        layout.label(text=f"BlendDC-MCP  v{v[0]}.{v[1]}.{v[2]}", icon='TOOL_SETTINGS')
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
