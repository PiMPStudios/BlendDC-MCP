"""
Polish, UX, safety, and documentation tools for the UPTIME datacenter simulator.

Phase 9 (v3.0.0) — Production readiness layer built on top of the 186-tool core.

  Session Awareness:
    get_scene_inventory      — structured tally: sections / bays / racks / cables / variation
    log_operation            — append one timestamped entry to the in-session audit log
    get_session_log          — read back the session log with filtering and table formatting

  Safety & Undo:
    push_undo_checkpoint     — push a named Blender undo step (Ctrl+Z restore point)
    confirm_destructive      — dry-run pre-flight for named destructive operations;
                               execute=True runs it with an automatic undo checkpoint first
    backup_section_metadata  — serialise collection custom properties to a JSON snapshot
    restore_section_metadata — re-apply custom properties from a metadata backup
    quick_save_scene         — timestamped .blend copy to disk (copy=True; working file unchanged)

  Quality of Life:
    validate_entire_scene    — one-call health check across all facility sections + cables
    suggest_next_step        — scene-aware prioritised action recommendations with pre-filled args
    list_all_tools           — all 189 tools grouped by module with one-line descriptions
    export_tool_reference    — generate Markdown or JSON tool reference document from live docstrings

Design rules:
  • All destructive paths default to dry_run=True / execute=False.
  • push_undo_checkpoint is inserted automatically on any confirm_destructive execute path.
  • @thread_safe is applied to every tool that touches the Blender API or asyncio.run().
  • The session log (_SESSION_LOG) persists in module memory for the Blender session lifetime;
    it is cleared only when the server module is reloaded.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import bpy

from core import mcp, thread_safe, _log


# ── Module-level session log ───────────────────────────────────────────────
# Plain Python list — no Blender dependency, persists across all tool calls
# for the lifetime of the Blender session.

_SESSION_LOG: List[Dict[str, Any]] = []
_SESSION_START: float = time.time()

# Module display order used by list_all_tools and export_tool_reference
_MODULE_DISPLAY_ORDER: List[str] = [
    "server",
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
]


# ══════════════════════════════════════════════════════════════════════════
#  PRIVATE HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _objects_in(col: bpy.types.Collection) -> List[bpy.types.Object]:
    """Recursively collect every object in a collection and its children."""
    result: List[bpy.types.Object] = list(col.objects)
    for child in col.children:
        result.extend(_objects_in(child))
    return result


def _idprop_to_python(val: Any) -> Any:
    """Convert a Blender IDProperty value to a JSON-serialisable Python type."""
    if isinstance(val, (bool, int, float, str)):
        return val
    if hasattr(val, "to_list"):          # IDPropertyArray
        return val.to_list()
    if hasattr(val, "items"):            # IDPropertyGroup
        return {k: _idprop_to_python(v) for k, v in val.items()}
    try:
        return list(val)
    except (TypeError, ValueError):
        return str(val)


def _serialize_collection_tree(
    col: bpy.types.Collection,
    visited: Optional[set] = None,
) -> Dict[str, Any]:
    """
    Recursively serialise a collection's custom properties to a plain dict tree.
    Cycles (should never happen in a valid .blend) are detected via a visited set.
    """
    if visited is None:
        visited = set()
    if col.name in visited:
        return {"name": col.name, "cycle_detected": True, "custom_properties": {}, "children": []}
    visited.add(col.name)

    props: Dict[str, Any] = {}
    for key in col.keys():
        if key.startswith("_"):   # skip internal RNA meta-keys
            continue
        try:
            props[key] = _idprop_to_python(col[key])
        except Exception:
            props[key] = str(col.get(key))

    return {
        "name": col.name,
        "custom_properties": props,
        "children": [
            _serialize_collection_tree(child, visited)
            for child in col.children
        ],
    }


def _restore_collection_tree(
    col: bpy.types.Collection,
    saved: Dict[str, Any],
    dry_run: bool,
) -> Tuple[int, int, List[str]]:
    """
    Re-apply custom properties from a saved tree node onto a live collection.
    Returns (collections_touched, properties_unchanged, mismatch_messages).
    """
    restored = 0
    skipped  = 0
    mismatches: List[str] = []

    if col.name != saved.get("name"):
        mismatches.append(
            f"Name mismatch — scene: '{col.name}', backup: '{saved.get('name')}'"
        )

    for key, val in saved.get("custom_properties", {}).items():
        current = col.get(key)
        if current == val:
            skipped += 1
            continue
        if not dry_run:
            try:
                col[key] = val
            except (TypeError, ValueError) as exc:
                mismatches.append(f"  {col.name}[{key!r}]: {exc}")
                continue
        restored += 1

    for saved_child in saved.get("children", []):
        child_name  = saved_child.get("name", "")
        scene_child = bpy.data.collections.get(child_name)
        if scene_child and scene_child.name in {c.name for c in col.children}:
            cr, cs, cm = _restore_collection_tree(scene_child, saved_child, dry_run)
            restored  += cr
            skipped   += cs
            mismatches.extend(cm)
        else:
            mismatches.append(f"Child collection '{child_name}' not found under '{col.name}'")

    return restored, skipped, mismatches


def _count_tree_collections(tree: Dict[str, Any]) -> int:
    return 1 + sum(_count_tree_collections(c) for c in tree.get("children", []))


def _count_tree_properties(tree: Dict[str, Any]) -> int:
    return (
        len(tree.get("custom_properties", {}))
        + sum(_count_tree_properties(c) for c in tree.get("children", []))
    )


def _count_cable_objects(col_name: str = "") -> int:
    if col_name:
        col  = bpy.data.collections.get(col_name)
        objs = _objects_in(col) if col else []
    else:
        objs = list(bpy.data.objects)
    return sum(1 for o in objs if o.get("cable_path"))


def _count_variation_objects(col_name: str = "") -> int:
    if col_name:
        col  = bpy.data.collections.get(col_name)
        objs = _objects_in(col) if col else []
    else:
        objs = list(bpy.data.objects)
    _LABELS = ("[WEAR]", "[DUST]", "[DAMAGE]")
    count = 0
    for obj in objs:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat and mat.node_tree:
                if any(n.label.startswith(_LABELS) for n in mat.node_tree.nodes):
                    count += 1
                    break
    return count


def _count_led_objects(col_name: str = "") -> int:
    if col_name:
        col  = bpy.data.collections.get(col_name)
        objs = _objects_in(col) if col else []
    else:
        objs = list(bpy.data.objects)
    return sum(1 for o in objs if o.get("led_state"))


def _count_collection_objects(col_name: str) -> int:
    col = bpy.data.collections.get(col_name)
    return len(_objects_in(col)) if col else 0


def _do_clear_orphaned(
    include_meshes:    bool = True,
    include_materials: bool = True,
    include_curves:    bool = True,
) -> Dict[str, int]:
    """Remove zero-user datablocks.  Must run on Blender's main thread."""
    counts: Dict[str, int] = {"meshes": 0, "materials": 0, "curves": 0}
    if include_meshes:
        for mesh in list(bpy.data.meshes):
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
                counts["meshes"] += 1
    if include_materials:
        for mat in list(bpy.data.materials):
            if mat.users == 0:
                bpy.data.materials.remove(mat)
                counts["materials"] += 1
    if include_curves:
        for curve in list(bpy.data.curves):
            if curve.users == 0:
                bpy.data.curves.remove(curve)
                counts["curves"] += 1
    return counts


