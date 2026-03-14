"""
Blender API discovery with keyword search and disk-backed JSON cache.

Indexes bpy.ops.*, bpy.types.*, and bpy.data members so an MCP agent can
locate operators and types without knowing their exact names.  The index is
built once (≈ 2-4 s) and persisted to api_discovery_cache.json alongside
the addon; subsequent server starts load from disk in milliseconds.
"""

import bpy
import difflib
import json
import os
import time
from typing import Any, Dict, List, Optional

_ADDON_DIR = os.path.dirname(__file__)
_CACHE_PATH = os.path.join(_ADDON_DIR, "api_discovery_cache.json")

_index: Optional[Dict[str, Any]] = None  # populated on first use


# ── Collectors ─────────────────────────────────────────────────────────────

def _collect_ops() -> List[Dict]:
    entries: List[Dict] = []
    for mod_name in dir(bpy.ops):
        if mod_name.startswith("_"):
            continue
        try:
            mod = getattr(bpy.ops, mod_name)
        except Exception:
            continue
        for op_name in dir(mod):
            if op_name.startswith("_"):
                continue
            try:
                op = getattr(mod, op_name)
                doc = (getattr(op, "__doc__", "") or "").strip()
                # Optional: try to get param names from RNA (best-effort, fast path only)
                params: List[str] = []
                try:
                    rna = op.get_rna_type()
                    for p in rna.properties:
                        if p.identifier == "rna_type":
                            continue
                        params.append(f"{p.identifier}:{p.type}")
                        if len(params) >= 8:
                            break
                except Exception:
                    pass
                entries.append({
                    "kind": "op",
                    "name": f"bpy.ops.{mod_name}.{op_name}",
                    "call": f"bpy.ops.{mod_name}.{op_name}()",
                    "doc": doc[:250],
                    "params": params,
                })
            except Exception:
                pass
    return entries


def _collect_types() -> List[Dict]:
    entries: List[Dict] = []
    for type_name in dir(bpy.types):
        if type_name.startswith("_"):
            continue
        try:
            t = getattr(bpy.types, type_name)
            doc = (getattr(t, "__doc__", "") or "").strip()
            if not doc:
                continue
            entries.append({
                "kind": "type",
                "name": f"bpy.types.{type_name}",
                "call": f"bpy.types.{type_name}",
                "doc": doc[:200],
                "params": [],
            })
        except Exception:
            pass
    return entries


def _collect_data() -> List[Dict]:
    entries: List[Dict] = []
    for attr in dir(bpy.data):
        if attr.startswith("_"):
            continue
        try:
            val = getattr(bpy.data, attr)
            doc = (getattr(type(val), "__doc__", "") or "").strip()
            entries.append({
                "kind": "data",
                "name": f"bpy.data.{attr}",
                "call": f"bpy.data.{attr}",
                "doc": doc[:150],
                "params": [],
            })
        except Exception:
            pass
    return entries


# ── Public API ─────────────────────────────────────────────────────────────

def build_index(force: bool = False) -> Dict[str, Any]:
    """Build (or load from disk) the full API index.  Returns the index dict."""
    global _index

    if not force and os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH, encoding="utf-8") as f:
                _index = json.load(f)
            print(
                f"[BlenderMCP] Discovery cache loaded: {len(_index['entries'])} entries",
                flush=True,
            )
            return _index
        except Exception:
            pass  # fall through to rebuild

    t0 = time.perf_counter()
    entries = _collect_ops() + _collect_types() + _collect_data()
    _index = {
        "entries": entries,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_index, f, separators=(",", ":"))
    except Exception as exc:
        print(f"[BlenderMCP] Discovery cache write failed: {exc}", flush=True)

    elapsed = time.perf_counter() - t0
    print(
        f"[BlenderMCP] Discovery index built: {len(entries)} entries in {elapsed:.1f}s",
        flush=True,
    )
    return _index


def search(
    query: str,
    category: str = "",
    max_results: int = 10,
) -> List[Dict[str, Any]]:
    """
    Search the API index.

    query:      search term (e.g. "mirror", "bevel", "material", "bpy.ops.mesh")
    category:   filter by "ops" | "types" | "data" | sub-module name like "mesh"
    max_results: cap on returned entries
    """
    global _index
    if _index is None:
        build_index()

    entries: List[Dict] = list(_index["entries"])
    q = query.lower().strip()
    cat = category.lower().strip()

    # Category filter
    if cat:
        if cat in ("ops", "operators", "op"):
            entries = [e for e in entries if e["kind"] == "op"]
        elif cat in ("types", "type"):
            entries = [e for e in entries if e["kind"] == "type"]
        elif cat == "data":
            entries = [e for e in entries if e["kind"] == "data"]
        else:
            entries = [e for e in entries if cat in e["name"].lower()]

    if not q:
        return entries[:max_results]

    scored: List[tuple] = []
    for e in entries:
        name_l = e["name"].lower()
        doc_l  = e["doc"].lower()
        score  = 0

        if q == name_l:
            score = 100
        elif name_l.endswith(f".{q}") or name_l.endswith(f".{q}()"):
            score = 85
        elif f".{q}" in name_l:
            score = 65
        elif q in name_l:
            score = 45
        elif q in doc_l:
            score = 20
        else:
            last_seg = name_l.rsplit(".", 1)[-1]
            ratio = difflib.SequenceMatcher(None, q, last_seg).ratio()
            if ratio > 0.55:
                score = int(ratio * 15)

        if score > 0:
            scored.append((score, e))

    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:max_results]]
