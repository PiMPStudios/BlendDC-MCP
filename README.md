# BlendDC-MCP

### Datacenter Asset Factory for UPTIME

**189 production-ready tools for building photorealistic datacenter environments in Blender — driven by any MCP-compatible AI frontend.**

BlendDC-MCP is a Blender addon that starts a lightweight MCP server inside Blender's Python process. Connect Claude, Cursor, LM Studio, or any MCP client, then describe what you want to build — racks, bays, cables, variation, failure states, full facility sections — and get production-ready UE5 assets back.

Built from the ground up for **UPTIME**, an Unreal Engine 5 datacenter operations simulator where players physically configure real-scale datacenters at runtime.

```
AI Frontend  ──HTTP──▶  BlendDC-MCP Server (inside Blender)  ──bpy──▶  Blender Scene
  (Claude,              FastMCP / uvicorn                               racks, cables,
   Cursor, etc.)        http://127.0.0.1:8400/mcp                       materials, UE5…
```

---

## What It Does

BlendDC-MCP gives an AI 189 tools spanning the complete datacenter asset pipeline — from a single EIA-310 rack cabinet to a fully dressed multi-bay facility section ready for UE5 import.

### The Full Pipeline

```
create_facility_section          ← lay out a grid of bays
  └─ create_rack_row             ← hot-aisle / cold-aisle row pairs
       └─ create_rack_cabinet    ← EIA-310 compliant 42U cabinet (origin at base-front-centre)
            └─ populate_rack_procedural   ← fill with servers, switches, patch panels
                 └─ route_cables_between_racks  ← NURBS cable curves on tray paths
                      └─ randomize_facility_variation  ← procedural wear, dust, damage
                           └─ apply_facility_theme     ← aged_colo / post_incident / crisis
                                └─ export_facility_layout_json  ← UE5 manifest + FBX
```

### 189 Tools Across 12 Modules

| Module | Tools | What It Builds |
|---|---:|---|
| `server` (core) | 62 | Scene queries, transforms, materials, render, export primitives |
| `rack_tools` | 25 | EIA-310 rack cabinets, rails, doors, blanks, cable managers |
| `mesh_tools` | 12 | Hard-surface mesh primitives, booleans, bevels, chamfers |
| `gn_tools` | 7 | Geometry Nodes setups: EIA holes, perforated panels, cable bundles |
| `export_tools` | 14 | UE5 FBX pipeline: transforms, LODs, manifests, socket embedding |
| `equipment_tools` | 9 | Equipment kitbashing, rack population, slot assignment |
| `material_tools` | 12 | PBR materials, LED states, brushed metal, anodised finishes |
| `bay_tools` | 11 | Hot/cold aisle bays, rack rows, raised-floor tiles, containment |
| `cable_tools` | 12 | NURBS cable paths, bundles, trays, patch panels, routing validation |
| `variation_tools` | 11 | Procedural wear, dust, damage nodes, failure states, themes |
| `facility_tools` | 11 | Multi-bay sections, corridors, power/cooling zones, UPS/CRAC |
| `polish_tools` | 12 | Undo checkpoints, session log, scene inventory, documentation gen |

### Phase 9 Safety Layer

The `polish_tools` module adds a professional-grade safety net for long asset-building sessions:

- **`push_undo_checkpoint`** — Named Blender undo steps before any destructive operation
- **`confirm_destructive`** — Dry-run pre-flight for `clear_cables`, `reset_variation`, etc.
- **`backup_section_metadata`** — Snapshot all custom properties to JSON; restore after reloads
- **`quick_save_scene`** — Timestamped `.blend` copy without touching your working file
- **`suggest_next_step`** — Scene-aware AI recommendations with pre-filled tool arguments
- **`validate_entire_scene`** — One-call health check before every export

---

## Quick Start

```bash
# 1. Install the addon (see Installation below)
# 2. In Blender: N-Panel → BlendDC tab → ▶ Start MCP Server
# 3. Connect your AI client to http://127.0.0.1:8400/mcp
```

Then ask your AI:

> *"Build me a 2×3 section of datacenter bays with hot-aisle containment, route ethernet cables between racks, apply aged_colo variation, then export to `/tmp/uptime_exports/` as a UE5 manifest."*

The AI calls `create_facility_section` → `route_cables_between_racks` →
`apply_facility_theme` → `export_facility_layout_json` — and reports back when done.

### Example Tool Calls

```python
# Create a 2×4 facility section, fully populated
create_facility_section(
    section_name          = "DC_Floor_01",
    bays_x                = 2,
    bays_y                = 4,
    racks_per_bay         = 6,
    populate_preset       = "standard_3tier",
    hot_aisle_containment = True,
)

# Apply a post-incident theme with a named epicentre bay
apply_facility_theme(
    section_name  = "DC_Floor_01",
    theme         = "post_incident",
    epicenter_bay = "Bay_DC_Floor_01_1_2",
)

# Export the full facility — manifest + cables + variation metadata
export_facility_layout_json(
    section_name      = "DC_Floor_01",
    output_path       = "/tmp/uptime/DC_Floor_01_layout.json",
    include_cables    = True,
    include_variation = True,
)
```

---

## Installation

### Option A — Pre-built zip (recommended)

1. Download `blenddcmcp_v3.0.0.zip` from the [Releases](../../releases) page
2. Open Blender → **Edit → Preferences → Add-ons → Install…**
3. Select the zip → enable **BlendDC-MCP - Datacenter Asset Factory for UPTIME**

### Option B — Build from source

```bash
git clone https://github.com/DaRealDaHoodie/BlendDC-MCP.git
cd BlendDC-MCP
python3 build_addon.py        # writes dist/blenddcmcp_v3.0.0.zip
```