def _build_module_map() -> Dict[str, str]:
    """
    Build a map of registered_tool_name → source_module_name.

    Tries two approaches in order:
      1. FastMCP internal registry — most accurate, uses function.__module__
      2. sys.modules scan — reliable fallback, first module that defines the name wins
    """
    # Attempt 1: FastMCP internals
    try:
        tools_dict: Optional[Dict] = None
        for attr in ("_tool_manager", "_tools", "tool_manager"):
            candidate = getattr(mcp, attr, None)
            if candidate is not None:
                inner = getattr(candidate, "_tools", candidate)
                if isinstance(inner, dict):
                    tools_dict = inner
                    break
        if tools_dict:
            result: Dict[str, str] = {}
            for name, tool_obj in tools_dict.items():
                fn = getattr(tool_obj, "fn", None)
                if fn:
                    mod = getattr(fn, "__module__", None)
                    if mod:
                        result[name] = mod
            if result:
                return result
    except Exception:
        pass

    # Attempt 2: sys.modules scan — first module in display order that has the name
    result = {}
    for mod_name in _MODULE_DISPLAY_ORDER:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for attr_name in vars(mod):
            if not attr_name.startswith("_"):
                result.setdefault(attr_name, mod_name)
    return result


def _schema_type_str(prop: Dict[str, Any]) -> str:
    """Return a human-readable type string for a JSON Schema property."""
    if "anyOf" in prop:
        parts = [t.get("type", "") for t in prop["anyOf"] if t.get("type") != "null"]
        return " | ".join(p for p in parts if p) or "any"
    return prop.get("type", "any")


def _mcp_list_tools_sync() -> List[Any]:
    """
    Call mcp.list_tools() synchronously.  Works from Blender's main thread
    (no running asyncio loop) via asyncio.run(), with fallback for edge cases.
    """
    try:
        return asyncio.run(mcp.list_tools())
    except RuntimeError:
        # Rare: an event loop already exists in this thread context
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(mcp.list_tools())
            finally:
                loop.close()
        except Exception:
            return []


def _build_markdown_reference(
    groups: Dict[str, List[Any]],
    include_params: bool,
) -> str:
    now   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    total = sum(len(v) for v in groups.values())
    lines: List[str] = []

    lines += [
        "# Universal Blender MCP — Tool Reference",
        "",
        f"> Auto-generated {now}  ·  **{total} tools** across "
        f"**{len(groups)} modules**  ·  v3.0.0",
        "",
        "## Module Index",
        "",
    ]
    for mod_name in _MODULE_DISPLAY_ORDER:
        if mod_name in groups:
            anchor = mod_name.replace("_", "-")
            lines.append(
                f"- [{mod_name}](#{anchor})  ({len(groups[mod_name])} tools)"
            )
    lines.append("")

    for mod_name in _MODULE_DISPLAY_ORDER:
        tool_list = groups.get(mod_name)
        if not tool_list:
            continue

        lines += [f"---", f"", f"## {mod_name}  ({len(tool_list)} tools)", ""]

        for tool in tool_list:
            lines.append(f"### `{tool.name}`")
            if tool.description:
                paras = [p.strip() for p in tool.description.split("\n\n") if p.strip()]
                lines.append(paras[0])
                if len(paras) > 1:
                    lines += ["", paras[1]]
            lines.append("")

            if include_params:
                schema   = tool.inputSchema or {}
                props    = schema.get("properties", {})
                required = set(schema.get("required", []))
                if props:
                    lines += [
                        "| Parameter | Type | Default | Req | Description |",
                        "|---|---|---|:---:|---|",
                    ]
                    for pname, pschema in props.items():
                        ptype   = _schema_type_str(pschema)
                        default = pschema.get("default", "—")
                        desc    = (pschema.get("description") or "").replace("|", "\\|")
                        req     = "✓" if pname in required else ""
                        lines.append(
                            f"| `{pname}` | `{ptype}` | `{default}` | {req} | {desc} |"
                        )
                    lines.append("")
                else:
                    lines += ["*No parameters.*", ""]

    return "\n".join(lines)


def _build_json_reference(
    groups: Dict[str, List[Any]],
    include_params: bool,
) -> Dict[str, Any]:
    total   = sum(len(v) for v in groups.values())
    modules = []
    for mod_name in _MODULE_DISPLAY_ORDER:
        tool_list = groups.get(mod_name)
        if not tool_list:
            continue
        tools_out = []
        for tool in tool_list:
            entry: Dict[str, Any] = {
                "name":        tool.name,
                "description": tool.description or "",
            }
            if include_params:
                schema   = tool.inputSchema or {}
                props    = schema.get("properties", {})
                required = set(schema.get("required", []))
                entry["parameters"] = [
                    {
                        "name":        pname,
                        "type":        _schema_type_str(pschema),
                        "default":     pschema.get("default"),
                        "required":    pname in required,
                        "description": pschema.get("description", ""),
                    }
                    for pname, pschema in props.items()
                ]
            tools_out.append(entry)
        modules.append({
            "name":       mod_name,
            "tool_count": len(tool_list),
            "tools":      tools_out,
        })
    return {
        "version":      "3.0.0",
        "generated_at": datetime.datetime.now().isoformat(),
        "total_tools":  total,
        "modules":      modules,
    }


def _log_entry(
    tool_name: str,
    duration_s: float,
    status: str,
    result_summary: str,
) -> None:
    """Append one entry to _SESSION_LOG (shared by all tools that want to self-report)."""
    _SESSION_LOG.append({
        "timestamp":      datetime.datetime.now().isoformat(timespec="seconds"),
        "tool_name":      tool_name,
        "duration_s":     round(float(duration_s), 2),
        "status":         status,
        "result_summary": result_summary,
    })


