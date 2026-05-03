# Changelog

All notable changes to BlendDC-MCP are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [3.0.0] — 2026-04-11

### Project rename

- Renamed from **Universal Blender MCP** → **BlendDC-MCP**
- Updated `bl_info` name, description, author (`DaRealDaHoodie`), and `doc_url`
- N-Panel tab renamed from `MCP` → `BlendDC`
- System console prefix updated to `[BlendDC-MCP]`

### Added — Phase 9: Polish, UX, Safety & Production Readiness (`polish_tools.py`, 12 tools)

**Session Awareness**

- `get_scene_inventory` — structured tally of all sections, bays, racks, cables, variation objects, and orphaned datablocks in the current scene
- `log_operation` — append a timestamped entry to the in-session audit log; optional on-disk NDJSON file
- `get_session_log` — read back session log with status filtering and formatted ASCII table output

**Safety & Undo**

- `push_undo_checkpoint` — push a named Blender undo step before any destructive operation; auto-recorded in session log
- `confirm_destructive` — dry-run pre-flight for `clear_cables`, `reset_variation`, `clear_orphaned_data`, etc.; `execute=True` runs with auto-checkpoint
- `backup_section_metadata` — serialise all collection custom properties to a JSON snapshot (metadata only, not geometry)
- `restore_section_metadata` — re-apply backed-up custom properties; defaults to `dry_run=True`
- `quick_save_scene` — timestamped `.blend` copy via `copy=True` (working file path unchanged)

**Quality of Life**

- `validate_entire_scene` — single call runs `validate_facility` per section, `validate_cable_routing` scene-wide, and orphaned rack check
- `suggest_next_step` — inspects actual scene state and returns up to 8 prioritised, pre-filled tool call recommendations
- `list_all_tools` — all 189 registered tools grouped by module with one-line descriptions; supports module and keyword filtering
- `export_tool_reference` — generates a full Markdown or JSON tool reference from live docstrings and parameter schemas

### Changed

- Version bumped to `(3, 0, 0)` — reflects major milestone: full facility pipeline complete + production safety layer added
- `bl_info.category` changed from `"Development"` to `"Add Mesh"`
- Module reload order in `__init__.py` updated to include `polish_tools`

---

## [2.7.0] — 2026-04-11

### Added — Phase 8: Full Facility & Multi-Bay Tools (`facility_tools.py`, 11 tools)

- `create_facility_section` — grid of N×M bays with perimeter walls, raised-floor slab, optional `populate_preset`
- `create_corridor` — floor tiles + overhead cable tray + `SOCKET_Light_XX` empties at ceiling height
- `add_power_cooling_zone` — UPS boxes, CRAC boxes, busway tray with `SOCKET_Busway_Tap_XX` sockets
- `create_multi_bay_row` — sequential bay placement with optional shared cable tray
- `populate_facility_from_json` — JSON-driven facility population delegating to bay_tools per bay
- `apply_facility_theme` — section-wide theme with `epicenter_bay` parameter for incident-style themes
- `randomize_facility_variation` — hot-zone gradient wear across all bays using distance-based `severity_bias`
- `get_section_bays` — read-only bay list with world positions
- `export_facility_layout_json` — comprehensive UE5 manifest: racks, equipment, cables, variation, bounding box
- `validate_facility` — delegates per-bay and cable validation, returns aggregated pass/warn/fail report
- `get_facility_info` — section totals including `ue5_actor_estimate`

---

## [2.6.0] — 2026-04-10

### Added — Phase 7: Advanced Variation & Failure States (`variation_tools.py`, 11 tools)

- `apply_wear_variation` — Noise Texture driven scratch/roughness overlay (copy-on-write per material)
- `apply_dust_overlay` — surface-normal driven dust accumulation (heaviest on top faces)
- `randomize_color_tint` — seeded per-object hue/saturation shift
- `apply_damage_state` — roughness spike, scorch overlay, heat glow emission for high damage levels
- `set_failure_state` — combines `apply_damage_state` + LED state + `SOCKET_MaintenanceTag` empty
- `generate_failure_preset` — named presets: `overheated`, `failed_unit`, `degraded`, `maintenance`
- `propagate_failure` — spread failure state to neighbouring equipment within a radius
- `reset_variation` — cleanly remove all `[WEAR]`/`[DUST]`/`[DAMAGE]` labelled nodes
- `randomize_bay_variation` — full bay pass with optional `severity_bias` for hot-aisle gradients
- `apply_theme` — bay-level themes: `new_install`, `aged_dc`, `post_incident`, `high_security`, `edge_pod`
- `get_variation_report` — read-only coverage summary per bay

