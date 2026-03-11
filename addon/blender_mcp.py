bl_info = {
    "name": "Universal Blender MCP",
    "author": "Da Hoodie Guy + community",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > N-Panel > MCP",
    "description": "Universal MCP server for Blender - works with Continue, LM Studio, Cursor, etc.",
    "category": "Development",
}

import bpy
import subprocess
import sys
import os
import threading
from pathlib import Path

# ============================================
# CONFIG
# ============================================

PORT = 8000
SERVER_SCRIPT = Path(__file__).parent.parent / "server" / "main.py"

class MCP_OT_start_server(bpy.types.Operator):
    bl_idname = "mcp.start_server"
    bl_label = "Start MCP Server"
    bl_options = {'REGISTER'}

    def execute(self, context):
        # Auto-install deps if missing (uv handles most, but ensure fastapi etc.)
        try:
            import fastapi
        except ImportError:
            self.report({'INFO'}, "Installing dependencies...")
            python_exe = sys.executable
            subprocess.check_call([python_exe, "-m", "pip", "install", "fastapi", "uvicorn[standard]", "pydantic", "mcp", "numpy"])

        # Start server in background thread
        def run_server():
            subprocess.Popen([sys.executable, str(SERVER_SCRIPT)], cwd=str(SERVER_SCRIPT.parent))

        threading.Thread(target=run_server, daemon=True).start()

        self.report({'INFO'}, f"MCP Server started on http://localhost:{PORT}/mcp")
        return {'FINISHED'}

class MCP_PT_panel(bpy.types.Panel):
    bl_label = "Universal MCP"
    bl_idname = "MCP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MCP"

    def draw(self, context):
        layout = self.layout
        layout.operator("mcp.start_server", text="Start MCP Server", icon='PLAY')
        layout.label(text=f"URL: http://localhost:{PORT}/mcp")

classes = (MCP_OT_start_server, MCP_PT_panel)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