# ══════════════════════════════════════════════════════════════════════════
#  CATEGORY 1 — SESSION AWARENESS
# ══════════════════════════════════════════════════════════════════════════

@mcp.tool()
@thread_safe
def get_scene_inventory() -> Dict[str, Any]:
    """
    Walk the entire Blender scene and return a structured tally of everything
    the UPTIME pipeline has created.

    Call this at the start of every session to orient yourself before issuing
    further commands.  The 'detail' list gives a per-section breakdown.

    Returns:
      sections          int  — Facility_ collections
      bays              int  — Bay_ collections
      racks             int  — rack cabinet collections
      equipment_objects int  — objects with an equipment_type custom property
      cable_curves      int  — objects with a cable_path custom property
      variation_objects int  — mesh objects that have [WEAR]/[DUST]/[DAMAGE] nodes
      orphaned_meshes   int  — zero-user mesh datablocks (cleanup candidates)
      orphaned_materials int — zero-user material datablocks
      session_age_s     int  — seconds since the MCP server was last started
      detail            list — per-section {section, bays, racks} summary
    """
    sections_cols = [c for c in bpy.data.collections if c.get("is_facility_section")]
    bays_cols     = [c for c in bpy.data.collections if c.get("is_bay")]
    racks_cols    = [c for c in bpy.data.collections if c.get("is_rack_cabinet")]
    equipment_objs = [o for o in bpy.data.objects if o.get("equipment_type")]
    cable_objs     = [o for o in bpy.data.objects if o.get("cable_path")]
    variation_n    = _count_variation_objects()
    orphan_meshes  = sum(1 for m in bpy.data.meshes     if m.users == 0)
    orphan_mats    = sum(1 for m in bpy.data.materials  if m.users == 0)

    detail = []
    for sec in sections_cols:
        bay_names = [b for b in sec.get("section_bay_names_csv", "").split(",") if b]
        # Count racks that actually exist in this section's bays
        rack_count = 0
        for bay_name in bay_names:
            bay_col = bpy.data.collections.get(bay_name)
            if bay_col:
                rack_count += sum(
                    1 for c in bpy.data.collections
                    if c.get("is_rack_cabinet") and
                    any(c.name == gc.name for gc in bay_col.children_recursive)
                )
        detail.append({
            "section":    sec.name,
            "bays":       len(bay_names),
            "racks":      rack_count,
            "has_cables": any(
                o.get("cable_path")
                for o in _objects_in(sec)
            ),
        })

    return {
        "sections":           len(sections_cols),
        "bays":               len(bays_cols),
        "racks":              len(racks_cols),
        "equipment_objects":  len(equipment_objs),
        "cable_curves":       len(cable_objs),
        "variation_objects":  variation_n,
        "orphaned_meshes":    orphan_meshes,
        "orphaned_materials": orphan_mats,
        "session_age_s":      int(time.time() - _SESSION_START),
        "session_log_entries": len(_SESSION_LOG),
        "detail":             detail,
    }


@mcp.tool()
def log_operation(
    tool_name:      str,
    duration_s:     float = 0.0,
    result_summary: str   = "",
    status:         str   = "ok",
    log_file:       str   = "",
) -> Dict[str, Any]:
    """
    Append one structured entry to the in-session audit log.

    The log persists in memory for the lifetime of the Blender session and
    is readable via get_session_log.  Optionally also appends to a file on
    disk in NDJSON format (one JSON object per line).

    Intended use: call this after any significant tool invocation to build an
    audit trail you can review if something goes wrong.

    tool_name:      name of the tool that ran
    duration_s:     how long it took in seconds (0 if unknown)
    result_summary: one-line description of the outcome
    status:         "ok" | "warn" | "fail"
    log_file:       optional absolute path; appends to file if provided
    """
    valid_statuses = {"ok", "warn", "fail"}
    if status not in valid_statuses:
        status = "ok"

    entry: Dict[str, Any] = {
        "timestamp":      datetime.datetime.now().isoformat(timespec="seconds"),
        "tool_name":      tool_name,
        "duration_s":     round(float(duration_s), 2),
        "status":         status,
        "result_summary": result_summary,
    }
    _SESSION_LOG.append(entry)

    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    return {
        "logged":      True,
        "entry_count": len(_SESSION_LOG),
        "entry":       entry,
    }


@mcp.tool()
def get_session_log(
    last_n:        int  = 20,
    filter_status: str  = "",
    as_table:      bool = True,
) -> Dict[str, Any]:
    """
    Return entries from the in-session audit log written by log_operation
    (and by push_undo_checkpoint, confirm_destructive, and quick_save_scene,
    which self-report automatically).

    last_n:        maximum number of entries to return (0 = all)
    filter_status: "" = all entries; "fail" = failures only; "warn" = warns + fails
    as_table:      True = include a formatted ASCII table in the 'table' key

    Returns:
      entries  list[dict]  — filtered log entries
      table    str | null  — aligned text table (if as_table=True)
      summary  dict        — {total, ok_count, warn_count, fail_count, total_duration_s,
                              session_age_s}
    """
    all_entries = list(_SESSION_LOG)

    # Filter
    if filter_status == "fail":
        filtered = [e for e in all_entries if e["status"] == "fail"]
    elif filter_status == "warn":
        filtered = [e for e in all_entries if e["status"] in ("warn", "fail")]
    else:
        filtered = all_entries

    # Limit to last N
    page = filtered[-last_n:] if last_n > 0 else filtered

    # Summary over the full log (not the filtered page)
    ok_n   = sum(1 for e in all_entries if e["status"] == "ok")
    warn_n = sum(1 for e in all_entries if e["status"] == "warn")
    fail_n = sum(1 for e in all_entries if e["status"] == "fail")
    total_dur = sum(e.get("duration_s", 0.0) for e in all_entries)

    # ASCII table
    table_str = None
    if as_table and page:
        col_w = 30
        header = (
            f"{'Timestamp':<20}  {'St':<4}  {'Dur':>6}  {'Tool':<{col_w}}  Summary"
        )
        sep    = "-" * (20 + 4 + 6 + col_w + 12)
        rows   = [header, sep]
        _STATUS_ICON = {"ok": "✓", "warn": "⚠", "fail": "✗"}
        for e in page:
            icon = _STATUS_ICON.get(e["status"], "?")
            dur  = f"{e.get('duration_s', 0.0):.1f}s"
            tool = e["tool_name"][:col_w]
            summ = e.get("result_summary", "")[:50]
            rows.append(f"{e['timestamp']:<20}  {icon:<4}  {dur:>6}  {tool:<{col_w}}  {summ}")
        table_str = "\n".join(rows)

    return {
        "entries": page,
        "table":   table_str,
        "summary": {
            "total":            len(all_entries),
            "ok_count":         ok_n,
            "warn_count":       warn_n,
            "fail_count":       fail_n,
            "total_duration_s": round(total_dur, 1),
            "session_age_s":    int(time.time() - _SESSION_START),
        },
    }


