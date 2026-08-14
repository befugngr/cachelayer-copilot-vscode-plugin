#!/usr/bin/env python3
"""Cheap post-edit CRITIC for Cursor/Claude hooks. Lint the edited file only. Fail-open."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from critic import verify_edit
from util import is_code_path


def _extract_path(payload: dict) -> str | None:
    for key in ("file_path", "filePath", "path"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
    inp = payload.get("tool_input") or payload.get("toolInput") or payload.get("input") or {}
    if isinstance(inp, dict):
        for key in ("path", "file_path", "filePath", "target_file"):
            v = inp.get(key)
            if isinstance(v, str) and v:
                return v
    return None


def _line_range(payload: dict) -> list[int] | None:
    inp = payload.get("tool_input") or payload.get("toolInput") or payload.get("input") or {}
    if not isinstance(inp, dict):
        return None
    for a, b in (("start_line", "end_line"), ("startLine", "endLine")):
        if isinstance(inp.get(a), int) and isinstance(inp.get(b), int):
            return [inp[a], inp[b]]
    return None


def main() -> None:
    raw = sys.stdin.read()
    empty = "{}"
    if not raw.strip():
        print(empty)
        return
    try:
        payload = json.loads(raw)
    except Exception:
        print(empty)
        return
    if not isinstance(payload, dict):
        print(empty)
        return

    path = _extract_path(payload)
    if not path or not is_code_path(path):
        print(empty)
        return

    try:
        result = verify_edit(
            paths=[path],
            line_range=_line_range(payload),
            run_tests=False,
            hook=True,
        )
    except Exception:
        print(empty)
        return

    if result.get("skipped") or result.get("ok") or not result.get("blocked"):
        print(empty)
        return

    bits = []
    for g in result.get("gates") or []:
        if g.get("ok") or g.get("skipped"):
            continue
        if g.get("output"):
            bits.append(f"{g.get('name')}:\n{g['output']}")
    if not bits:
        print(empty)
        return
    msg = (
        "CRITIC (local, no extra LLM call): type/lint errors in the file you just edited. "
        "Fix these before more edits or tests.\n\n" + "\n\n".join(bits)
    )
    if os.environ.get("CACHELAYER_HOOK_FORMAT") == "claude" or os.environ.get("CLAUDE_PLUGIN_ROOT"):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": msg,
            }
        }))
    else:
        print(json.dumps({"additional_context": msg}))


if __name__ == "__main__":
    main()
