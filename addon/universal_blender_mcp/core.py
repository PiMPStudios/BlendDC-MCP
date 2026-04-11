"""
Shared FastMCP instance, thread-safety decorator, logging, middleware, and ASGI app factory.

All tool modules import from here:
    from core import mcp, thread_safe, _log

The single `mcp` instance is shared via Python's module cache — no duplicate registrations.
"""

import bpy
import json
import threading
import functools
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

# ── Shared MCP instance ────────────────────────────────────────────────────
mcp = FastMCP("blender-universal")

# ── Logging ────────────────────────────────────────────────────────────────
_LOG_PREFIX  = "[BlenderMCP]"
_MAX_LOG_LEN = 400


def _log(msg: str) -> None:
    import time as _time
    ts = _time.strftime("%H:%M:%S")
    print(f"{_LOG_PREFIX} {ts} {msg}", flush=True)


def _parse_sse(raw: bytes) -> list:
    """Extract JSON objects from an SSE-formatted byte string (data: ... lines)."""
    results = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                results.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return results


def _fmt_json(obj, max_len: int = _MAX_LOG_LEN) -> str:
    s = json.dumps(obj)
    return s if len(s) <= max_len else s[:max_len] + " …"


def _log_rpc(direction: str, data: dict) -> None:
    """Log a single parsed JSON-RPC object."""
    if direction == ">>":
        method  = data.get("method", "?")
        params  = data.get("params") or {}
        req_id  = data.get("id", "–")
        if method == "tools/call":
            tool_name = params.get("name", "?")
            args      = params.get("arguments", {})
            _log(f"MCP >> [{req_id}] tools/call  tool={tool_name}  args={_fmt_json(args)}")
        elif method == "tools/list":
            _log(f"MCP >> [{req_id}] tools/list")
        elif method == "initialize":
            client = (params.get("clientInfo") or {})
            _log(f"MCP >> [{req_id}] initialize  client={client.get('name','?')} {client.get('version','')}")
        elif method and method.startswith("notifications/"):
            reason       = params.get("reason", "")
            cancelled_id = params.get("requestId", "")
            _log(f"MCP >> NOTIFY  {method}  requestId={cancelled_id}  reason={reason}")
        else:
            _log(f"MCP >> [{req_id}] {method}  {_fmt_json(params)}")
    else:
        req_id = data.get("id", "–")
        if "error" in data:
            _log(f"MCP << [{req_id}] ERROR  {_fmt_json(data['error'])}")
        elif "result" in data:
            result = data["result"]
            if isinstance(result, dict) and "tools" in result:
                names = [t.get("name", "?") for t in result["tools"]]
                _log(f"MCP << [{req_id}] tools/list  ({len(names)} tools): {', '.join(names)}")
            else:
                _log(f"MCP << [{req_id}] result  {_fmt_json(result)}")
        else:
            _log(f"MCP << [{req_id}] {_fmt_json(data)}")


def _log_body(direction: str, raw: bytes) -> None:
    """Parse and pretty-print an MCP request/response body (JSON or SSE)."""
    if not raw or not raw.strip():
        return

    try:
        data = json.loads(raw)
        _log_rpc(direction, data)
        return
    except json.JSONDecodeError:
        pass

    sse_objects = _parse_sse(raw)
    if sse_objects:
        for obj in sse_objects:
            _log_rpc(direction, obj)
        return

    text = raw.decode("utf-8", errors="replace").strip()
    _log(f"MCP {direction} (unparsed): {text[:_MAX_LOG_LEN]}")


# ── ASGI middleware ────────────────────────────────────────────────────────