# ══════════════════════════════════════════════════════════════════════════
#  CATEGORY 2 — SAFETY & UNDO
# ══════════════════════════════════════════════════════════════════════════

@mcp.tool()
@thread_safe
def push_undo_checkpoint(
    label: str = "UPTIME checkpoint",
) -> Dict[str, Any]:
    """
    Push a named undo step onto Blender's undo stack.

    Call this BEFORE any destructive tool — apply_facility_theme, clear_cables,
    reset_variation, randomize_facility_variation, populate_* — so that Ctrl+Z
    in Blender restores the scene to exactly this point.

    The checkpoint is also recorded in the session log, so get_session_log
    shows when each checkpoint was pushed relative to other operations.

    label: human-readable name displayed in Blender's Undo History (Edit menu)
    """
    bpy.ops.ed.undo_push(message=label)
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    _log_entry("push_undo_checkpoint", 0.0, "ok", f"Checkpoint: {label}")

    return {
        "checkpoint":  label,
        "timestamp":   ts,
        "message":     (
            f"Undo checkpoint '{label}' pushed. "
            "Ctrl+Z in Blender will restore the scene to this state."
        ),
    }


@mcp.tool()
@thread_safe
def confirm_destructive(
    operation:       str,
    collection_name: str  = "",
    execute:         bool = False,
) -> Dict[str, Any]:
    """
    Pre-flight check and optional executor for a named destructive operation.

    Default mode (execute=False): counts exactly what would be affected and
    returns a human-readable impact summary without making any changes.

    When execute=True: pushes an undo checkpoint automatically, then runs the
    operation.  The checkpoint label is returned so you can verify it in
    Blender's Edit → Undo History.

    Supported operations:
      "clear_cables"        — removes all cable curve objects from a collection
      "reset_variation"     — removes all [WEAR]/[DUST]/[DAMAGE] shader nodes
      "reset_all_leds"      — not executable here; returns tool + args to call
      "delete_bay"          — not executable here; returns Outliner instruction
      "clear_orphaned_data" — purges zero-user meshes, materials, and curves

    operation:       one of the supported operation names (see above)
    collection_name: collection to scope the operation (empty = scene-wide)
    execute:         False = dry-run only; True = run with auto checkpoint first
    """
    _SUPPORTED: Dict[str, str] = {
        "clear_cables":        "Remove cable curve objects from a collection or scene",
        "reset_variation":     "Remove [WEAR]/[DUST]/[DAMAGE] shader nodes from materials",
        "reset_all_leds":      "Reset LED state to 'on' on all equipment objects",
        "delete_bay":          "Delete a bay collection and all contained objects",
        "clear_orphaned_data": "Purge zero-user meshes, materials, and curves",
    }

    if operation not in _SUPPORTED:
        return {
            "error":                f"Unknown operation '{operation}'.",
            "supported_operations": _SUPPORTED,
        }

    # ── Count affected objects ────────────────────────────────────────────
    if operation == "clear_cables":
        count = _count_cable_objects(collection_name)
        desc  = f"{count} cable curve object(s)"

    elif operation == "reset_variation":
        count = _count_variation_objects(collection_name)
        desc  = f"{count} object(s) with variation nodes"

    elif operation == "delete_bay":
        count = _count_collection_objects(collection_name)
        desc  = f"{count} object(s) in collection '{collection_name}'"

    elif operation == "reset_all_leds":
        count = _count_led_objects(collection_name)
        desc  = f"{count} object(s) with led_state custom property"

    elif operation == "clear_orphaned_data":
        om  = sum(1 for m in bpy.data.meshes     if m.users == 0)
        omt = sum(1 for m in bpy.data.materials  if m.users == 0)
        oc  = sum(1 for c in bpy.data.curves     if c.users == 0)
        count = om + omt + oc
        desc  = f"{om} mesh(es) + {omt} material(s) + {oc} curve(s) (all zero-user)"

    else:
        count = 0
        desc  = "unknown"

    scope_label = collection_name or "(scene-wide)"
    safe        = count <= 500
    impact_warn = (
        f"  WARNING: {count} objects is unusually large — double-check the collection name."
        if not safe else ""
    )
    confirmation_msg = (
        f"Operation '{operation}' on {scope_label} would affect {desc}.{impact_warn}"
    )

    # ── Dry-run path ──────────────────────────────────────────────────────
    if not execute:
        return {
            "operation":        operation,
            "collection_name":  scope_label,
            "would_affect":     count,
            "description":      desc,
            "safe_to_proceed":  safe,
            "confirmation_msg": confirmation_msg,
            "next_step": (
                f"Call confirm_destructive again with execute=True to proceed. "
                f"An undo checkpoint will be pushed automatically before execution."
            ),
        }

    # ── Execute path ──────────────────────────────────────────────────────
    undo_label = f"Before {operation} on {scope_label}"
    bpy.ops.ed.undo_push(message=undo_label)

    result: Any = None

    if operation == "clear_cables":
        _ct = sys.modules.get("cable_tools")
        if _ct is None:
            return {"error": "cable_tools module not loaded — restart the MCP server."}
        result = _ct.clear_cables(collection_name=collection_name, confirm=True)

    elif operation == "reset_variation":
        _vt = sys.modules.get("variation_tools")
        if _vt is None:
            return {"error": "variation_tools module not loaded — restart the MCP server."}
        result = _vt.reset_variation(target=collection_name)

    elif operation == "clear_orphaned_data":
        counts = _do_clear_orphaned()
        result = {
            "removed_meshes":    counts["meshes"],
            "removed_materials": counts["materials"],
            "removed_curves":    counts["curves"],
            "total_removed":     sum(counts.values()),
        }

    elif operation in ("delete_bay", "reset_all_leds"):
        return {
            "error": (
                f"execute=True is not supported for '{operation}' via confirm_destructive "
                "to avoid accidental large-scope deletions.  "
                "Please use the dedicated tool directly after calling push_undo_checkpoint."
            ),
            "suggested_flow": [
                "push_undo_checkpoint({'label': 'Before " + operation + "'})",
                {
                    "delete_bay":    "Use Blender's Outliner → right-click collection → Delete",
                    "reset_all_leds": "Call material_tools.set_led_state on each object",
                }.get(operation, ""),
            ],
        }

    _log_entry(
        f"confirm_destructive/{operation}",
        0.0, "ok",
        f"Executed on {scope_label}: affected {count}",
    )

    return {
        "operation":       operation,
        "undo_checkpoint": undo_label,
        "affected":        count,
        "result":          result,
    }


