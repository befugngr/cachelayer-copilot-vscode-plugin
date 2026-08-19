#!/usr/bin/env python3
"""Fail-open CRITIC/TIA/debug hook for Copilot: coherent gate after code edits."""
from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from verify_edit import verify_edit
from affected_tests import run_affected_tests
from debug_failure import debug_failure
from process_util import is_code_path

EMPTY = "{}"

# VS Code can ignore matchers, so aliases and deny-lists are enforced here.
_EDIT_ALIASES = {
    "applypatch", "apply_patch", "edit", "editfile", "edit_file", "edit_files",
    "writefile", "write_file", "write", "multiedit", "multi_edit", "notebookedit",
    "notebook_edit", "create", "createfile", "create_file", "create_directory",
    "str_replace_editor", "replace_string_in_file", "insert_edit_into_file",
    "multi_replace_string_in_file", "patch_file",
}
_NEVER = ("todo", "plan", "search", "fetch", "read", "grep", "glob", "view", "list", "mcp", "shell", "bash")
_DEDUP_SECONDS = 30

_FULL_SUITE_RE = re.compile(
    r"(?:^|[\s;&|])(?:npm\s+(?:(?:run\s+)?test)|npx\s+(?:jest|vitest|mocha)(?!\s+\S)|"
    r"yarn\s+test|pnpm\s+test|python(?:3)?\s+-m\s+pytest(?:\s*$)|pytest(?:\s*$)|"
    r"mvn\s+test|gradle(?:w)?\s+test)(?:\s|$)",
    re.I,
)
_PATCH_FILE_RE = re.compile(r"^\*\*\*\s+(?:Add|Update)\s+File:\s*(.+?)\s*$", re.MULTILINE)


_TERMINAL_ALIASES = {
    "run_in_terminal", "runinterminal", "bash", "shell", "powershell",
    "execute_command", "run_command", "runcommand",
    "runtests", "run_tests", "create_and_run_task", "run_task",
}


def _is_edit_tool(name: str) -> bool:
    n = name.strip().lower().replace("-", "_")
    compact = n.replace("_", "")
    if not n:
        return False
    if any(bad in n for bad in _NEVER):
        return False
    return n in _EDIT_ALIASES or compact in _EDIT_ALIASES


def _hook_event(payload: dict) -> str:
    return str(payload.get("hook_event_name") or payload.get("hookEventName") or "").lower()


def _command_text(inp: dict | str) -> str:
    if isinstance(inp, str):
        return inp
    if isinstance(inp, dict):
        return str(inp.get("command") or inp.get("cmd") or inp.get("input") or "")
    return ""


def _is_terminal_tool(name: str) -> bool:
    n = name.strip().lower().replace("-", "_")
    compact = n.replace("_", "")
    return n in _TERMINAL_ALIASES or compact in _TERMINAL_ALIASES


def _tool_input(payload: dict) -> dict | str:
    for key in ("tool_input", "toolInput", "input", "arguments"):
        v = payload.get(key)
        if isinstance(v, (dict, str)):
            return v
    return {}


def normalize_payload(payload: dict) -> dict:
    """Normalize Cursor, Claude, Codex, Copilot CLI, and VS Code hook shapes."""
    nested = payload.get("tool") if isinstance(payload.get("tool"), dict) else {}
    name = (
        payload.get("tool_name") or payload.get("toolName")
        or nested.get("name") or payload.get("name") or ""
    )
    inp = _tool_input(payload)
    if not inp and nested:
        inp = nested.get("input") or nested.get("arguments") or {}
    cwd = payload.get("cwd") or payload.get("workspaceRoot") or payload.get("workspace_root")
    if not isinstance(cwd, str):
        roots = payload.get("workspace_roots")
        cwd = roots[0] if isinstance(roots, list) and roots and isinstance(roots[0], str) else None
    cycle = next((
        payload.get(key) for key in (
            "edit_cycle_id", "editCycleId", "generation_id", "turn_id",
            "turnId",
        ) if isinstance(payload.get(key), str) and payload.get(key)
    ), "")
    event = next((
        payload.get(key) for key in (
            "tool_call_id", "toolCallId", "call_id", "callId",
            "invocation_id", "invocationId", "event_id",
        ) if isinstance(payload.get(key), str) and payload.get(key)
    ), "")
    full_gate = True
    return {
        "tool_name": name if isinstance(name, str) else "",
        "tool_input": inp,
        "cwd": cwd,
        "cycle_id": cycle,
        "event_id": event,
        "full_gate": full_gate,
    }


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