---

## [2.5.0] — 2026-04-10

### Added — Phase 6: Cable Management & Routing (`cable_tools.py`, 12 tools)

- `add_brush_strip` — 1U horizontal cable brush strip geometry
- `add_vertical_cable_manager` — side-mount vertical cable management panel
- `add_overhead_cable_tray` — flat tray with optional lid and mounting brackets
- `add_cable_entry_panel` — rear panel with cable entry openings
- `add_cable_endpoint_sockets` — `SOCKET_` empties at rack cable ports
- `create_cable_path` — NURBS curve with catenary sag, configurable segments
- `route_cables_between_racks` — three-waypoint paths (up → across tray → down) between racks
- `generate_cable_bundle` — multi-cable bundle with circular cross-section and taper
- `add_patch_panel_connections` — 1U patch panel with port-to-port cable runs
- `export_cable_data_json` — UE5 spline mesh / Cable Component manifest
- `validate_cable_routing` — loose endpoints, over-length cables, duplicate routes, missing materials
- `clear_cables` — safe removal with `confirm=True` gate

---

## [2.4.0] — 2026-04-09

### Added — Phase 5: Bay & Row Generation (`bay_tools.py`, 11 tools)

- `create_rack_row` — hot or cold aisle row with configurable rack count and spacing
- `create_bay` — hot/cold aisle bay pair with floor tiles, cable tray, optional HAC containment caps
- `create_bay_preset` — fully populated bay (calls `create_rack_row` + `populate_rack_procedural`)
- `populate_bay_from_json` — JSON-driven bay population delegating to `equipment_tools`
- `duplicate_bay` — deep copy of a bay collection with renamed hierarchy
- `export_bay_layout_json` — per-bay UE5 manifest with rack positions and equipment slots
- `validate_bay` — U-slot overlap detection, origin checks, equipment metadata validation
- `get_bay_info` — rack count, U capacity/used, equipment breakdown, bounding box
- `create_cable_tray_run` — continuous overhead tray spanning a bay
- `set_bay_lighting` — `SOCKET_Light_XX` empties at configurable ceiling height
- `mirror_bay` — reflect a bay about the hot-aisle centre line

---

## [2.3.0] — 2026-04-08

### Added — Phase 4: Material & Texturing Tools (`material_tools.py`, 12 tools)

PBR material creation, LED state management, surface finish variants (brushed metal, anodised, painted), rack-specific material presets, UV mapping helpers, and material-to-FBX export utilities.

---

## [2.2.0] — 2026-04-07

### Added — Phase 3: UE5 Export Pipeline (`export_tools.py`, 14 tools)

Full UE5 export pipeline: transform application, FBX export with correct axis conventions, LOD mesh generation (LOD1/LOD2 with configurable decimation ratios), `SOCKET_` empty embedding, asset registry JSON manifest, and batch export for equipment sets.

---

## [2.1.0] — 2026-04-06

### Added — Phase 2: Equipment Kitbashing (`equipment_tools.py`, 9 tools)

Rack equipment population (procedural + JSON-driven), equipment type registration, U-slot assignment, `SOCKET_Power` / `SOCKET_Data_XX` empty placement, clear-population utilities.

---

## [2.0.0] — 2026-04-05

### Added — Phase 1: Core Rack Tools (`rack_tools.py`, 25 tools)

EIA-310 rack cabinet geometry, rail webs, mounting rails, blanking panels, 1U/2U/4U equipment primitives, rack doors (front/rear), cable brush strips, validate/info/export per-rack tools.

### Changed

- Migrated from single-file script to modular architecture (`core.py` + per-domain tool modules)
- Introduced `@thread_safe` decorator for all Blender API calls
- Schema pre-warming on server start (eliminates 15-second first-call delay in LM Studio)
- Windows compatibility: avoids `pywin32` by using plain `mcp` + `uvicorn` on Win32

---

## [1.4.1] — 2026-03-31

### Fixed

- Windows dependency install for Blender 5.1 / Python 3.13

## [1.4.0] — 2026-03-28

### Added

- UV unwrap, texture wiring, batch export, texture bake, and mesh edit tools
- Version display in N-Panel UI

## [1.x.x] — Initial releases

Universal Blender MCP — general-purpose Blender control via MCP for Claude Desktop, Cursor, Continue.dev, LM Studio, Open WebUI.