@mcp.tool()
@thread_safe
def backup_section_metadata(
    section_name:      str,
    output_path:       str,
    include_variation: bool = True,
    include_cables:    bool = True,
) -> Dict[str, Any]:
    """
    Serialise all custom properties from a facility section's collection
    hierarchy to a JSON snapshot file.

    This is NOT a geometry backup — it captures only the metadata that drives
    tool behaviour: rack U-heights, bay positions, equipment types, LED states,
    variation flags, cable routing properties, and all other custom properties
    set by UPTIME tools.

    Use restore_section_metadata to re-apply this data after a .blend reload,
    a merge conflict, or if metadata gets corrupted.

    Pair with quick_save_scene for a complete "before I do something risky"
    checkpoint: metadata backup + .blend copy covers both data layers.

    section_name:      facility section name (with or without 'Facility_' prefix)
    output_path:       absolute path for the output JSON file (created if missing)
    include_variation: if False, [WEAR]/[DUST] object properties are still serialised
                       but the flag is recorded so restore can skip them if desired
    include_cables:    similarly marks cable_path properties in the backup
    """
    col_name = (
        section_name
        if section_name.startswith("Facility_")
        else f"Facility_{section_name}"
    )
    col = bpy.data.collections.get(col_name) or bpy.data.collections.get(section_name)
    if col is None:
        raise ValueError(
            f"Collection '{col_name}' not found in scene. "
            f"Available Facility_ collections: "
            f"{[c.name for c in bpy.data.collections if c.get('is_facility_section')]}"
        )

    tree = _serialize_collection_tree(col)

    backup: Dict[str, Any] = {
        "backup_version":   "1.0",
        "addon_version":    "3.0.0",
        "created_at":       datetime.datetime.now().isoformat(),
        "section_name":     section_name,
        "collection_name":  col.name,
        "include_variation": include_variation,
        "include_cables":    include_cables,
        "collection_tree":   tree,
    }

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=2)

    size_kb      = Path(output_path).stat().st_size / 1024
    col_count    = _count_tree_collections(tree)
    prop_count   = _count_tree_properties(tree)

    _log_entry(
        "backup_section_metadata", 0.0, "ok",
        f"Saved {col_count} collections / {prop_count} properties → {Path(output_path).name}",
    )

    return {
        "output_path":       output_path,
        "collections_saved": col_count,
        "properties_saved":  prop_count,
        "file_size_kb":      round(size_kb, 1),
    }


@mcp.tool()
@thread_safe
def restore_section_metadata(
    section_name: str,
    backup_path:  str,
    dry_run:      bool = True,
) -> Dict[str, Any]:
    """
    Re-apply collection custom properties from a backup JSON file created by
    backup_section_metadata.

    Defaults to dry_run=True: reports exactly what would change without
    modifying anything.  Set dry_run=False to apply.

    Mismatches (collections present in the backup but not in the current scene,
    or name changes) are reported and do not abort the restore — they are listed
    in the 'mismatches' field so you can investigate and fix manually.

    section_name: must match the section_name stored in the backup
    backup_path:  path to the JSON backup file written by backup_section_metadata
    dry_run:      True = report only; False = apply changes
    """
    if not Path(backup_path).exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    with open(backup_path, encoding="utf-8") as f:
        backup = json.load(f)

    saved_section = backup.get("section_name", "")
    if saved_section != section_name:
        return {
            "error": (
                f"Backup is for section '{saved_section}', "
                f"not '{section_name}'. Pass section_name='{saved_section}' "
                f"to restore the correct section."
            ),
            "backup_section": saved_section,
        }

    col_name = backup["collection_name"]
    col      = bpy.data.collections.get(col_name)
    if col is None:
        return {
            "error":            f"Collection '{col_name}' not found in current scene.",
            "backup_section":   saved_section,
            "collection_name":  col_name,
        }

    restored, skipped, mismatches = _restore_collection_tree(
        col, backup["collection_tree"], dry_run
    )

    action = "Would restore" if dry_run else "Restored"
    _log_entry(
        "restore_section_metadata", 0.0,
        "warn" if mismatches else "ok",
        f"{action} {restored} collections, {len(mismatches)} mismatch(es)",
    )

    return {
        "dry_run":              dry_run,
        "collections_restored": restored,
        "properties_unchanged": skipped,
        "mismatches":           mismatches,
        "message": (
            f"Dry run: would touch {restored} collection(s); "
            f"{skipped} properties already match; "
            f"{len(mismatches)} mismatch(es) noted."
            if dry_run else
            f"Restored {restored} collection(s). "
            f"{len(mismatches)} mismatch(es) — see 'mismatches' list."
        ),
    }