def _deduplicated(root: Path, normalized: dict, paths: list[str]) -> bool:
    snapshots: list[str] = []
    for raw in paths[:100]:
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        try:
            stat = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                digest.update(stream.read(256_000))
            snapshots.append(
                f"{raw}:{stat.st_mtime_ns}:{stat.st_size}:{digest.hexdigest()}"
            )
        except OSError:
            snapshots.append(f"{raw}:missing")
    identity = normalized["event_id"] or json.dumps({
        "tool": normalized["tool_name"].lower(),
        "paths": paths,
        "cycle": normalized["cycle_id"],
        "input": normalized["tool_input"],
        "snapshots": snapshots,
    }, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    root_key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:20]
    path = cache / "cachelayer" / "critic-hook" / root_key / "state.json"
    now = int(__import__("time").time())
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
        if prior.get("digest") == digest and now - int(prior.get("at", 0)) <= _DEDUP_SECONDS:
            return True
    except (OSError, ValueError, TypeError):
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"digest": digest, "at": now}, stream)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    except OSError:
        pass
    return False


def _line_range(payload: dict) -> list[int] | None:
    inp = _tool_input(payload)
    if not isinstance(inp, dict):
        return None
    for a, b in (("start_line", "end_line"), ("startLine", "endLine")):
        if isinstance(inp.get(a), int) and isinstance(inp.get(b), int):
            return [inp[a], inp[b]]
    return None


def _failure_blob(payload: dict, inp: dict | str) -> str:
    chunks: list[str] = []
    for key in ("tool_response", "toolResponse", "tool_output", "toolOutput", "output", "result", "error"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            chunks.append(v.strip())
        elif isinstance(v, dict):
            chunks.append(json.dumps(v, default=str)[:8000])
    if isinstance(inp, dict):
        for key in ("stdout", "stderr", "output", "error"):
            v = inp.get(key)
            if isinstance(v, str) and v.strip():
                chunks.append(v.strip())
    return "\n".join(chunks)


def _exit_code(payload: dict, inp: dict | str) -> int | None:
    sources: list[Any] = [payload]
    if isinstance(inp, dict):
        sources.append(inp)
    resp = payload.get("tool_response") or payload.get("toolResponse")
    if isinstance(resp, dict):
        sources.append(resp)
    for src in sources:
        for key in ("exit_code", "exitCode", "status_code", "statusCode", "status", "code"):
            v = src.get(key)
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.lstrip("-").isdigit():
                return int(v)
    return None


def _terminal_failed(payload: dict, inp: dict | str, blob: str) -> bool:
    code = _exit_code(payload, inp)
    if code not in (None, 0):
        return True
    low = blob.lower()
    if "cancelled" in low and "error ts" not in low:
        return False
    return bool(re.search(
        r"error TS\d+|npm ERR!|ERR!|AssertionError|FAILED|Traceback \(most recent call last\)|"
        r"Could not read package\.json|is not assignable to",
        blob,
    ))


def _verify_summary(result: dict) -> str:
    if result.get("skipped"):
        return f"CacheLayer verify_edit skipped: {result.get('reason') or 'no checker'}"
    tests = next((g for g in (result.get("gates") or []) if g.get("name") == "tests"), {})
    tia = tests.get("tia") or {}
    lines = [
        f"CacheLayer verify_edit mode={result.get('mode')} ok={result.get('ok')} "
        f"blocked={result.get('blocked')} tests_ran={result.get('tests_ran')}",
    ]
    if tia:
        lines.append(
            f"TIA selected={tia.get('selected')} skipped={tia.get('skipped_estimate')} "
            f"runner={tia.get('runner')}"
        )
    elif tests.get("skipped"):
        lines.append(f"TIA skipped: {tests.get('reason') or tests.get('install') or 'not run'}")
    feedback = result.get("feedback") or {}
    if result.get("blocked"):
        bits = []
        for g in result.get("gates") or []:
            if g.get("ok") or g.get("skipped"):
                continue
            if g.get("output"):
                bits.append(f"{g.get('name')}:\n{g['output']}")
        if bits:
            lines.extend(bits)
        lines.append(
            f"Corrective protocol: {feedback.get('instruction') or 'Make one coherent corrective edit.'}"
        )
        lines.append(
            f"cycle_id={feedback.get('cycle_id', '')} "
            f"attempt={feedback.get('attempt', 0)}/{feedback.get('max_retries', 0)} "
            f"next_action={feedback.get('action', 'stop_and_report')}"
        )
    return "\n".join(lines)


def _run_tia_instead_of_full_suite(payload: dict, normalized: dict) -> None:
    cmd = _command_text(normalized["tool_input"])
    if not _FULL_SUITE_RE.search(cmd):
        print(EMPTY)
        return
    cwd = normalized["cwd"]
    try:
        affected = run_affected_tests(changed_files=None, cwd=cwd, timeout=40)
    except Exception as exc:
        _emit(
            f"CacheLayer TIA error (full suite blocked): {type(exc).__name__}: {exc}",
            payload,
            event="PreToolUse",
            deny=True,
        )
        return
    _emit(
        "CacheLayer blocked the full test suite. TIA result (do not re-run npm test):\n"
        + json.dumps(affected, default=str)[:6000],
        payload,
        event="PreToolUse",
        deny=True,
    )


def _emit(msg: str, payload: dict, event: str = "PostToolUse", deny: bool = False) -> None:
    fmt = (os.environ.get("CACHELAYER_HOOK_FORMAT") or "").strip().lower()
    if not fmt:
        cursor_keys = ("workspace_roots", "conversation_id", "generation_id")
        fmt = "cursor" if any(k in payload for k in cursor_keys) else "nested"
    if fmt == "cursor":
        print(json.dumps({"additional_context": msg}))
        return
    print(json.dumps({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny" if deny else "allow",
            "permissionDecisionReason": "tia_full_suite" if deny else "ok",
            "additionalContext": msg,
        },
    }))


