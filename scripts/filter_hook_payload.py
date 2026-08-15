#!/usr/bin/env python3
"""Bound and sanitize read/search hook payloads before network use."""
from __future__ import annotations

import json
import re
import sys
from typing import Any

MAX_BODY = 256 * 1024
SECRET_KEY = re.compile(
    r"(?:^|_)(?:authorization|api_?key|token|secret|password|passwd|private_?key|credential)(?:$|_)",
    re.I,
)
SECRET_PATH = re.compile(
    r'(?:^|[/\\"])(?:\.env(?:\.[^/\\"]*)?|credentials?(?:\.[^/\\"]*)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.[^/\\"]*)?|[^/\\"]*\.(?:pem|p12|pfx|key))(?:$|[/\\"])',
    re.I,
)
SECRET_VALUE = re.compile(
    r"(?i)((?:bearer\s+|(?:api[_-]?key|token|secret|password|passwd)=))[A-Za-z0-9._~+/=-]{4,}|"
    r"\b(?:sk|pk|rk|clct|ghp|github_pat|xox[baprs])_[A-Za-z0-9_-]{12,}\b|"
    r"\b(?:AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.S,
)
READ_TOOLS = re.compile(
    r"^(?:read|grep|glob|search|websearch|webfetch|"
    r"mcp__.+__(?:read|grep|glob|search|fetch)[A-Za-z0-9_]*)$",
    re.I,
)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SECRET_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]",
            value,
        )
    return value


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        return 3
    try:
        body = json.loads(raw)
    except Exception:
        return 3
    if not isinstance(body, dict):
        return 3
    tool_name = str(body.get("tool_name") or body.get("toolName") or body.get("tool") or "")
    if not READ_TOOLS.fullmatch(tool_name):
        return 3
    tool_input = body.get("tool_input") or body.get("toolInput") or body.get("input") or {}
    try:
        if SECRET_PATH.search(json.dumps(tool_input, default=str)):
            return 3
    except Exception:
        return 3
    sanitized = json.dumps(_redact(body), separators=(",", ":"), default=str)
    if len(sanitized.encode()) > MAX_BODY:
        return 3
    sys.stdout.write(sanitized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