@mcp.tool()
@thread_safe
def quick_save_scene(
    label:      str = "",
    output_dir: str = "",
) -> Dict[str, Any]:
    """
    Save a timestamped copy of the current .blend file to disk.

    This is the "I just built something cool, don't lose it" button.

    Uses Blender's save-as-copy mode (copy=True), which means:
      • A new file is written with a timestamped name
      • Your current working file path is NOT changed
      • Subsequent Ctrl+S still saves to your original working file

    File naming:   {original_stem}_{YYYYMMDD_HHMMSS}[_{label}].blend
                   e.g. uptime_dc_v2_20260411_143022_pre_variation_pass.blend

    Save directory priority:
      1. output_dir if provided
      2. Directory of the currently open .blend file
      3. ~/Desktop
      4. System temp directory (fallback)

    label:      optional suffix added to the filename (spaces → underscores)
    output_dir: override the save directory (created if it does not exist)
    """
    ts     = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label.strip().replace(' ', '_')}" if label.strip() else ""

    # Determine base name
    if bpy.data.filepath:
        stem = Path(bpy.data.filepath).stem
    else:
        stem = "uptime_session"

    filename = f"{stem}_{ts}{suffix}.blend"

    # Determine save directory
    if output_dir:
        save_dir = Path(output_dir)
    elif bpy.data.filepath:
        save_dir = Path(bpy.data.filepath).parent
    else:
        desktop = Path.home() / "Desktop"
        save_dir = desktop if desktop.exists() else Path(tempfile.gettempdir())

    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / filename

    t0 = time.perf_counter()
    bpy.ops.wm.save_as_mainfile(
        filepath=str(save_path),
        copy=True,
        check_existing=False,
        compress=False,
    )
    elapsed = time.perf_counter() - t0

    size_mb = save_path.stat().st_size / (1024 * 1024) if save_path.exists() else 0.0

    _log_entry(
        "quick_save_scene", elapsed, "ok",
        f"Copy saved → {save_path.name}  ({size_mb:.1f} MB)",
    )

    return {
        "saved_path":   str(save_path),
        "size_mb":      round(size_mb, 2),
        "duration_s":   round(elapsed, 2),
        "label":        label or "(no label)",
        "note": (
            "COPY saved — your working file is unchanged.  "
            "Use Ctrl+S in Blender to save the working file normally."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════
#  CATEGORY 3 — QUALITY OF LIFE
# ══════════════════════════════════════════════════════════════════════════

@mcp.tool()
@thread_safe
def validate_entire_scene(
    stop_on_first_error: bool = False,
    include_orphan_racks: bool = True,
) -> Dict[str, Any]:
    """
    One-call health check across every UPTIME structure in the current scene.

    Runs three validators in sequence:
      1. facility_tools.validate_facility   — per Facility_ collection
      2. cable_tools.validate_cable_routing — scene-wide cable check
      3. bay_tools.validate_bay             — any Bay_ collections not inside a section

    All results are aggregated into a single pass/warn/fail report.  Call this
    before any export session to confirm the scene is UE5-ready.

    stop_on_first_error:  abort after the first section with failures (faster)
    include_orphan_racks: also check rack cabinets that exist outside any bay

    Returns:
      overall_status   "pass" | "warn" | "fail"
      fail_count       int
      warn_count       int
      sections_checked list[{section, fail_count, warn_count, status}]
      cable_validation {errors: int, warnings: int}
      orphan_racks     list[str] — rack names outside any bay/section hierarchy
      recommendations  list[str] — up to 5 actionable next-step suggestions
    """
    _ft = sys.modules.get("facility_tools")
    _ct = sys.modules.get("cable_tools")
    _bt = sys.modules.get("bay_tools")

    total_fails  = 0
    total_warns  = 0
    sections_checked: List[Dict[str, Any]] = []

    # ── 1. Validate every Facility_ section ──────────────────────────────
    for col in bpy.data.collections:
        if not col.get("is_facility_section"):
            continue
        if _ft is None:
            sections_checked.append({
                "section":    col.name,
                "fail_count": 0,
                "warn_count": 1,
                "status":     "warn",
                "note":       "facility_tools not loaded",
            })
            total_warns += 1
            continue
        try:
            result     = _ft.validate_facility(col.name)
            fail_count = result.get("fail_count", 0) if isinstance(result, dict) else 0
            warn_count = result.get("warn_count", 0) if isinstance(result, dict) else 0
            total_fails += fail_count
            total_warns += warn_count
            sections_checked.append({
                "section":    col.name,
                "fail_count": fail_count,
                "warn_count": warn_count,
                "status":     "fail" if fail_count > 0 else ("warn" if warn_count > 0 else "pass"),
            })
            if stop_on_first_error and fail_count > 0:
                break
        except Exception as exc:
            total_fails += 1
            sections_checked.append({
                "section":    col.name,
                "fail_count": 1,
                "warn_count": 0,
                "status":     "fail",
                "error":      str(exc),
            })

    # ── 2. Scene-wide cable validation ────────────────────────────────────
    cable_errors   = 0
    cable_warnings = 0
    if _ct is not None:
        try:
            cr = _ct.validate_cable_routing(collection_name="")
            if isinstance(cr, dict):
                cable_errors   = len(cr.get("errors",   []))
                cable_warnings = len(cr.get("warnings", []))
                total_fails   += cable_errors
                total_warns   += cable_warnings
        except Exception:
            pass

    # ── 3. Orphaned racks outside any bay ────────────────────────────────
    orphan_racks: List[str] = []
    if include_orphan_racks:
        all_bay_children: set = set()
        for bay_col in (c for c in bpy.data.collections if c.get("is_bay")):
            all_bay_children.update(c.name for c in bay_col.children_recursive)
        for col in bpy.data.collections:
            if col.get("is_rack_cabinet") and col.name not in all_bay_children:
                orphan_racks.append(col.name)

    # ── Recommendations ───────────────────────────────────────────────────
    recs: List[str] = []
    if orphan_racks:
        recs.append(
            f"{len(orphan_racks)} orphaned rack(s) outside any bay — "
            "move them into a bay collection or delete unused geometry."
        )
    if cable_errors > 0:
        recs.append(
            f"{cable_errors} cable routing error(s) — loose endpoints or "
            "over-length cables. Run validate_cable_routing for details."
        )
    if total_fails == 0 and total_warns == 0:
        recs.append("Scene is clean and export-ready. Proceed with export_facility_layout_json.")
    elif total_fails == 0:
        recs.append(
            f"{total_warns} warning(s). Review before final export — warnings "
            "often indicate missing metadata that degrades UE5 import quality."
        )
    else:
        recs.append(
            f"Fix {total_fails} error(s) before running export_facility_layout_json. "
            "Errors indicate geometry or metadata issues that will break UE5 import."
        )
    if not sections_checked:
        recs.append(
            "No Facility_ sections found. "
            "Create one with create_facility_section before validating."
        )

    overall = "fail" if total_fails > 0 else ("warn" if total_warns > 0 else "pass")
    _log_entry(
        "validate_entire_scene", 0.0,
        overall,
        f"{len(sections_checked)} section(s): {total_fails} fails, {total_warns} warns",
    )

    return {
        "overall_status":  overall,
        "fail_count":      total_fails,
        "warn_count":      total_warns,
        "sections_checked": sections_checked,
        "cable_validation": {
            "errors":   cable_errors,
            "warnings": cable_warnings,
        },
        "orphan_racks":    orphan_racks,
        "recommendations": recs[:5],
    }


@mcp.tool()
@thread_safe
def suggest_next_step(
    collection_name: str = "",
    context_hint:    str = "",
) -> Dict[str, Any]:
    """
    Inspect the current scene (or a specific collection) and return a
    prioritised, actionable list of recommended next tool calls.

    Each suggestion includes the exact tool name and pre-filled suggested_args
    that you can copy directly into a tool call.

    This tool makes no modifications.  It is safe to call at any time.

    collection_name: scope to a specific section or bay (empty = full scene)
    context_hint:    nudges priority ordering for a specific workflow stage:
                     "fresh_session"  — lead with get_scene_inventory
                     "about_to_export"— lead with validate_entire_scene + clear_orphaned_data
                     "variation_pass" — lead with push_undo_checkpoint

    Returns:
      suggestions  list[{priority, reason, tool_name, suggested_args,
                          estimated_impact}]
    """
    suggestions: List[Dict[str, Any]] = []

    def _add(reason: str, tool_name: str, args: dict, impact: str) -> None:
        suggestions.append({
            "priority":          len(suggestions) + 1,
            "reason":            reason,
            "tool_name":         tool_name,
            "suggested_args":    args,
            "estimated_impact":  impact,
        })

    # ── Check 1: No undo checkpoint since last destructive op ─────────────
    _DESTRUCTIVE_PREFIXES = (
        "apply_", "randomize_", "clear_", "reset_", "populate_",
        "create_facility", "export_",
    )
    _SAFE_TOOLS = {
        "validate_entire_scene", "get_scene_inventory", "get_session_log",
        "push_undo_checkpoint", "get_variation_report", "get_facility_info",
        "get_bay_info", "suggest_next_step", "list_all_tools",
        "get_section_bays", "validate_bay", "validate_facility",
        "validate_cable_routing", "export_cable_data_json",
    }
    checkpoint_idxs  = [i for i, e in enumerate(_SESSION_LOG)
                        if e["tool_name"] == "push_undo_checkpoint"]
    destructive_idxs = [i for i, e in enumerate(_SESSION_LOG)
                        if e["tool_name"] not in _SAFE_TOOLS
                        and any(e["tool_name"].startswith(p) for p in _DESTRUCTIVE_PREFIXES)]
    last_cp  = max(checkpoint_idxs,  default=-1)
    last_dst = max(destructive_idxs, default=-1)

    if last_dst > last_cp:
        _add(
            "Destructive operations detected since last undo checkpoint — protect your work",
            "push_undo_checkpoint",
            {"label": "Before next operation"},
            "Creates a Ctrl+Z restore point immediately (no scene changes)",
        )

    # ── Check 2: Unvalidated scene ────────────────────────────────────────
    sections = [c for c in bpy.data.collections if c.get("is_facility_section")]
    validate_idxs = [i for i, e in enumerate(_SESSION_LOG)
                     if e["tool_name"] == "validate_entire_scene"]
    modify_idxs   = [i for i, e in enumerate(_SESSION_LOG)
                     if e["tool_name"] not in _SAFE_TOOLS]
    last_val = max(validate_idxs, default=-1)
    last_mod = max(modify_idxs,   default=-1)

    if sections and last_mod > last_val:
        _add(
            f"{len(sections)} section(s) modified since last validation",
            "validate_entire_scene",
            {},
            f"Read-only check across {len(sections)} section(s) and all cables",
        )

    # ── Check 3: Empty bays ────────────────────────────────────────────────
    bays_all = [c for c in bpy.data.collections if c.get("is_bay")]
    if collection_name:
        target_col = bpy.data.collections.get(collection_name)
        if target_col:
            bays_all = [c for c in bays_all
                        if c.name in {gc.name for gc in target_col.children_recursive}]

    empty_bays: List[str] = []
    for bay in bays_all[:40]:   # cap scan to avoid stalling large scenes
        if not any(o.get("equipment_type") for o in _objects_in(bay)):
            empty_bays.append(bay.name)

    if empty_bays:
        _add(
            f"{len(empty_bays)} bay(s) contain no equipment objects",
            "create_bay_preset",
            {
                "bay_name":     empty_bays[0],
                "preset":       "standard_3tier",
                "random_variation": True,
            },
            f"Populates each bay with racks + equipment "
            f"(call once per bay or use create_facility_section with populate_preset)",
        )

    # ── Check 4: No variation applied ─────────────────────────────────────
    equip_total = sum(1 for o in bpy.data.objects if o.get("equipment_type"))
    var_total   = _count_variation_objects(collection_name)

    if equip_total > 0 and var_total == 0:
        first_sec = sections[0].name.replace("Facility_", "") if sections else "YOUR_SECTION"
        _add(
            f"{equip_total} equipment object(s) with no variation — scene looks uniformly new",
            "randomize_facility_variation",
            {
                "section_name":       first_sec,
                "age_factor":         0.4,
                "dust_factor":        0.3,
                "hot_zone_x_m":       5.0,
                "hot_zone_falloff_m": 4.0,
                "seed":               42,
            },
            f"Adds procedural wear + dust to all {equip_total} equipment objects",
        )

    # ── Check 5: Sections never exported ──────────────────────────────────
    unexported = [c for c in sections if not c.get("export_timestamp")]
    if unexported:
        sec_name = unexported[0].name.replace("Facility_", "")
        _add(
            f"{len(unexported)} section(s) created but never exported",
            "export_facility_layout_json",
            {
                "section_name":       sec_name,
                "output_path":        f"/tmp/uptime_exports/{sec_name}_layout.json",
                "include_cables":     True,
                "include_variation":  True,
            },
            f"Writes UE5 manifest for {len(unexported)} section(s); enables PCG placement",
        )

    # ── Check 6: Orphaned data ─────────────────────────────────────────────
    om  = sum(1 for m in bpy.data.meshes     if m.users == 0)
    omt = sum(1 for m in bpy.data.materials  if m.users == 0)
    oc  = sum(1 for c in bpy.data.curves     if c.users == 0)
    orphan_total = om + omt + omt + oc
    if orphan_total > 30:
        _add(
            f"{orphan_total} orphaned datablocks may slow Blender and inflate file size",
            "confirm_destructive",
            {"operation": "clear_orphaned_data", "execute": False},
            f"Dry-run shows {om}M + {omt}Mt + {oc}C zero-user blocks; no change until execute=True",
        )

    # ── Check 7: No cables on a section with multiple racks ───────────────
    cable_total = sum(1 for o in bpy.data.objects if o.get("cable_path"))
    rack_total  = sum(1 for c in bpy.data.collections if c.get("is_rack_cabinet"))
    if rack_total >= 4 and cable_total == 0:
        first_sec = sections[0].name.replace("Facility_", "") if sections else "YOUR_SECTION"
        _add(
            f"{rack_total} racks found but no cable routes created yet",
            "route_cables_between_racks",
            {
                "rack_a":     "RACK_NAME_A",
                "rack_b":     "RACK_NAME_B",
                "cable_type": "cat6",
                "max_cables": 4,
            },
            "Adds NURBS cable curves between adjacent racks for UE5 spline mesh export",
        )

    # ── Context hint reordering ────────────────────────────────────────────
    if context_hint == "about_to_export":
        # Bubble validate and cleanup to the top
        def _export_priority(s: Dict[str, Any]) -> int:
            t = s["tool_name"]
            if t == "validate_entire_scene":  return 0
            if t == "push_undo_checkpoint":   return 1
            if t == "confirm_destructive":    return 2
            return s["priority"] + 10
        suggestions.sort(key=_export_priority)

    elif context_hint == "variation_pass":
        suggestions.insert(0, {
            "priority":         1,
            "reason":           "Starting a variation pass — protect your geometry first",
            "tool_name":        "push_undo_checkpoint",
            "suggested_args":   {"label": "Before variation pass"},
            "estimated_impact": "Ctrl+Z restore point before any material changes",
        })

    elif context_hint == "fresh_session":
        suggestions.insert(0, {
            "priority":         1,
            "reason":           "New session — orient yourself before starting work",
            "tool_name":        "get_scene_inventory",
            "suggested_args":   {},
            "estimated_impact": "Read-only scene overview (no changes)",
        })

    # Re-number priorities after reordering
    for i, s in enumerate(suggestions):
        s["priority"] = i + 1

    if not suggestions:
        suggestions.append({
            "priority":         1,
            "reason":           "Scene looks good — no urgent actions detected",
            "tool_name":        "get_scene_inventory",
            "suggested_args":   {},
            "estimated_impact": "Read-only confirmation",
        })

    return {
        "collection_name": collection_name or "(full scene)",
        "context_hint":    context_hint or "(none)",
        "suggestions":     suggestions[:8],
    }


@mcp.tool()
@thread_safe
def list_all_tools(
    module_filter: str  = "",
    keyword:       str  = "",
    include_params: bool = False,
) -> Dict[str, Any]:
    """
    Return all registered MCP tools grouped by source module with one-line
    descriptions extracted from their docstrings.

    Use module_filter to narrow results to a single module (e.g. "cable_tools").
    Use keyword to search across tool names and descriptions (case-insensitive).

    module_filter:  limit results to this module name (empty = all modules)
    keyword:        search string (empty = no filter); matches name + description
    include_params: if True, include parameter count and names per tool

    Returns:
      total_count   int
      module_count  int
      filters       {module, keyword}
      modules       list[{module_name, tool_count, tools: [{name, description,
                                                            param_count, params?}]}]
    """
    try:
        raw_tools = _mcp_list_tools_sync()
    except Exception as exc:
        return {"error": f"Could not retrieve tool list: {exc}"}

    module_map = _build_module_map()

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for tool in raw_tools:
        mod = module_map.get(tool.name, "server")

        if module_filter and mod != module_filter:
            continue

        desc_first = (tool.description or "").split("\n")[0].strip()[:120]

        if keyword:
            haystack = (tool.name + " " + desc_first).lower()
            if keyword.lower() not in haystack:
                continue

        schema      = tool.inputSchema or {}
        props       = schema.get("properties", {})
        param_count = len(props)

        entry: Dict[str, Any] = {
            "name":        tool.name,
            "description": desc_first,
            "param_count": param_count,
        }
        if include_params:
            entry["params"] = list(props.keys())

        groups.setdefault(mod, []).append(entry)

    ordered: List[Dict[str, Any]] = []
    for mod_name in _MODULE_DISPLAY_ORDER:
        if mod_name in groups:
            ordered.append({
                "module_name": mod_name,
                "tool_count":  len(groups[mod_name]),
                "tools":       groups[mod_name],
            })
    for mod_name, tool_list in groups.items():
        if mod_name not in _MODULE_DISPLAY_ORDER:
            ordered.append({
                "module_name": mod_name,
                "tool_count":  len(tool_list),
                "tools":       tool_list,
            })

    total = sum(m["tool_count"] for m in ordered)
    return {
        "total_count":  total,
        "module_count": len(ordered),
        "filters":      {
            "module":  module_filter or None,
            "keyword": keyword or None,
        },
        "modules": ordered,
    }


@mcp.tool()
@thread_safe
def export_tool_reference(
    output_path:    str,
    format:         str  = "markdown",
    include_params: bool = True,
    module_filter:  str  = "",
) -> Dict[str, Any]:
    """
    Write a complete tool reference document to disk, generated live from
    registered docstrings and parameter schemas.

    Because the reference is derived from the running addon, it is always
    accurate — it reflects exactly the tools that are deployed, not a
    hand-maintained document that can go stale.

    Use this to generate:
      • The GitHub README tool reference section (format="markdown")
      • A machine-readable schema for IDE integrations (format="json")
      • A module-specific cheat sheet (set module_filter, e.g. "cable_tools")

    Markdown output structure:
      # Universal Blender MCP — Tool Reference
      ## rack_tools (25 tools)
      ### `create_rack_cabinet`
      | Parameter | Type | Default | Req | Description |
      ...

    output_path:    absolute path (.md for Markdown, .json for JSON)
    format:         "markdown" | "json"
    include_params: include full parameter table per tool (True recommended)
    module_filter:  limit to one module (empty = all modules)
    """
    if format not in ("markdown", "json"):
        return {"error": f"format must be 'markdown' or 'json', got '{format}'"}

    try:
        raw_tools = _mcp_list_tools_sync()
    except Exception as exc:
        return {"error": f"Could not retrieve tool list: {exc}"}

    module_map = _build_module_map()

    # Group tools by module
    groups: Dict[str, List[Any]] = {}
    for tool in raw_tools:
        mod = module_map.get(tool.name, "server")
        if module_filter and mod != module_filter:
            continue
        groups.setdefault(mod, []).append(tool)

    if format == "markdown":
        content = _build_markdown_reference(groups, include_params)
    else:
        doc     = _build_json_reference(groups, include_params)
        content = json.dumps(doc, indent=2)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    total    = sum(len(v) for v in groups.values())
    size_kb  = Path(output_path).stat().st_size / 1024
    sections = [m for m in _MODULE_DISPLAY_ORDER if m in groups]

    _log_entry(
        "export_tool_reference", 0.0, "ok",
        f"{total} tools → {Path(output_path).name}  ({size_kb:.0f} KB, {format})",
    )

    return {
        "output_path":       output_path,
        "format":            format,
        "tools_documented":  total,
        "sections":          sections,
        "file_size_kb":      round(size_kb, 1),
    }