class _StripOutputSchemaMiddleware:
    """
    ASGI middleware that removes 'outputSchema' from tools/list responses.

    MCP spec 2025-06-18 added outputSchema, but many clients (LM Studio, etc.)
    don't implement it and immediately cancel the session when they see it.
    Stripping it makes the tools/list response look like 2024-11-05 format,
    which every client understands.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def _send(msg):
            if msg.get("type") == "http.response.body":
                msg = dict(msg)
                msg["body"] = self._strip(msg.get("body", b""))
            await send(msg)

        await self.app(scope, receive, _send)

    @staticmethod
    def _strip(chunk: bytes) -> bytes:
        if b"outputSchema" not in chunk:
            return chunk

        try:
            data  = json.loads(chunk)
            tools = (data.get("result") or {}).get("tools")
            if isinstance(tools, list):
                for tool in tools:
                    tool.pop("outputSchema", None)
            return json.dumps(data).encode("utf-8")
        except (json.JSONDecodeError, AttributeError):
            pass

        lines = []
        for line in chunk.decode("utf-8", errors="replace").splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith("data:"):
                payload = stripped[5:].strip()
                try:
                    data   = json.loads(payload)
                    result = (data.get("result") or {})
                    tools  = result.get("tools")
                    if isinstance(tools, list):
                        for tool in tools:
                            tool.pop("outputSchema", None)
                        line = f"data: {json.dumps(data)}\n"
                except (json.JSONDecodeError, AttributeError):
                    pass
            lines.append(line)
        return "".join(lines).encode("utf-8")


class _MCPDebugMiddleware:
    """
    ASGI middleware that logs MCP JSON-RPC request/response bodies in real time.

    Requests are logged when fully received.
    Responses are logged per-chunk so SSE events show their actual send timestamp.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        req_chunks: list[bytes] = []

        async def _receive():
            msg = await receive()
            if msg.get("type") == "http.request":
                req_chunks.append(msg.get("body", b""))
                if not msg.get("more_body", False):
                    _log_body(">>", b"".join(req_chunks))
            return msg

        async def _send(msg):
            if msg.get("type") == "http.response.body":
                chunk = msg.get("body", b"")
                if chunk and chunk.strip():
                    _log_body("<<", chunk)
            await send(msg)

        await self.app(scope, _receive, _send)


# ── Thread-safety decorator ────────────────────────────────────────────────

def thread_safe(func):
    """Run a function on Blender's main thread and return its result."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        arg_summary = ", ".join(
            [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
        )
        _log(f"TOOL >> {func.__name__}({arg_summary})")

        if threading.current_thread() is threading.main_thread():
            try:
                result = func(*args, **kwargs)
                _log(f"TOOL << {func.__name__} OK: {result!r}")
                return result
            except Exception as exc:
                _log(f"TOOL << {func.__name__} ERROR: {exc}")
                raise

        result = [None]
        error  = [None]
        done   = threading.Event()

        def _run():
            try:
                result[0] = func(*args, **kwargs)
            except Exception as exc:
                error[0] = exc
            finally:
                done.set()

        bpy.app.timers.register(_run, first_interval=0.0)

        if not done.wait(timeout=10.0):
            _log(f"TOOL << {func.__name__} TIMEOUT")
            raise TimeoutError(f"Blender main thread timeout in {func.__name__}")

        if error[0] is not None:
            _log(f"TOOL << {func.__name__} ERROR: {error[0]}")
            raise error[0]

        _log(f"TOOL << {func.__name__} OK: {result[0]!r}")
        return result[0]
    return wrapper


# ── ASGI app factory ───────────────────────────────────────────────────────

def get_app():
    """
    Return the FastMCP ASGI application with compatibility fixes and debug logging.

    Stack (outermost → innermost):
      _MCPDebugMiddleware          – real-time request/response logging
      _StripOutputSchemaMiddleware – removes outputSchema from tools/list
      mcp.streamable_http_app()    – Streamable HTTP transport (POST /mcp)
    """
    if hasattr(mcp, "streamable_http_app"):
        transport = mcp.streamable_http_app()
    elif hasattr(mcp, "http_app"):
        transport = mcp.http_app(stateless_http=True)
    else:
        _log("WARNING: falling back to SSE transport (mcp package is outdated)")
        transport = mcp.sse_app()
    return _MCPDebugMiddleware(_StripOutputSchemaMiddleware(transport))
