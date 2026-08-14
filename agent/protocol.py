"""Minimal MCP stdio (JSON-RPC). Stdlib only — no fastmcp install required."""
from __future__ import annotations

import json
import sys
from typing import Any, Callable

try:
    from .util import capped_json
except ImportError:
    from util import capped_json

PROTOCOL = "2024-11-05"


def _read_stdio_message() -> dict[str, Any] | None:
    header = sys.stdin.buffer.readline()
    if not header:
        return None
    # MCP stdio uses one compact JSON-RPC message per line.
    if header.lstrip().startswith(b"{"):
        try:
            value = json.loads(header.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
    # Accept legacy Content-Length framing from older hosts.
    headers = {}
    line = header
    while line not in (b"", b"\r\n", b"\n"):
        try:
            k, v = line.decode("utf-8").split(":", 1)
            headers[k.strip().lower()] = v.strip()
        except Exception:
            pass
        line = sys.stdin.buffer.readline()
        if not line:
            break
    try:
        n = int(headers.get("content-length") or 0)
    except ValueError:
        return {}
    if n <= 0:
        return None
    body = sys.stdin.buffer.read(n)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _write_stdio_message(msg: dict[str, Any]) -> None:
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def serve(tools: dict[str, tuple[dict[str, Any], Callable[..., Any]]]) -> None:
    """
    tools: name -> (list_schema, handler)
    handler(**kwargs) -> dict
    """
    while True:
        msg = _read_stdio_message()
        if msg is None:
            break
        if not msg or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            _write_stdio_message({
                "jsonrpc": "2.0",
                "id": msg.get("id") if msg else None,
                "error": {"code": -32600, "message": "Invalid Request"},
            })
            continue
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            requested = params.get("protocolVersion")
            _write_stdio_message({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": requested if isinstance(requested, str) else PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "cachelayer-agent", "version": "1.0.0"},
                    "instructions": (
                        "Local coding-agent tools. "
                        "verify_edit after code edits. "
                        "run_affected_tests instead of the full suite. "
                        "debug_failure ONCE on a traceback instead of grepping. "
                        "Do not call these before every Read/Grep."
                    ),
                },
            })
            continue
        if method == "notifications/initialized":
            continue
        if method == "ping":
            _write_stdio_message({"jsonrpc": "2.0", "id": mid, "result": {}})
            continue
        if method == "tools/list":
            _write_stdio_message({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"tools": [schema for schema, _ in tools.values()]},
            })
            continue
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in tools:
                _write_stdio_message({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": f"unknown tool {name}"}],
                    },
                })
                continue
            if not isinstance(args, dict):
                _write_stdio_message({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32602, "message": "Tool arguments must be an object"},
                })
                continue
            try:
                result = tools[name][1](**args)
                text = capped_json(result)
                _write_stdio_message({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"content": [{"type": "text", "text": text}]},
                })
            except TypeError as exc:
                _write_stdio_message({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32602, "message": str(exc)},
                })
            except Exception as exc:
                _write_stdio_message({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    },
                })
            continue
        if method in ("shutdown",):
            _write_stdio_message({"jsonrpc": "2.0", "id": mid, "result": {}})
            continue
        if method == "notifications/cancelled":
            continue
        if method == "exit":
            break
        if mid is not None:
            _write_stdio_message({
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })
