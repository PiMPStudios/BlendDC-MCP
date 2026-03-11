# Universal Blender MCP

A **Model Context Protocol (MCP) server** that runs inside Blender, letting any MCP-compatible LLM frontend control Blender through natural language.

Works with **Claude Desktop, Cursor, Continue.dev, LM Studio, Open WebUI**, and anything else that speaks MCP — no API keys or cloud services required.

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

## Available Tools (54)

### Objects
| Tool | Description |
|------|-------------|
| `list_objects` | List all objects in the scene |
| `get_object_info` | Get location, rotation, scale, type, dimensions, visibility |
| `create_object` | Add a primitive (cube, sphere, cylinder, plane, cone, torus) |
| `delete_object` | Remove an object by name |
| `rename_object` | Rename an object |
| `duplicate_object` | Copy an object |
| `join_objects` | Join a list of mesh objects into one |
| `parent_objects` | Set a parent-child relationship between two objects |
| `move_object` | Set absolute world-space location |
| `rotate_object` | Set rotation in radians (XYZ Euler) |
| `scale_object` | Scale per-axis |
| `apply_transforms` | Apply location / rotation / scale to mesh data |
| `set_origin` | Move the object origin (to geometry, cursor, mass centre…) |
| `set_object_visibility` | Show / hide in viewport and render |
| `set_active_object` | Select and make active |

### Selection
| Tool | Description |
|------|-------------|
| `get_selected_objects` | Return names of currently selected objects |
| `select_objects` | Select a list of objects by name |

### Materials
| Tool | Description |
|------|-------------|
| `list_materials` | List all materials in the file |
| `assign_material` | Create or assign a material with a base color |
| `set_material_color` | Update the Base Color of an existing material |
| `set_material_property` | Set Metallic, Roughness, Emission Strength, etc. |
| `delete_material` | Remove a material |

### Lights
| Tool | Description |
|------|-------------|
| `add_light` | Add POINT / SUN / SPOT / AREA light |

### Camera
| Tool | Description |
|------|-------------|
| `add_camera` | Add a new camera |
| `set_active_camera` | Set which camera is used for rendering |
| `set_camera_properties` | Adjust focal length and clip distances |
| `point_camera_at` | Add a Track-To constraint to aim a camera at an object |

### Modifiers
| Tool | Description |
|------|-------------|
| `add_modifier` | Add a modifier (SUBSURF, BEVEL, SOLIDIFY, MIRROR, ARRAY…) |
| `list_modifiers` | List all modifiers on an object |
| `set_modifier_property` | Change any modifier property by attribute name |
| `apply_modifier` | Collapse a modifier into the mesh |

### Animation
| Tool | Description |
|------|-------------|
| `set_scene_frame` | Jump to an animation frame |
| `set_frame_range` | Set scene start and end frames |
| `insert_keyframe` | Insert a keyframe for location / rotation / scale |
| `get_keyframes` | List all keyframes on an object grouped by data path |

### Rendering
| Tool | Description |
|------|-------------|
| `render_preview` | Fast OpenGL viewport render → returns PNG path |
| `full_render` | Full CPU/GPU render → saves to output path |
| `set_render_engine` | Switch between EEVEE, Cycles, Workbench |
| `set_render_resolution` | Set render width, height, and percentage |
| `set_render_output` | Set output file path and format |

### Scene & World
| Tool | Description |
|------|-------------|
| `get_scene_info` | Scene name, frame range, FPS, object count, render settings |
| `clear_scene` | Remove all (or all non-camera/light) objects |
| `save_file` | Save the current .blend file |
| `set_world_color` | Set the world background to a solid color |

### Collections
| Tool | Description |
|------|-------------|
| `list_collections` | List all collections |
| `create_collection` | Create a new collection |
| `move_to_collection` | Move an object into a collection |

### 3D Cursor
| Tool | Description |
|------|-------------|
| `get_cursor_location` | Get the 3D cursor position |
| `set_cursor_location` | Move the 3D cursor |

### Viewport
| Tool | Description |
|------|-------------|
| `set_viewport_shading` | Switch between WIREFRAME / SOLID / MATERIAL / RENDERED |

### Text
| Tool | Description |
|------|-------------|
| `add_text_object` | Add a 3D text object with optional extrusion |

### Import / Export
| Tool | Description |
|------|-------------|
| `import_file` | Import .obj, .fbx, .glb/.gltf, .stl, .ply, .abc, .usd, .x3d |
| `export_file` | Export to .obj, .fbx, .glb/.gltf, .stl, .ply, .abc, .usd, .x3d |

### Scripting
| Tool | Description |
|------|-------------|
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
