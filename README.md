# Universal Blender MCP

A **Model Context Protocol (MCP) server** that runs inside Blender, letting any MCP-compatible LLM frontend control Blender through natural language.

Works with **Claude Desktop, Cursor, Continue.dev, LM Studio, Open WebUI, AnythingLLM**, and anything else that speaks MCP — no API keys or cloud services required.

---

## How It Works

```
LLM Frontend  ──HTTP──▶  MCP Server (inside Blender)  ──bpy──▶  Blender Scene
  (Claude,               FastMCP / uvicorn                       objects, materials,
   Cursor, etc.)         http://127.0.0.1:8400/mcp               lights, render…
```

The addon starts a lightweight HTTP server inside Blender's Python process. Every tool call is dispatched to Blender's main thread using `bpy.app.timers`, keeping the UI responsive and the API calls thread-safe. The server speaks the [MCP Streamable HTTP](https://spec.modelcontextprotocol.io/) transport, which all modern MCP clients support.

---

## Installation

### 1. Download the addon

**Option A — Pre-built zip (recommended)**

Download `universal_blender_mcp_vX.Y.Z.zip` from the [Releases](../../releases) page.

**Option B — Build from source**

```bash
git clone https://github.com/DaRealDaHoodie/universal-blender-mcp.git
cd universal-blender-mcp
python3 build_addon.py        # creates dist/universal_blender_mcp_v*.zip
```

### 2. Install in Blender

1. Open Blender
2. **Edit → Preferences → Add-ons → Install…**
3. Select the `.zip` file
4. Enable **Universal Blender MCP** in the add-on list

### 3. Start the server

1. Press **N** in the 3D Viewport to open the N-Panel
2. Go to the **MCP** tab
3. Click **▶ Start MCP Server**

On first start the addon automatically installs `fastmcp` and `uvicorn` into Blender's Python — this takes about 30 seconds and only happens once.

The server runs at:
```
http://127.0.0.1:8400/mcp
```

---

## Connecting Your LLM Frontend

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "blender": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8400/mcp"
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` (or Cursor's MCP settings):

```json
{
  "mcpServers": {
    "blender": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8400/mcp"
    }
  }
}
```

### Continue.dev

Add to `~/.continue/config.yaml`:

```yaml
mcpServers:
  - name: Blender
    type: streamable-http
    url: http://127.0.0.1:8400/mcp
```

### LM Studio

Add to `mcp.json`:

```json
{
  "servers": [
    {
      "name": "Blender",
      "type": "remote",
      "url": "http://127.0.0.1:8400/mcp"
    }
  ]
}
```

### Quick test (curl)

```bash
curl -X POST http://127.0.0.1:8400/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0", "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "test", "version": "1.0"}
    }
  }'
```

---

## Available Tools

| Tool | Description |
|------|-------------|
| `list_objects` | List all objects in the scene |
| `get_object_info` | Get location, rotation, scale, type, dimensions |
| `create_object` | Add a primitive (cube, sphere, cylinder, plane, cone, torus) |
| `delete_object` | Remove an object by name |
| `move_object` | Set absolute world-space location |
| `rotate_object` | Set rotation in radians (XYZ Euler) |
| `scale_object` | Scale per-axis |
| `duplicate_object` | Copy an object |
| `set_object_visibility` | Show / hide in viewport and render |
| `set_active_object` | Select and make active |
| `assign_material` | Create or assign a material with a base color |
| `set_material_color` | Update the Base Color of an existing material |
| `add_light` | Add POINT / SUN / SPOT / AREA light |
| `add_modifier` | Add a modifier (SUBSURF, BEVEL, SOLIDIFY, MIRROR…) |
| `apply_modifier` | Collapse a modifier into the mesh |
| `get_scene_info` | Scene name, frame range, FPS, render settings |
| `set_scene_frame` | Jump to an animation frame |
| `render_preview` | OpenGL viewport render → returns PNG path |
| `clear_scene` | Remove all (or all non-camera/light) objects |
| `save_file` | Save the current .blend file |
| `execute_python` | Run arbitrary Python in Blender's environment |

---

## Project Structure

```
universal-blender-mcp/
├── addon/
│   └── universal_blender_mcp/   ← installable Blender addon
│       ├── __init__.py           ← bl_info, UI panel, server management
│       └── server.py             ← FastMCP tools (all Blender API calls)
├── build_addon.py               ← builds the installable .zip
├── pyproject.toml
└── LICENSE
```

---

## Building a Release

```bash
python3 build_addon.py
# → dist/universal_blender_mcp_v1.1.0.zip
```

---

## Requirements

- Blender 4.0 or later (Python 3.11+)
- Internet connection on first start (to download `fastmcp` and `uvicorn`)

---

## License

MIT — see [LICENSE](LICENSE)
