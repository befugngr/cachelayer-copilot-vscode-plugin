#!/usr/bin/env python3
"""Cheap post-edit CRITIC for Cursor/Claude/Codex/Copilot hooks. Lint the edited file only. Fail-open."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from critic import verify_edit
from util import is_code_path

EMPTY = "{}"

# VS Code ignores hook matchers and runs every hook on every tool, so gate here too.
_EDIT_HINTS = ("edit", "write", "patch", "replace", "create")
_NEVER = ("todo", "plan", "search", "fetch", "read", "grep", "glob")

_PATCH_FILE_RE = re.compile(r"^\*\*\*\s+(?:Add|Update)\s+File:\s*(.+?)\s*$", re.MULTILINE)


def _is_edit_tool(name: str) -> bool:
    n = name.strip().lower()
    if not n or n.startswith("mcp"):
        return False
    if any(bad in n for bad in _NEVER):
        return False
    return any(hint in n for hint in _EDIT_HINTS)


def _tool_input(payload: dict) -> dict | str:
    for key in ("tool_input", "toolInput", "input", "arguments"):
        v = payload.get(key)
        if isinstance(v, (dict, str)):
            return v
    return {}


def _paths_from_patch(text: str) -> list[str]:
    return [m.strip().strip('"') for m in _PATCH_FILE_RE.findall(text or "")]


def _extract_paths(payload: dict) -> list[str]:
    found: list[str] = []
    inp = _tool_input(payload)

    # Codex apply_patch sends the whole patch as a command string.
    if isinstance(inp, str):
        found.extend(_paths_from_patch(inp))
        inp = {}
    elif isinstance(inp.get("command"), str):
        found.extend(_paths_from_patch(inp["command"]))

    sources: list[dict] = [payload]
    if isinstance(inp, dict):
        sources.append(inp)
    for src in sources:
        for key in (
            "file_path", "filePath", "path", "target_file", "targetFile",
            "notebook_path", "notebookPath", "file", "uri",
        ):
            v = src.get(key)
            if isinstance(v, str) and v:
                found.append(v)
        files = src.get("files")
        if isinstance(files, list):
            for item in files:
                if isinstance(item, str) and item:
                    found.append(item)
                elif isinstance(item, dict):
                    for key in ("path", "file_path", "filePath", "uri"):
                        v = item.get(key)
                        if isinstance(v, str) and v:
                            found.append(v)

    out: list[str] = []
    for p in found:
        p = p.removeprefix("file://")
        if p and is_code_path(p) and p not in out:
            out.append(p)
    return out


def _line_range(payload: dict) -> list[int] | None:
    inp = _tool_input(payload)
    if not isinstance(inp, dict):
        return None
    for a, b in (("start_line", "end_line"), ("startLine", "endLine")):
        if isinstance(inp.get(a), int) and isinstance(inp.get(b), int):
            return [inp[a], inp[b]]
    return None


def _emit(msg: str, payload: dict) -> None:
    fmt = (os.environ.get("CACHELAYER_HOOK_FORMAT") or "").strip().lower()
    if not fmt:
        cursor_keys = ("workspace_roots", "conversation_id", "generation_id")
        fmt = "cursor" if any(k in payload for k in cursor_keys) else "nested"
    if fmt == "cursor":
        print(json.dumps({"additional_context": msg}))
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": msg,
        }
    }))


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        print(EMPTY)
        return
    try:
        payload = json.loads(raw)
    except Exception:
        print(EMPTY)
        return
    if not isinstance(payload, dict):
        print(EMPTY)
        return

    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    if isinstance(tool_name, str) and tool_name and not _is_edit_tool(tool_name):
        print(EMPTY)
        return

    paths = _extract_paths(payload)
    if not paths:
        print(EMPTY)
        return

    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    try:
        result = verify_edit(
            paths=paths,
            line_range=_line_range(payload) if len(paths) == 1 else None,
            run_tests=False,
            hook=True,
            cwd=cwd,
        )
    except Exception:
        print(EMPTY)
        return

    if result.get("skipped") or result.get("ok") or not result.get("blocked"):
        print(EMPTY)
        return

    bits = []
    for g in result.get("gates") or []:
        if g.get("ok") or g.get("skipped"):
            continue
        if g.get("output"):
            bits.append(f"{g.get('name')}:\n{g['output']}")
    if not bits:
        print(EMPTY)
        return

    _emit(
        "CRITIC (local, no extra LLM call): type/lint errors in the file you just edited. "
        "Fix these before more edits or tests.\n\n" + "\n\n".join(bits),
        payload,
    )


if __name__ == "__main__":
    main()
