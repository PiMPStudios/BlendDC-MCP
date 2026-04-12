# BlendDC-MCP — Tool Reference

> **This document is auto-generated from live docstrings and parameter schemas.**
>
> To regenerate with the latest tools and full parameter tables, start the MCP
> server in Blender and run:
> ```python
> export_tool_reference(
>     output_path = "/path/to/BlendDC-MCP/docs/tool_reference.md",
>     format      = "markdown",
> )
> ```
>
> What follows is the structure + the `polish_tools` module in full detail.
> All other modules list their tools; run `export_tool_reference` for
> complete parameter tables on all 189 tools.

---

**v3.0.0  ·  189 tools  ·  12 modules**

---

## Module Index

- [server](#server)  (62 tools)
- [rack_tools](#rack_tools)  (25 tools)
- [mesh_tools](#mesh_tools)  (12 tools)
- [gn_tools](#gn_tools)  (7 tools)
- [export_tools](#export_tools)  (14 tools)
- [equipment_tools](#equipment_tools)  (9 tools)
- [material_tools](#material_tools)  (12 tools)
- [bay_tools](#bay_tools)  (11 tools)
- [cable_tools](#cable_tools)  (12 tools)
- [variation_tools](#variation_tools)  (11 tools)
- [facility_tools](#facility_tools)  (11 tools)
- [polish_tools](#polish_tools)  (12 tools)

---

## server

62 core tools covering scene management, object transforms, visibility, basic mesh
creation, material assignment, render settings, and general Blender scene queries.
These tools are registered directly in `server.py` and form the general-purpose
foundation that all domain modules build on.

*Run `list_all_tools(module_filter="server")` or `export_tool_reference` for the full parameter tables.*

---

## rack_tools

25 tools for EIA-310 compliant rack cabinet geometry.

| Tool | Description |
|---|---|
| `create_rack_cabinet` | EIA-310 rack cabinet with correct U-height geometry and base-front-centre origin |
| `add_rack_rails` | Front and rear mounting rails with EIA hole pattern |
| `add_blanking_panels` | 1U blanking panels to fill empty rack slots |
| `add_rack_doors` | Front and/or rear door panels with optional venting |
| `add_side_panels` | Left/right side panels with optional cable routing cutouts |
| `add_pdu_vertical` | Vertical PDU strip mounted to rack interior |
| `add_cable_brush_strip` | 1U horizontal cable brush entry strip |
| `add_vertical_cable_manager` | Side-mount vertical cable management panel |
| `set_rack_origin` | Ensure rack origin is at base-front-centre (UE5 export requirement) |
| `validate_rack_collection` | Pre-export health check: scale, UVs, normals, origin position |
| `get_rack_info` | Read rack metadata: U-height, width_mm, depth_mm, u_used |
| `clear_rack_population` | Remove all equipment objects from a rack (keeps cabinet geometry) |
| `populate_rack_from_json` | Load rack population from a JSON slot-assignment file |
| `mirror_rack_contents` | Mirror equipment layout to a second rack (hot/cold pairs) |
| `add_rack_label` | SOCKET_Label empty + text object at front-top of rack |
| `set_rack_led_row` | Status LED strip across rack front panel |
| `add_cable_management_arm` | Rear cable management arm (1U) |
| `add_patch_panel` | 24-port or 48-port patch panel geometry |
| `add_kvm_switch` | 1U KVM switch placeholder with SOCKET_KVM empty |
| `add_rack_pdu_rear` | Rear-mount horizontal PDU |
| `lock_rack_doors` | Set door open/closed state via custom property |
| `export_rack_collection_ue5` | Full one-call UE5 export: validate → transforms → FBX → LODs → manifest |
| `duplicate_rack` | Deep copy of a rack collection with renamed hierarchy |
| `align_racks_to_row` | Distribute racks evenly along a row axis |
| `get_rack_socket_positions` | Return world-space positions of all SOCKET_ empties in a rack |

---

## mesh_tools

12 tools for hard-surface mesh operations used throughout the rack and equipment pipeline.

`create_chassis_box`, `bevel_edges`, `add_chamfer`, `boolean_cut`, `inset_face`,
`extrude_face`, `bridge_edges`, `merge_by_distance`, `recalculate_normals`,
`triangulate_mesh`, `apply_all_transforms`, `weld_vertices`

---

## gn_tools

7 Geometry Nodes modifier setups for procedural details.

`add_eia_holes_gn`, `add_perforated_panel_gn`, `add_cable_bundle_gn`,
`add_louvre_vent_gn`, `add_led_array_gn`, `apply_gn_modifier`,
`remove_gn_modifier`

---

## export_tools

14 tools for the complete UE5 export pipeline.

`apply_ue5_transforms`, `export_ue5_fbx`, `generate_lod_meshes`,
`export_lod_set_ue5`, `cleanup_lod_meshes`, `export_scene_manifest`,
`embed_socket_empties`, `validate_fbx_ready`, `batch_export_collections`,
`export_equipment_set_ue5`, `set_lod_screen_size`, `export_collision_mesh`,
`write_asset_registry_json`, `verify_export_directory`

---

## equipment_tools

9 tools for rack equipment creation and population.

`create_1u_server`, `create_2u_server`, `create_network_switch`,
`create_patch_panel_1u`, `populate_rack_procedural`, `populate_rack_from_json`,
`clear_rack_population`, `get_equipment_manifest`, `assign_equipment_sockets`

---

## material_tools

12 tools for PBR materials, surface finishes, and LED states.

`create_rack_material`, `create_brushed_metal_material`, `create_anodised_material`,
`create_painted_steel_material`, `set_led_state`, `set_equipment_color`,
`apply_material_to_collection`, `create_cable_material`, `bake_to_texture`,
`export_material_set`, `reset_material`, `list_scene_materials`

---

## bay_tools

11 tools for hot/cold aisle bay and row generation.

`create_rack_row`, `create_bay`, `create_bay_preset`, `populate_bay_from_json`,
`duplicate_bay`, `export_bay_layout_json`, `validate_bay`, `get_bay_info`,
`create_cable_tray_run`, `set_bay_lighting`, `mirror_bay`

---

## cable_tools

12 tools for NURBS cable routing, management geometry, and validation.

`add_brush_strip`, `add_vertical_cable_manager`, `add_overhead_cable_tray`,
`add_cable_entry_panel`, `add_cable_endpoint_sockets`, `create_cable_path`,
`route_cables_between_racks`, `generate_cable_bundle`, `add_patch_panel_connections`,
`export_cable_data_json`, `validate_cable_routing`, `clear_cables`

---

## variation_tools

11 tools for procedural variation, wear, and failure state simulation.

`apply_wear_variation`, `apply_dust_overlay`, `randomize_color_tint`,
`apply_damage_state`, `set_failure_state`, `generate_failure_preset`,
`propagate_failure`, `reset_variation`, `randomize_bay_variation`,
`apply_theme`, `get_variation_report`

---

## facility_tools

11 tools for full facility section layout and export.

`create_facility_section`, `create_corridor`, `add_power_cooling_zone`,
`create_multi_bay_row`, `populate_facility_from_json`, `apply_facility_theme`,
`randomize_facility_variation`, `get_section_bays`, `export_facility_layout_json`,
`validate_facility`, `get_facility_info`

---

## polish_tools

12 tools for session awareness, safety, undo, and documentation generation.
*This module is documented in full below — the others require `export_tool_reference` for complete parameter tables.*

---

### `get_scene_inventory`

Walk the entire Blender scene and return a structured tally of everything the BlendDC pipeline has created.

Call this at the start of every session to orient yourself before issuing further commands.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| *(none)* | | | | |

**Returns:** `sections`, `bays`, `racks`, `equipment_objects`, `cable_curves`, `variation_objects`, `orphaned_meshes`, `orphaned_materials`, `session_age_s`, `session_log_entries`, `detail`

---

### `log_operation`

Append one structured entry to the in-session audit log.

Persists in memory for the Blender session lifetime; readable via `get_session_log`. Optionally appends to a file on disk in NDJSON format.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `tool_name` | `string` | — | ✓ | Name of the tool that ran |
| `duration_s` | `number` | `0.0` | | Execution time in seconds |
| `result_summary` | `string` | `""` | | One-line outcome description |
| `status` | `string` | `"ok"` | | `"ok"` \| `"warn"` \| `"fail"` |
| `log_file` | `string` | `""` | | Absolute path; appends NDJSON if provided |

---

### `get_session_log`

Return entries from the in-session audit log with optional filtering and ASCII table formatting.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `last_n` | `integer` | `20` | | Maximum entries to return (0 = all) |
| `filter_status` | `string` | `""` | | `""` = all; `"fail"` = failures; `"warn"` = warns+fails |
| `as_table` | `boolean` | `true` | | Include formatted ASCII table in `table` key |

**Returns:** `entries`, `table`, `summary` `{total, ok_count, warn_count, fail_count, total_duration_s, session_age_s}`

---

### `push_undo_checkpoint`

Push a named undo step onto Blender's undo stack.

**Call this before any destructive tool.** Ctrl+Z in Blender restores the scene to exactly this point. The checkpoint is also recorded in the session log.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `label` | `string` | `"BlendDC checkpoint"` | | Displayed in Blender's Edit → Undo History |

---

### `confirm_destructive`

Pre-flight check and optional executor for a named destructive operation.

Default (`execute=False`): counts what would be affected, returns impact summary — no changes made. When `execute=True`: pushes an undo checkpoint automatically, then runs the operation.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `operation` | `string` | — | ✓ | `"clear_cables"` \| `"reset_variation"` \| `"reset_all_leds"` \| `"delete_bay"` \| `"clear_orphaned_data"` |
| `collection_name` | `string` | `""` | | Scope to this collection (empty = scene-wide) |
| `execute` | `boolean` | `false` | | `false` = dry-run; `true` = run with auto checkpoint |

---

### `backup_section_metadata`

Serialise all custom properties from a facility section's collection hierarchy to a JSON snapshot file.

Captures metadata only (rack U-heights, bay positions, equipment types, LED states, variation flags, cable routing properties) — not geometry. Use `restore_section_metadata` to re-apply.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `section_name` | `string` | — | ✓ | Facility section name (with or without `Facility_` prefix) |
| `output_path` | `string` | — | ✓ | Absolute path for the JSON output file |
| `include_variation` | `boolean` | `true` | | Record variation state flags |
| `include_cables` | `boolean` | `true` | | Record cable routing properties |

**Returns:** `output_path`, `collections_saved`, `properties_saved`, `file_size_kb`

---

### `restore_section_metadata`

Re-apply collection custom properties from a backup JSON file created by `backup_section_metadata`.

Defaults to `dry_run=True` — reports what would change without modifying anything.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `section_name` | `string` | — | ✓ | Must match `section_name` stored in the backup |
| `backup_path` | `string` | — | ✓ | Path to the backup JSON file |
| `dry_run` | `boolean` | `true` | | `true` = report only; `false` = apply changes |

**Returns:** `dry_run`, `collections_restored`, `properties_unchanged`, `mismatches`, `message`

---

### `quick_save_scene`

Save a timestamped copy of the current `.blend` file to disk.

Uses `copy=True` — the working file path is **not** changed. Subsequent Ctrl+S still saves to the original file.

Filename format: `{original_stem}_{YYYYMMDD_HHMMSS}[_{label}].blend`

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `label` | `string` | `""` | | Optional suffix added to filename (spaces → underscores) |
| `output_dir` | `string` | `""` | | Override save directory (created if missing); defaults to current file's directory or Desktop |

**Returns:** `saved_path`, `size_mb`, `duration_s`, `label`, `note`

---

### `validate_entire_scene`

One-call health check across every BlendDC structure in the current scene.

Runs `validate_facility` per `Facility_` collection, `validate_cable_routing` scene-wide, and checks for rack cabinets outside any bay hierarchy.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `stop_on_first_error` | `boolean` | `false` | | Abort after the first section with failures |
| `include_orphan_racks` | `boolean` | `true` | | Also check racks outside any bay |

**Returns:** `overall_status`, `fail_count`, `warn_count`, `sections_checked`, `cable_validation`, `orphan_racks`, `recommendations`

---

### `suggest_next_step`

Inspect the current scene and return a prioritised, actionable list of recommended next tool calls — with pre-filled `suggested_args` ready to copy into a tool call.

Makes no modifications. Safe to call at any time.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `collection_name` | `string` | `""` | | Scope to a section or bay (empty = full scene) |
| `context_hint` | `string` | `""` | | `"fresh_session"` \| `"about_to_export"` \| `"variation_pass"` |

**Returns:** `collection_name`, `context_hint`, `suggestions` (list of up to 8 `{priority, reason, tool_name, suggested_args, estimated_impact}`)

---

### `list_all_tools`

Return all registered MCP tools grouped by source module with one-line descriptions.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `module_filter` | `string` | `""` | | Limit to one module (e.g. `"cable_tools"`) |
| `keyword` | `string` | `""` | | Case-insensitive search across names + descriptions |
| `include_params` | `boolean` | `false` | | Include parameter names per tool |

**Returns:** `total_count`, `module_count`, `filters`, `modules`

---

### `export_tool_reference`

Write a complete tool reference document to disk, generated from live docstrings and parameter schemas. Always accurate — derived from what's actually running.

| Parameter | Type | Default | Req | Description |
|---|---|---|:---:|---|
| `output_path` | `string` | — | ✓ | Absolute path (`.md` or `.json`) |
| `format` | `string` | `"markdown"` | | `"markdown"` \| `"json"` |
| `include_params` | `boolean` | `true` | | Include full parameter table per tool |
| `module_filter` | `string` | `""` | | Limit to one module (empty = all) |

**Returns:** `output_path`, `format`, `tools_documented`, `sections`, `file_size_kb`

---

*To regenerate this document with full parameter tables for all 189 tools, run `export_tool_reference` with `output_path` pointing here.*
