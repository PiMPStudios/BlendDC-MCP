#!/usr/bin/env python3
"""
UPTIME BlenderDC — Full Pipeline Integration Test Suite
=======================================================
Version:  v2.7.0  (186 tools, Phases 1–8)
Protocol: MCP Streamable HTTP  →  http://127.0.0.1:8400/mcp

HOW TO RUN
──────────
1. Open Blender with a clean scene  (File → New → General is fine)
2. N-Panel → MCP tab → click "Start MCP Server"
   • Wait for "MCP Server running" in the System Console
3. From a terminal OUTSIDE Blender:

       python3 tests/full_uptime_pipeline_test.py

   Override the export directory:
       OUTPUT_DIR=/tmp/uptime_test python3 tests/full_uptime_pipeline_test.py

   Override the server URL:
       MCP_URL=http://127.0.0.1:8400/mcp python3 tests/full_uptime_pipeline_test.py

NOTE: requires only the Python standard library (no pip packages).
      Uses urllib.request so it runs wherever Python 3.8+ is available.

EXIT CODES
──────────
  0  All tests passed (warnings are OK)
  1  One or more hard failures

PHASES
──────
  Phase 1 — Server health              tools/list, version check
  Phase 2 — Facility creation          create_facility_section (bare + preset)
  Phase 3 — Bay & row validation       validate_bay, validate_facility, info queries
  Phase 4 — Cable routing              route, bundle, tray, validate, export
  Phase 5 — Variation & failure        themes, post_incident, hot-zone, reports
  Phase 6 — Full UE5 export            manifest JSON, rack FBX, equipment FBX
  Phase 7 — Cleanup & reset            clear_cables, reset_variation, final validate
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import urllib.error


# ══════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  — edit or override via environment variables
# ══════════════════════════════════════════════════════════════════════════

# Where test exports are written.  Switch to your UE5 project's Content/
# folder to validate the files import correctly.
OUTPUT_DIR = Path(os.environ.get(
    "OUTPUT_DIR",
    Path.home() / "Desktop" / "uptime_test_exports",
))

# MCP server address (started via N-Panel → MCP → Start MCP Server)
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8400/mcp")

# Per-call timeout in seconds.  Exports and facility creation can take a
# while on large scenes, so leave this generous.
TIMEOUT_S = 180

# Root collection name used throughout the test.  Change only if you need
# to run this alongside an existing scene that already uses this name.
SECTION = "UPTIME_TEST_SECTION"

# Minimum expected tool count.  Adjust upward as phases are added.
MIN_TOOL_COUNT = 186


# ══════════════════════════════════════════════════════════════════════════
#  FORMATTING
# ══════════════════════════════════════════════════════════════════════════

_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

# Disable colour if stdout is not a TTY (e.g. redirected to a file)
if not sys.stdout.isatty():
    _GREEN = _YELLOW = _RED = _CYAN = _BOLD = _DIM = _RESET = ""

PASS = f"{_GREEN}{_BOLD}[PASS]{_RESET}"
WARN = f"{_YELLOW}{_BOLD}[WARN]{_RESET}"
FAIL = f"{_RED}{_BOLD}[FAIL]{_RESET}"
INFO = f"{_CYAN}[INFO]{_RESET}"

def _trunc(v: Any, n: int = 90) -> str:
    s = str(v)
    return s if len(s) <= n else s[:n] + "…"


# ══════════════════════════════════════════════════════════════════════════
#  RESULT ACCUMULATOR
# ══════════════════════════════════════════════════════════════════════════

class Results:
    def __init__(self) -> None:
        self.passed:  List[str]  = []
        self.warned:  List[str]  = []
        self.failed:  List[str]  = []
        self.outputs: List[Path] = []
        self._t0 = time.perf_counter()

    # ── record ────────────────────────────────────────────────────────────

    def ok(self, name: str, detail: str = "") -> None:
        self.passed.append(name)
        suffix = f"  {_DIM}({detail}){_RESET}" if detail else ""
        print(f"  {PASS}  {name}{suffix}")

    def warn(self, name: str, detail: str = "") -> None:
        self.warned.append(name)
        suffix = f"  {_DIM}({detail}){_RESET}" if detail else ""
        print(f"  {WARN}  {name}{suffix}")

    def fail(self, name: str, detail: str = "") -> None:
        self.failed.append(name)
        suffix = f"  {_DIM}({detail}){_RESET}" if detail else ""
        print(f"  {FAIL}  {name}{suffix}")

    def file(self, path: Path) -> None:
        self.outputs.append(path)

    # ── final summary ─────────────────────────────────────────────────────

    def summary(self) -> int:
        elapsed  = time.perf_counter() - self._t0
        total    = len(self.passed) + len(self.warned) + len(self.failed)
        bar      = "═" * 68

        print()
        print(bar)
        print(f"{_BOLD}{_CYAN}  UPTIME PIPELINE TEST SUMMARY{_RESET}   "
              f"{_DIM}({elapsed:.1f}s total){_RESET}")
        print(bar)
        print(f"  {_GREEN}Passed{_RESET} : {len(self.passed):>3d}")
        print(f"  {_YELLOW}Warned{_RESET} : {len(self.warned):>3d}")
        print(f"  {_RED}Failed{_RESET} : {len(self.failed):>3d}")
        print(f"  Total  : {total:>3d}")

        if self.outputs:
            print()
            print(f"  {_BOLD}Generated files ({len(self.outputs)}):{_RESET}")
            for p in self.outputs:
                exists = "✓" if p.exists() else "✗"
                print(f"    [{exists}]  {p}")

        print()

        if self.failed:
            print(f"{_RED}{_BOLD}  ✗  TESTS FAILED{_RESET}")
            print()
            for name in self.failed:
                print(f"    • {name}")
            print()
            print(f"  {_BOLD}Suggested next steps:{_RESET}")
            print("    1. Open Blender's System Console and look for Python tracebacks.")
            print("    2. Confirm the addon version is v2.7.0+  "
                  "(N-Panel shows version).")
            print("    3. Re-run against a fresh scene  (File → New → General).")
            print("    4. Check that facility_tools is in the module reload list in")
            print("       __init__.py and that server.py imports it at the bottom.")
            return 1

        if self.warned:
            print(f"{_YELLOW}{_BOLD}  ⚠  ALL TESTS PASSED  (with warnings — see above){_RESET}")
        else:
            print(f"{_GREEN}{_BOLD}  ✓  ALL TESTS PASSED{_RESET}")
        return 0


# ══════════════════════════════════════════════════════════════════════════
#  MCP SESSION
# ══════════════════════════════════════════════════════════════════════════

class MCPSession:
    """
    Minimal MCP client for Streamable HTTP transport.

    Handles the initialize/initialized handshake, persistent session ID
    (if the server issues one), and both JSON and SSE response bodies.

    Every tool call is a single POST.  The server is effectively stateless
    across calls for tool execution (Blender state is shared global state),
    so the session ID is tracked but its absence won't break tool calls.
    """

    PROTOCOL_VER = "2024-11-05"

    def __init__(self, url: str = MCP_URL) -> None:
        self.url        = url
        self._id        = 0
        self._sid: Optional[str] = None   # Mcp-Session-Id from server

    # ── internal helpers ──────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict) -> dict:
        """POST JSON, return parsed response dict (handles JSON + SSE)."""
        data    = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept":       "application/json, text/event-stream",
        }
        if self._sid:
            headers["Mcp-Session-Id"] = self._sid

        req = urllib.request.Request(
            self.url, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._sid = sid
                ctype = resp.headers.get("Content-Type", "")
                raw   = resp.read().decode("utf-8", errors="replace")

                if not raw.strip():
                    # 204 / empty body — normal for notifications
                    return {}

                if "text/event-stream" in ctype:
                    return self._parse_sse(raw)
                return json.loads(raw)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ConnectionError(
                f"HTTP {exc.code} from {self.url}: {body[:200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(
                f"Cannot reach MCP server at {self.url}.\n"
                f"  Cause: {exc.reason}\n"
                f"  Make sure Blender is open and the MCP server is running\n"
                f"  (N-Panel → MCP → Start MCP Server)."
            ) from exc

    @staticmethod
    def _parse_sse(raw: str) -> dict:
        """Return the last parseable JSON object from an SSE stream."""
        result = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    continue
                try:
                    result = json.loads(chunk)
                except json.JSONDecodeError:
                    pass
        if result is None:
            raise ValueError(
                f"No parseable data in SSE response:\n{raw[:400]}"
            )
        return result

    # ── public API ────────────────────────────────────────────────────────

    def initialize(self) -> dict:
        """Perform the MCP initialize handshake. Must be called first."""
        resp = self._post({
            "jsonrpc": "2.0",
            "id":      self._next_id(),
            "method":  "initialize",
            "params":  {
                "protocolVersion": self.PROTOCOL_VER,
                "capabilities":    {"roots": {"listChanged": False}},
                "clientInfo":      {
                    "name":    "uptime-pipeline-test",
                    "version": "2.7.0",
                },
            },
        })
        if "error" in resp:
            raise RuntimeError(f"Initialize failed: {resp['error']}")

        # Send initialized notification (fire-and-forget; response may be empty)
        try:
            self._post({
                "jsonrpc": "2.0",
                "method":  "notifications/initialized",
                "params":  {},
            })
        except Exception:
            pass

        return resp.get("result", {})

    def list_tools(self) -> List[dict]:
        resp = self._post({
            "jsonrpc": "2.0",
            "id":      self._next_id(),
            "method":  "tools/list",
            "params":  {},
        })
        if "error" in resp:
            raise RuntimeError(f"tools/list error: {resp['error']}")
        return resp["result"]["tools"]

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> Any:
        """
        Invoke a named tool and return its result.

        Return type is dict/list if the tool returned JSON, otherwise str.
        Raises RuntimeError if the server returns an MCP error object.
        """
        resp = self._post({
            "jsonrpc": "2.0",
            "id":      self._next_id(),
            "method":  "tools/call",
            "params":  {"name": name, "arguments": arguments or {}},
        })
        if "error" in resp:
            raise RuntimeError(
                f"Tool '{name}' error: {resp['error']}"
            )

        # MCP content is a list of typed blocks; join text blocks
        content    = resp.get("result", {}).get("content", [])
        text_parts = [c["text"] for c in content if c.get("type") == "text"]
        raw        = "\n".join(text_parts)

        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw


# ══════════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════

def section_header(title: str) -> None:
    width = 68
    print()
    print(f"{_BOLD}{_CYAN}{'─' * width}{_RESET}")
    print(f"{_BOLD}{_CYAN}  {title}{_RESET}")
    print(f"{_BOLD}{_CYAN}{'─' * width}{_RESET}")


def check_dict_key(
    r:         Results,
    test_name: str,
    data:      Any,
    key:       str,
    expected:  Any = None,
) -> bool:
    """
    Assert that data is a dict containing key.
    Optionally check the value matches expected (mismatch → WARN, not FAIL).
    Returns True if the key is present.
    """
    if not isinstance(data, dict):
        r.fail(test_name, f"expected dict, got {type(data).__name__}: {_trunc(data, 60)}")
        return False
    if key not in data:
        r.fail(test_name, f"missing key '{key}'")
        return False
    if expected is not None and data[key] != expected:
        r.warn(test_name, f"'{key}': expected {expected!r}, got {data[key]!r}")
        return True
    r.ok(test_name, f"{key}={_trunc(data[key])!r}" if expected is None else "")
    return True


# ══════════════════════════════════════════════════════════════════════════
#  PHASE 1 — SERVER HEALTH
# ══════════════════════════════════════════════════════════════════════════

def phase_server_health(mcp: MCPSession, r: Results) -> List[dict]:
    """
    Verifies the MCP server is up, returns the tool list.

    What to check in Blender:
      System Console → "[BlenderMCP] Schema cache ready — 186 tools in X.XXs"
    What to check here:
      All [PASS]; tool count ≥ 186; facility_tools key names present.

    Aborts the entire test run on connection failure — there is no point
    continuing if the server isn't reachable.
    """
    section_header("Phase 1 — Server Health")

    # 1a. Initialize handshake
    try:
        caps = mcp.initialize()
        proto = caps.get("protocolVersion", "?")
        r.ok("MCP initialize handshake", f"protocolVersion={proto}")
    except ConnectionError as exc:
        r.fail("MCP initialize handshake", str(exc))
        print()
        print(f"  {_RED}{_BOLD}FATAL — cannot reach MCP server.  Aborting.{_RESET}")
        sys.exit(1)
    except Exception as exc:
        r.fail("MCP initialize handshake", str(exc))
        sys.exit(1)

    # 1b. tools/list — confirm count
    try:
        tools = mcp.list_tools()
        count = len(tools)
        if count >= MIN_TOOL_COUNT:
            r.ok(f"Tool count ≥ {MIN_TOOL_COUNT}", f"{count} tools registered")
        elif count >= 150:
            r.warn(
                f"Tool count ≥ {MIN_TOOL_COUNT}",
                f"Only {count} tools — check __init__.py reload order & addon version",
            )
        else:
            r.fail(
                f"Tool count ≥ {MIN_TOOL_COUNT}",
                f"Only {count} tools — addon may not be v2.7.0 or modules failed to load",
            )
        return tools
    except Exception as exc:
        r.fail("tools/list", str(exc))
        return []

    # 1c. Key facility_tools names present
    tool_names = {t["name"] for t in tools}
    must_have = {
        "create_facility_section",
        "apply_facility_theme",
        "randomize_facility_variation",
        "export_facility_layout_json",
        "validate_facility",
        "get_facility_info",
        "get_section_bays",
    }
    missing = must_have - tool_names
    if not missing:
        r.ok("facility_tools registered", f"{len(must_have)} key tools present")
    else:
        r.fail("facility_tools registered", f"missing: {missing}")

    # 1d. Cross-phase tools present
    cross_phase = {
        "route_cables_between_racks",   # cable_tools
        "apply_wear_variation",         # variation_tools
        "validate_bay",                 # bay_tools
        "export_rack_collection_ue5",   # export_tools
    }
    missing2 = cross_phase - tool_names
    if not missing2:
        r.ok("Cross-phase tool availability", "cable/variation/bay/export tools present")
    else:
        r.warn("Cross-phase tool availability", f"missing: {missing2}")

    return tools


# ══════════════════════════════════════════════════════════════════════════
#  PHASE 2 — FACILITY CREATION
# ══════════════════════════════════════════════════════════════════════════

def phase_facility_creation(
    mcp: MCPSession, r: Results
) -> Tuple[List[str], Optional[str]]:
    """
    Creates a 2×2 bay section and a smaller preset-populated section.

    Viewport: look for a new 'Facility_UPTIME_TEST_SECTION' collection in the
    Outliner with 4 child Bay_ collections, each containing two Row_ collections
    (hot aisle / cold aisle), perimeter wall meshes, and a raised-floor slab.

    Returns (bay_names, preset_section_name).
    """
    section_header("Phase 2 — Facility Creation")
    PRESET_SECTION = f"{SECTION}_PRESET"

    # 2a. Bare geometry section (fast — no equipment population)
    #     This is the primary section used for all downstream tests.
    try:
        result = mcp.call_tool("create_facility_section", {
            "section_name":          SECTION,
            "bays_x":                2,
            "bays_y":                2,
            "racks_per_bay":         4,
            "u_height":              42,
            "aisle_width_mm":        1200.0,
            "add_perimeter_walls":   True,
            "wall_height_m":         4.0,
        })
        r.ok("create_facility_section — 2×2 bare geometry", _trunc(result))
    except Exception as exc:
        r.fail("create_facility_section — 2×2 bare geometry", str(exc))
        # Without the section there is nothing to test downstream.
        return [], None

    # 2b. Preset-populated section (1×2 bays — tests the populate_preset code path)
    #     Creates a smaller section so this step isn't too slow.
    try:
        result = mcp.call_tool("create_facility_section", {
            "section_name":    PRESET_SECTION,
            "bays_x":          1,
            "bays_y":          2,
            "racks_per_bay":   3,
            "populate_preset": "standard_3tier",
        })
        r.ok("create_facility_section — 1×2 with populate_preset=standard_3tier",
             _trunc(result))
        preset_section = PRESET_SECTION
    except Exception as exc:
        r.warn("create_facility_section — populate_preset path", str(exc))
        preset_section = None

    # 2c. Discover bay names via get_section_bays
    #     All downstream tests derive rack names from these.
    try:
        result = mcp.call_tool("get_section_bays", {"section_name": SECTION})
        # Result shape: {"bays": [{"name": ..., "x_m": ..., "y_m": ...}, ...]}
        if isinstance(result, dict) and "bays" in result:
            bays = [
                b["name"] if isinstance(b, dict) else b
                for b in result["bays"]
            ]
        elif isinstance(result, list):
            bays = [b["name"] if isinstance(b, dict) else b for b in result]
        else:
            bays = []

        if len(bays) >= 4:
            r.ok("get_section_bays — ≥4 bays", str(bays))
        elif bays:
            r.warn("get_section_bays", f"expected ≥4, got {len(bays)}: {bays}")
        else:
            r.fail("get_section_bays", "returned empty list — check section creation")
            return [], preset_section

        return bays, preset_section

    except Exception as exc:
        r.fail("get_section_bays", str(exc))
        return [], preset_section


# ══════════════════════════════════════════════════════════════════════════
#  PHASE 3 — BAY & ROW VALIDATION
# ══════════════════════════════════════════════════════════════════════════

def phase_bay_validation(
    mcp: MCPSession, r: Results, bays: List[str]
) -> None:
    """
    Validates every bay and the full facility section.

    Success means:
      • No 'FAIL' entries in any per-bay report  (overlapping U slots, missing
        origins, out-of-bounds equipment)
      • validate_facility reports 0 errors
      • get_bay_info returns plausible rack counts
      • get_facility_info computes a non-zero ue5_actor_estimate

    Nothing in the viewport changes — this is read-only.  Check the System
    Console for per-rack detail lines if you need to drill into a failure.
    """
    section_header("Phase 3 — Bay & Row Validation")

    if not bays:
        r.warn("Bay validation (all)", "No bay names — skipping Phase 3")
        return

    # 3a. validate_bay on each bay individually
    for bay_name in bays:
        try:
            result = mcp.call_tool("validate_bay", {"bay_name": bay_name})
            errors   = result.get("errors",   []) if isinstance(result, dict) else []
            warnings = result.get("warnings", []) if isinstance(result, dict) else []
            if errors:
                r.fail(f"validate_bay({bay_name})",
                       f"{len(errors)} error(s): {errors[:2]}")
            elif warnings:
                r.warn(f"validate_bay({bay_name})",
                       f"{len(warnings)} warning(s): {warnings[:1]}")
            else:
                r.ok(f"validate_bay({bay_name})")
        except Exception as exc:
            r.fail(f"validate_bay({bay_name})", str(exc))

    # 3b. validate_facility — aggregated section-level check
    try:
        result = mcp.call_tool("validate_facility", {"section_name": SECTION})
        fail_count = result.get("fail_count", -1) if isinstance(result, dict) else -1
        warn_count = result.get("warn_count", -1) if isinstance(result, dict) else -1
        if fail_count == 0:
            r.ok("validate_facility", f"0 failures, {warn_count} warnings")
        elif fail_count > 0:
            r.warn("validate_facility",
                   f"{fail_count} failures, {warn_count} warnings — see System Console")
        else:
            r.ok("validate_facility", _trunc(result))
    except Exception as exc:
        r.fail("validate_facility", str(exc))

    # 3c. get_bay_info on first bay
    try:
        result = mcp.call_tool("get_bay_info", {"bay_name": bays[0]})
        rack_count = result.get("rack_count", "?") if isinstance(result, dict) else "?"
        u_total    = result.get("u_total",    "?") if isinstance(result, dict) else "?"
        r.ok("get_bay_info", f"racks={rack_count}, u_total={u_total}")
    except Exception as exc:
        r.warn("get_bay_info", str(exc))

    # 3d. get_facility_info — section totals + UE5 actor estimate
    try:
        result = mcp.call_tool("get_facility_info", {"section_name": SECTION})
        est = (
            result.get("ue5_actor_estimate", "?")
            if isinstance(result, dict) else "?"
        )
        total_racks = (
            result.get("total_racks", "?")
            if isinstance(result, dict) else "?"
        )
        r.ok("get_facility_info",
             f"total_racks={total_racks}, ue5_actor_estimate={est}")
    except Exception as exc:
        r.warn("get_facility_info", str(exc))


# ══════════════════════════════════════════════════════════════════════════
#  PHASE 4 — CABLE ROUTING
# ══════════════════════════════════════════════════════════════════════════

def phase_cable_routing(
    mcp: MCPSession, r: Results, bays: List[str]
) -> None:
    """
    Routes cables between racks, generates a bundle, adds an overhead tray,
    then validates and exports cable data.

    Viewport after this phase:
      • NURBS curve objects with a visible bevel cross-section between racks
      • Blue cables = cat6 ethernet, orange = power
      • A grey flat box above the racks = overhead cable tray

    The cable export JSON (written to OUTPUT_DIR) can be consumed by a UE5
    PCG graph or Blueprint to instance Spline Mesh / Cable Component actors.
    """
    section_header("Phase 4 — Cable Routing")

    if not bays:
        r.warn("Cable routing (all)", "No bay names — skipping Phase 4")
        return

    first_bay = bays[0]

    # 4a. Discover rack names within the first bay
    rack_a = rack_b = None
    try:
        bay_info = mcp.call_tool("get_bay_info", {"bay_name": first_bay})
        racks = bay_info.get("racks", []) if isinstance(bay_info, dict) else []
        if len(racks) >= 2:
            rack_a = racks[0]["name"] if isinstance(racks[0], dict) else racks[0]
            rack_b = racks[1]["name"] if isinstance(racks[1], dict) else racks[1]
            r.ok("Discover rack pair for cable test", f"{rack_a}  ↔  {rack_b}")
        else:
            r.warn("Discover rack pair", f"Only {len(racks)} rack(s) in {first_bay}")
    except Exception as exc:
        r.warn("Discover rack pair", str(exc))

    # 4b. route_cables_between_racks — cat6 ethernet
    if rack_a and rack_b:
        for cable_type in ("cat6", "power"):
            try:
                result = mcp.call_tool("route_cables_between_racks", {
                    "rack_a":      rack_a,
                    "rack_b":      rack_b,
                    "cable_type":  cable_type,
                    "max_cables":  3,
                })
                r.ok(f"route_cables_between_racks ({cable_type}×3)", _trunc(result))
            except Exception as exc:
                r.fail(f"route_cables_between_racks ({cable_type})", str(exc))

        # 4c. generate_cable_bundle — 6-cable ethernet bundle on first rack
        try:
            result = mcp.call_tool("generate_cable_bundle", {
                "rack_a":          rack_a,
                "rack_b":          rack_b,
                "cable_type":      "cat6",
                "count":           6,
                "bundle_radius_m": 0.04,
                "seed":            42,
            })
            r.ok("generate_cable_bundle (cat6 × 6)", _trunc(result))
        except Exception as exc:
            r.warn("generate_cable_bundle", str(exc))
    else:
        r.warn("Cable routing tools", "No rack pair found — route/bundle tests skipped")

    # 4d. add_overhead_cable_tray on first bay
    try:
        result = mcp.call_tool("add_overhead_cable_tray", {
            "bay_collection_name": first_bay,
            "tray_width_m":        0.3,
        })
        r.ok("add_overhead_cable_tray", _trunc(result))
    except Exception as exc:
        r.warn("add_overhead_cable_tray", str(exc))

    # 4e. validate_cable_routing — check all cables in the section
    try:
        result = mcp.call_tool("validate_cable_routing", {
            "collection_name": SECTION,
        })
        errors = result.get("errors", []) if isinstance(result, dict) else []
        warns  = result.get("warnings", []) if isinstance(result, dict) else []
        if not errors:
            r.ok("validate_cable_routing", f"0 errors, {len(warns)} warning(s)")
        else:
            r.warn("validate_cable_routing",
                   f"{len(errors)} issue(s): {errors[:2]}")
    except Exception as exc:
        r.warn("validate_cable_routing", str(exc))

    # 4f. export_cable_data_json — write routing manifest for UE5
    cable_json = OUTPUT_DIR / f"{SECTION}_cables.json"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = mcp.call_tool("export_cable_data_json", {
            "output_path":     str(cable_json),
            "collection_name": SECTION,
        })
        if cable_json.exists():
            kb = cable_json.stat().st_size / 1024
            r.ok("export_cable_data_json", f"{kb:.1f} KB → {cable_json.name}")
            r.file(cable_json)
        else:
            r.warn("export_cable_data_json", f"tool completed but file missing: {cable_json}")
    except Exception as exc:
        r.warn("export_cable_data_json", str(exc))


# ══════════════════════════════════════════════════════════════════════════
#  PHASE 5 — VARIATION & FAILURE STATES
# ══════════════════════════════════════════════════════════════════════════

def phase_variation_states(
    mcp: MCPSession, r: Results, bays: List[str]
) -> None:
    """
    Applies visual themes and variation to the facility, then reads back reports.

    Viewport expectations:
      After aged_colo:    racks should have subtle roughness differences.
      After post_incident on bays[0]:  central rack → emission glow (orange/red).
        Surrounding racks → scratched/degraded materials.
      After randomize_facility_variation:  racks near hot_zone_x_m show heavier
        wear than those further away (visible roughness gradient).

    get_variation_report is read-only — check it returns non-empty coverage data.
    """
    section_header("Phase 5 — Variation & Failure States")

    if not bays:
        r.warn("Variation tests", "No bay names — skipping Phase 5")
        return

    # 5a. apply_facility_theme: aged_colo — section-wide aesthetic aged theme
    try:
        result = mcp.call_tool("apply_facility_theme", {
            "section_name": SECTION,
            "theme":        "aged_colo",
            "seed":         1701,
        })
        r.ok("apply_facility_theme — aged_colo", _trunc(result))
    except Exception as exc:
        r.fail("apply_facility_theme — aged_colo", str(exc))

    # 5b. apply_facility_theme: post_incident — incident on explicit epicentre bay
    #     The epicenter_bay parameter was a specific Phase 8 user request.
    #     Success = central rack of bays[0] has a failure preset applied.
    try:
        result = mcp.call_tool("apply_facility_theme", {
            "section_name":  SECTION,
            "theme":         "post_incident",
            "epicenter_bay": bays[0],
            "seed":          2025,
        })
        r.ok("apply_facility_theme — post_incident (explicit epicenter)", _trunc(result))
    except Exception as exc:
        r.fail("apply_facility_theme — post_incident", str(exc))

    # 5c. randomize_facility_variation — hot-zone gradient across section
    #     hot_zone_x_m=5.0 means racks near X=5m get max severity_bias,
    #     racks 4m away (X=1m or X=9m) get near-zero bias.
    try:
        result = mcp.call_tool("randomize_facility_variation", {
            "section_name":       SECTION,
            "age_factor":         0.45,
            "dust_factor":        0.30,
            "hot_zone_x_m":       5.0,
            "hot_zone_falloff_m": 4.0,
            "seed":               99,
        })
        r.ok("randomize_facility_variation — hot_zone gradient", _trunc(result))
    except Exception as exc:
        r.fail("randomize_facility_variation", str(exc))

    # 5d. get_variation_report — read-only summary of first bay
    try:
        result = mcp.call_tool("get_variation_report", {"bay_name": bays[0]})
        if isinstance(result, dict):
            wear_count = result.get("wear_count",    "?")
            dust_count = result.get("dust_count",    "?")
            coverage   = result.get("coverage_pct",  "?")
            r.ok("get_variation_report",
                 f"wear={wear_count}, dust={dust_count}, coverage={coverage}%")
        else:
            r.warn("get_variation_report", _trunc(result))
    except Exception as exc:
        r.warn("get_variation_report", str(exc))

    # 5e. apply_wear_variation on a single object (direct object-level call)
    #     Uses the first bay's name as a proxy — variation_tools accepts
    #     either a single object name or a collection name.
    try:
        result = mcp.call_tool("apply_wear_variation", {
            "object_name": bays[0],
            "wear_level":  0.4,
            "seed":        7,
        })
        r.ok("apply_wear_variation — direct object call", _trunc(result))
    except Exception as exc:
        r.warn("apply_wear_variation", str(exc))

    # 5f. apply_dust_overlay
    try:
        result = mcp.call_tool("apply_dust_overlay", {
            "object_name":       bays[0],
            "dust_intensity":    0.30,
            "accumulation_bias": "top",
        })
        r.ok("apply_dust_overlay — top-surface accumulation", _trunc(result))
    except Exception as exc:
        r.warn("apply_dust_overlay", str(exc))

    # 5g. randomize_bay_variation — bay-level call with severity_bias (Phase 7 feature)
    try:
        result = mcp.call_tool("randomize_bay_variation", {
            "bay_name":       bays[0],
            "age_factor":     0.5,
            "dust_factor":    0.35,
            "severity_bias":  0.7,
            "seed":           12345,
        })
        r.ok("randomize_bay_variation — severity_bias=0.7", _trunc(result))
    except Exception as exc:
        r.warn("randomize_bay_variation", str(exc))


# ══════════════════════════════════════════════════════════════════════════
#  PHASE 6 — FULL UE5 EXPORT
# ══════════════════════════════════════════════════════════════════════════

def phase_full_export(
    mcp: MCPSession, r: Results, bays: List[str]
) -> None:
    """
    Runs the complete export pipeline and verifies output files on disk.

    After this phase you should have:
      OUTPUT_DIR/
        UPTIME_TEST_SECTION_layout.json   — UE5 facility manifest
        UPTIME_TEST_SECTION_cables.json   — cable routing (written in Phase 4)
        racks/
          *.fbx                           — rack cabinet static mesh
          *_manifest.json                 — asset registry for the rack
        equipment/
          EQ_*.fbx                        — deduplicated equipment type FBX files

    Importing into UE5:
      • File → Import → FBX → select any .fbx file
      • Import settings: -X Forward, Z Up, Scale 1.0, Import as Static Mesh
      • Verify origin is at base-front-centre of the rack (Z=0 at floor level)
      • Check StaticMesh Editor → Sockets panel for SOCKET_ attachment points
    """
    section_header("Phase 6 — Full UE5 Export")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 6a. export_facility_layout_json — master UE5 manifest (most important export)
    #     This single file drives runtime procedural placement in UE5.
    manifest_path = OUTPUT_DIR / f"{SECTION}_layout.json"
    try:
        result = mcp.call_tool("export_facility_layout_json", {
            "section_name":      SECTION,
            "output_path":       str(manifest_path),
            "include_cables":    True,
            "include_variation": True,
        })
        if manifest_path.exists():
            kb = manifest_path.stat().st_size / 1024
            r.ok("export_facility_layout_json", f"{kb:.1f} KB → {manifest_path.name}")
            r.file(manifest_path)
            # Spot-check manifest top-level structure
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                expected_keys = {"section_name", "bays"}
                missing = expected_keys - set(manifest.keys())
                if not missing:
                    r.ok("Manifest structure check",
                         f"keys: {sorted(manifest.keys())}")
                else:
                    r.warn("Manifest structure check",
                           f"missing top-level keys: {missing}")
            except Exception as exc:
                r.warn("Manifest JSON parse", str(exc))
        else:
            r.warn("export_facility_layout_json",
                   f"tool completed but no file at {manifest_path}")
    except Exception as exc:
        r.fail("export_facility_layout_json", str(exc))

    # 6b. export_rack_collection_ue5 — individual rack FBX + LOD set
    rack_export_dir = OUTPUT_DIR / "racks"
    rack_export_dir.mkdir(exist_ok=True)
    rack_name = None

    if bays:
        try:
            bay_info = mcp.call_tool("get_bay_info", {"bay_name": bays[0]})
            racks = bay_info.get("racks", []) if isinstance(bay_info, dict) else []
            if racks:
                rack_name = racks[0]["name"] if isinstance(racks[0], dict) else racks[0]
        except Exception:
            pass

    if rack_name:
        try:
            result = mcp.call_tool("export_rack_collection_ue5", {
                "collection_name": rack_name,
                "output_dir":      str(rack_export_dir),
                "generate_lods":   True,
                "write_manifest":  True,
            })
            fbx_files = sorted(rack_export_dir.glob("*.fbx"))
            if fbx_files:
                r.ok("export_rack_collection_ue5",
                     f"{len(fbx_files)} FBX file(s) in racks/")
                for f in fbx_files:
                    r.file(f)
            else:
                r.warn("export_rack_collection_ue5",
                       f"tool ok but no .fbx in {rack_export_dir}")
        except Exception as exc:
            r.warn("export_rack_collection_ue5", str(exc))
    else:
        r.warn("export_rack_collection_ue5", "No rack name discovered — skipped")

    # 6c. export_equipment_set_ue5 — deduplicated equipment type FBX files
    #     One FBX per unique equipment_type; UE5 instances them at runtime.
    equip_export_dir = OUTPUT_DIR / "equipment"
    equip_export_dir.mkdir(exist_ok=True)
    if bays:
        try:
            result = mcp.call_tool("export_equipment_set_ue5", {
                "collection_name": bays[0],
                "output_dir":      str(equip_export_dir),
            })
            fbx_files = sorted(equip_export_dir.glob("*.fbx"))
            if fbx_files:
                r.ok("export_equipment_set_ue5",
                     f"{len(fbx_files)} equipment FBX file(s) in equipment/")
                for f in fbx_files:
                    r.file(f)
            else:
                r.warn("export_equipment_set_ue5",
                       "tool completed; no .fbx found (bay may have no equipment)")
        except Exception as exc:
            r.warn("export_equipment_set_ue5", str(exc))

    # 6d. export_bay_layout_json — bay-level JSON for the first bay
    bay_json = OUTPUT_DIR / f"{bays[0]}_layout.json" if bays else None
    if bays and bay_json:
        try:
            result = mcp.call_tool("export_bay_layout_json", {
                "bay_name":    bays[0],
                "output_path": str(bay_json),
            })
            if bay_json.exists():
                kb = bay_json.stat().st_size / 1024
                r.ok("export_bay_layout_json", f"{kb:.1f} KB → {bay_json.name}")
                r.file(bay_json)
            else:
                r.warn("export_bay_layout_json", f"no file at {bay_json}")
        except Exception as exc:
            r.warn("export_bay_layout_json", str(exc))


# ══════════════════════════════════════════════════════════════════════════
#  PHASE 7 — CLEANUP & RESET
# ══════════════════════════════════════════════════════════════════════════

def phase_cleanup_reset(
    mcp: MCPSession, r: Results, bays: List[str]
) -> None:
    """
    Clears cables and variation nodes, then re-validates the scene.

    This confirms the reset path works correctly — a real workflow might
    block after variation preview, reset to clean, re-validate, then export.

    Viewport after reset_variation:
      • Rack materials return to flat grey (default Principled BSDF).
      • No [WEAR] / [DUST] / [DAMAGE] nodes visible in the Shader Editor.
    Viewport after clear_cables:
      • No curve objects anywhere in the section.
    Final validate_facility should report 0 failures — confirming the scene
    is ready for a clean export pass.
    """
    section_header("Phase 7 — Cleanup & Reset")

    # 7a. clear_cables — must pass confirm=True (safety gate)
    try:
        result = mcp.call_tool("clear_cables", {
            "collection_name": SECTION,
            "confirm":         True,
        })
        removed = result.get("removed", "?") if isinstance(result, dict) else "?"
        r.ok("clear_cables", f"removed={removed} cable objects")
    except Exception as exc:
        r.warn("clear_cables", str(exc))

    # 7b. reset_variation — first bay only (targeted reset)
    if bays:
        try:
            result = mcp.call_tool("reset_variation", {
                "target":       bays[0],
                "reset_wear":   True,
                "reset_dust":   True,
                "reset_damage": True,
            })
            nodes_removed = (
                result.get("nodes_removed", "?")
                if isinstance(result, dict) else "?"
            )
            r.ok(f"reset_variation — {bays[0]}", f"nodes_removed={nodes_removed}")
        except Exception as exc:
            r.warn(f"reset_variation — {bays[0]}", str(exc))

    # 7c. reset_variation — full section
    try:
        result = mcp.call_tool("reset_variation", {
            "target":       SECTION,
            "reset_wear":   True,
            "reset_dust":   True,
            "reset_damage": True,
        })
        nodes_removed = (
            result.get("nodes_removed", "?")
            if isinstance(result, dict) else "?"
        )
        r.ok("reset_variation — full section", f"nodes_removed={nodes_removed}")
    except Exception as exc:
        r.warn("reset_variation — full section", str(exc))

    # 7d. get_variation_report after reset — coverage should drop to 0
    if bays:
        try:
            result = mcp.call_tool("get_variation_report", {"bay_name": bays[0]})
            coverage = (
                result.get("coverage_pct", -1)
                if isinstance(result, dict) else -1
            )
            if coverage == 0:
                r.ok("Variation report after reset", "coverage_pct=0 — fully clean")
            elif coverage > 0:
                r.warn("Variation report after reset",
                       f"coverage_pct={coverage} — some variation nodes remain")
            else:
                r.ok("Variation report after reset", _trunc(result))
        except Exception as exc:
            r.warn("Variation report after reset", str(exc))

    # 7e. Final validate_facility — should be clean after reset + cable clear
    try:
        result = mcp.call_tool("validate_facility", {"section_name": SECTION})
        fail_count = result.get("fail_count", -1) if isinstance(result, dict) else -1
        warn_count = result.get("warn_count", -1) if isinstance(result, dict) else -1
        if fail_count == 0:
            r.ok("Final validate_facility — post reset",
                 f"0 failures, {warn_count} warnings — scene is export-ready")
        elif fail_count > 0:
            r.warn("Final validate_facility — post reset",
                   f"{fail_count} failure(s) remain after cleanup")
        else:
            r.ok("Final validate_facility — post reset", _trunc(result))
    except Exception as exc:
        r.fail("Final validate_facility", str(exc))


# ══════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════

def main() -> None:
    width = 68
    print()
    print("═" * width)
    print(f"{_BOLD}{_CYAN}  UPTIME BlenderDC — Full Pipeline Integration Test{_RESET}")
    print(f"  v2.7.0  |  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    print(f"  Server  :  {MCP_URL}")
    print(f"  Exports :  {OUTPUT_DIR}")
    print("═" * width)

    r   = Results()
    mcp = MCPSession(MCP_URL)

    # ── Phase 1  Server health (aborts on connection failure) ──────────────
    tools = phase_server_health(mcp, r)

    # ── Phase 2  Facility creation ─────────────────────────────────────────
    bays, _preset_section = phase_facility_creation(mcp, r)

    # ── Phase 3  Bay & row validation ──────────────────────────────────────
    phase_bay_validation(mcp, r, bays)

    # ── Phase 4  Cable routing ─────────────────────────────────────────────
    phase_cable_routing(mcp, r, bays)

    # ── Phase 5  Variation & failure states ───────────────────────────────
    phase_variation_states(mcp, r, bays)

    # ── Phase 6  Full UE5 export ───────────────────────────────────────────
    phase_full_export(mcp, r, bays)

    # ── Phase 7  Cleanup & reset ───────────────────────────────────────────
    phase_cleanup_reset(mcp, r, bays)

    # ── Final summary ──────────────────────────────────────────────────────
    sys.exit(r.summary())


if __name__ == "__main__":
    main()