def _run_debug(payload: dict, normalized: dict) -> None:
    inp = normalized["tool_input"]
    blob = _failure_blob(payload, inp)
    if not _terminal_failed(payload, inp, blob):
        print(EMPTY)
        return
    cwd = normalized["cwd"]
    try:
        result = debug_failure(stack_trace=blob[:12000], test_output="", cwd=cwd)
    except Exception as exc:
        _emit(f"CacheLayer debug_failure error: {type(exc).__name__}: {exc}", payload)
        return
    _emit("CacheLayer debug_failure\n" + json.dumps(result, default=str)[:6000], payload)


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        print(EMPTY)
        return
    try:
        payload = json.loads(raw)
    except Exception as exc:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"CacheLayer hook JSON error: {exc}",
            }
        }))
        return
    if not isinstance(payload, dict):
        print(EMPTY)
        return

    normalized = normalize_payload(payload)
    tool_name = normalized["tool_name"]
    if _is_terminal_tool(tool_name):
        if _hook_event(payload).startswith("pre"):
            _run_tia_instead_of_full_suite(payload, normalized)
            return
        _run_debug(payload, normalized)
        return
    if tool_name and not _is_edit_tool(tool_name):
        print(EMPTY)
        return

    normalized_payload = {**payload, "tool_input": normalized["tool_input"]}
    paths = _extract_paths(normalized_payload)
    if not paths:
        print(EMPTY)
        return

    cwd = normalized["cwd"]
    root = Path(cwd or os.getcwd()).resolve()
    if _deduplicated(root, normalized, paths):
        print(EMPTY)
        return
    try:
        result = verify_edit(
            paths=paths,
            line_range=_line_range(normalized_payload) if len(paths) == 1 else None,
            run_tests=True,
            hook=True,
            cwd=cwd,
            mode="coherent",
            edit_cycle_id=normalized["cycle_id"] or None,
        )
    except Exception as exc:
        _emit(f"CacheLayer verify_edit error: {type(exc).__name__}: {exc}", payload)
        return

    _emit(_verify_summary(result), payload)


if __name__ == "__main__":
    main()
