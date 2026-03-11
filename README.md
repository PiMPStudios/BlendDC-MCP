# Universal Blender MCP

Full MCP server for Blender. Works with Continue.dev, LM Studio, Cursor, Claude Desktop, Open WebUI, AnythingLLM — no hacks required.

## Features
- Auto-installs dependencies on first start
- Full JSON-RPC handshake (initialize + notifications/initialized)
- Thread-safe Blender API calls
- 50+ tools (expanding)

## Install
1. Download ZIP or clone repo
2. In Blender: Edit → Preferences → Add-ons → Install... → select `addon/blender_mcp.py`
3. Enable the addon
4. N-Panel → MCP tab → Start Server

Server runs on http://localhost:8000/mcp

## Clients
- Continue.dev: Add to config.yaml: `mcpServers: [{name: "Blender", type: "streamable-http", url: "http://127.0.0.1:8000/mcp"}]`
- LM Studio: mcp.json → `"url": "http://127.0.0.1:8000/mcp", "type": "remote"`

MIT License — contributions welcome!
