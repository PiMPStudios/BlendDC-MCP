from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
import bpy  # Blender API available since server runs in Blender context

app = FastAPI(title="Universal Blender MCP")

# MCP server instance
mcp = FastMCP("blender-universal")

# Example tool (we'll expand this massively in tools.py)
@mcp.tool()
def get_scene_objects() -> list[str]:
    """Return list of all object names in the current scene."""
    return [obj.name for obj in bpy.data.objects]

# Mount MCP endpoints under /mcp
app.mount("/mcp", mcp.app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