Then install the zip as above.

### First Start

1. Press **N** in the 3D Viewport → **BlendDC** tab → **▶ Start MCP Server**
2. On first start, `fastmcp` and `uvicorn` install automatically into `addon/lib/`
   — no system packages required, no admin rights needed
3. The System Console confirms:

```
[BlendDC-MCP] Schema cache ready — 189 tools in 0.01s
[BlendDC-MCP] Server running — http://127.0.0.1:8400/mcp
```

### Connect Your AI Client

**Claude Desktop** (`~/.claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "blenddcmcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8400/mcp"]
    }
  }
}
```

**Cursor / Continue.dev** — Add MCP server `http://127.0.0.1:8400/mcp` in settings.

**LM Studio** — MCP Servers → Add → `http://127.0.0.1:8400/mcp`.

---

## Running the Test Suite

A full integration test validates the pipeline end-to-end — facility creation through UE5 export — from outside Blender via the live MCP endpoint:

```bash
# Start the server in Blender first, then from a terminal:
python3 tests/full_uptime_pipeline_test.py

# Override the export directory:
OUTPUT_DIR=~/Desktop/uptime_test python3 tests/full_uptime_pipeline_test.py
```

The test runner covers 7 phases and prints `[PASS]` / `[WARN]` / `[FAIL]` per check with timing and a final summary. Requires only the Python standard library — no extra packages.

```
══════════════════════════════════════════════════════════════════════
  UPTIME PIPELINE TEST SUMMARY   (14.3s total)
══════════════════════════════════════════════════════════════════════
  Passed :  47
  Warned :   3
  Failed :   0
  ✓  ALL TESTS PASSED
```

---

## Architecture

```
BlendDC-MCP/
├── addon/
│   └── blenddc_mcp/               # Blender addon package
│       ├── __init__.py            # bl_info, server start/stop, N-Panel UI
│       ├── core.py                # FastMCP instance, @thread_safe, middleware
│       ├── constants.py           # EIA-310 dimensions, UE5 axis conventions
│       ├── server.py              # 62 core tools + module import registry
│       ├── rack_tools.py          # 25 rack cabinet tools
│       ├── mesh_tools.py          # 12 hard-surface mesh tools
│       ├── gn_tools.py            # 7 Geometry Nodes tools
│       ├── export_tools.py        # 14 UE5 export pipeline tools
│       ├── equipment_tools.py     # 9 equipment kitbash + population tools
│       ├── material_tools.py      # 12 material + texturing tools
│       ├── bay_tools.py           # 11 bay + row generation tools
│       ├── cable_tools.py         # 12 cable management + routing tools
│       ├── variation_tools.py     # 11 variation + failure state tools
│       ├── facility_tools.py      # 11 facility layout + export tools
│       └── polish_tools.py        # 12 polish, UX, safety + documentation tools
├── tests/
│   ├── uptime_pipeline_test.py       # In-Blender test (Scripting workspace)
│   └── full_uptime_pipeline_test.py  # External integration test (v3.0.0)
├── docs/
│   └── tool_reference.md          # Full 189-tool reference (auto-generated)
├── build_addon.py
├── CHANGELOG.md
└── README.md
```

### Key Design Decisions

**Thread safety** — Every Blender API call is dispatched to the main thread via `bpy.app.timers.register()`. The uvicorn HTTP server runs in a daemon thread; the `@thread_safe` decorator queues work to the main thread and blocks until complete.

**Copy-on-write materials** — Variation tools copy shared materials before injecting shader nodes, so identical-looking neighbours are never unintentionally modified. Injected nodes are labelled `[WEAR]`, `[DUST]`, `[DAMAGE]` for clean removal by `reset_variation`.

**Self-contained modules** — Each tool module copies required helpers rather than importing from sibling modules, preventing circular import failures during server hot-reload.

**EIA-310 compliance** — 44.45 mm per U, 482.6 mm inner rail span, rack origin at base-front-centre. All dimensions verified against real-world rack datasheets.

**UE5 axis convention** — FBX exports use `-X` forward, `Z` up (`axis_forward='-X'`, `axis_up='Z'`) at `FBX Units Scale` so racks import at correct real-world metre scale with no manual correction.

---

## Generating the Tool Reference

The full 189-tool reference with parameter tables is auto-generated from live docstrings. Start the server and ask your AI:

```python
export_tool_reference(
    output_path = "/path/to/BlendDC-MCP/docs/tool_reference.md",
    format      = "markdown",
)
```

See the current reference: [docs/tool_reference.md](docs/tool_reference.md)

---

## Built for UPTIME

BlendDC-MCP was designed for a single purpose: building the asset pipeline for **UPTIME**, a UE5 game where players operate and expand a real-scale datacenter.

Every design decision reflects that goal:

- **Real-world dimensions** — racks, equipment, cables, and aisles match actual datacenter specifications so gameplay feels physically accurate
- **Variation system** — aged equipment, post-incident failure states, and hot-zone wear gradients give each section a believable history
- **UE5-first export** — `SOCKET_` attachment points, LOD sets, manifests, and spline-ready cable control points are designed for UE5's StaticMesh and PCG workflows
- **Facility-scale output** — a single `create_facility_section` call produces a complete, populated, variation-dressed section ready for UE5 level import

---

## Requirements

- Blender **4.0 or newer** (tested on 4.x and 5.x)
- Internet access on first start (to install `fastmcp` and `uvicorn` into `addon/lib/`)
- Any MCP-compatible AI client

---

## License

[MIT License](LICENSE) — © 2026 DaRealDaHoodie

---

*BlendDC-MCP is an independent project and is not affiliated with Autodesk, Epic Games, or Anthropic.*
