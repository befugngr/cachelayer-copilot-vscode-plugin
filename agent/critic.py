"""CRITIC: typecheck → lint → tests (tests only if earlier gates pass)."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

try:
    from .detect import detect
    from .util import (
        HOOK_MAX_CHARS, HOOK_TIMEOUT_S, MAX_TOOL_CHARS, cap_text,
        git_changed_files, is_code_path, rel_to_root, run_cmd, which,
    )
except ImportError:
    from detect import detect
    from util import (
        HOOK_MAX_CHARS, HOOK_TIMEOUT_S, MAX_TOOL_CHARS, cap_text,
        git_changed_files, is_code_path, rel_to_root, run_cmd, which,
    )

_LINE_RE = re.compile(
    r"""(?x)
    (?P<file>[\w./\\-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|java))
    :(?P<line>\d+)
    (?::(?P<col>\d+))?
    """
)
MAX_PARALLEL_GATES = 3
MAX_RETRIES = 3
_STATE_DIR = ".cachelayer"
_RETRY_FILE = "critic-retry.json"


def _norm_paths(paths: list[str] | None, root: Path) -> list[str]:
    out: list[str] = []
    for raw in paths or []:
        if not raw:
            continue
        rel = rel_to_root(raw, root)
        if is_code_path(rel):
            out.append(rel.replace("\\", "/"))
    # unique, keep order
    seen = set()
    uniq = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _in_range(line: int, ranges: list[tuple[int, int]] | None) -> bool:
    if not ranges:
        return True
    for start, end in ranges:
        lo, hi = min(start, end) - 3, max(start, end) + 3
        if lo <= line <= hi:
            return True
    return False


def filter_tool_output(
    output: str,
    paths: list[str],
    ranges: list[tuple[int, int]] | None,
    root: Path,
) -> str:
    if not output:
        return ""
    wanted = {p.replace("\\", "/") for p in paths}
    keep: list[str] = []
    for raw in output.splitlines():
        m = _LINE_RE.search(raw.replace("\\", "/"))
        if not m:
            # keep summary lines if we already captured a hit
            if keep and raw.strip() and not raw.startswith(" " * 8):
                if any(s in raw.lower() for s in ("error", "failed", "issue")):
                    keep.append(raw)
            continue
        f = m.group("file").lstrip("./")
        line = int(m.group("line"))
        match_path = not wanted or any(f == w or f.endswith("/" + w) or w.endswith("/" + f) for w in wanted)
        if match_path and _in_range(line, ranges):
            keep.append(raw)
    if keep:
        return "\n".join(keep)
    # nothing matched filter — return a short tail so the agent still sees the failure
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return "\n".join(lines[-12:])


def _npx_or_bin(bin_name: str, args: list[str], root: Path, timeout: int) -> dict[str, Any]:
    local = root / "node_modules" / ".bin" / (f"{bin_name}.cmd" if os.name == "nt" else bin_name)
    if local.exists():
        return run_cmd([str(local), *args], cwd=root, timeout=timeout)
    if which(bin_name):
        return run_cmd([bin_name, *args], cwd=root, timeout=timeout)
    npx = which("npx")
    if npx:
        return run_cmd([npx, "--no-install", bin_name, *args], cwd=root, timeout=timeout)
    return {"ok": False, "available": False, "output": f"{bin_name} not installed", "code": 127}


def _cycle_id(root: Path, files: list[str], supplied: str | None) -> str:
    if supplied and len(supplied) <= 160:
        return supplied
    # Without an editor/agent cycle ID, scope state to this concrete edit
    # snapshot rather than accidentally carrying retries across later edits.
    snapshot: list[str] = []
    for name in files:
        try:
            stat = (root / name).stat()
            snapshot.append(f"{name}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            snapshot.append(name)
    return hashlib.sha256("\0".join(snapshot).encode("utf-8")).hexdigest()[:20]


def _state_path(root: Path) -> Path:
    return root / _STATE_DIR / _RETRY_FILE


def _read_retry(root: Path) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_retry(root: Path, data: dict[str, Any] | None) -> None:
    path = _state_path(root)
    try:
        if data is None:
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        # Feedback state must never make an editor hook fail closed.
        return


def _feedback(
    root: Path,
    cycle: str,
    *,
    blocked: bool,
    files: list[str],
    max_retries: int,
) -> dict[str, Any]:
    if not blocked:
        _write_retry(root, None)
        return {
            "cycle_id": cycle,
            "attempt": 0,
            "max_retries": max_retries,
            "action": "continue",
            "clear": True,
        }
    prior = _read_retry(root)
    attempt = int(prior.get("attempt", 0)) + 1 if prior.get("cycle_id") == cycle else 1
    attempt = min(attempt, max_retries)
    exhausted = attempt >= max_retries
    _write_retry(root, {
        "version": 1,
        "cycle_id": cycle,
        "attempt": attempt,
        "max_retries": max_retries,
        "files": files[:100],
        "updated_at": int(time.time()),
    })
    return {
        "cycle_id": cycle,
        "attempt": attempt,
        "max_retries": max_retries,
        "remaining": max(0, max_retries - attempt),
        "action": "stop_and_report" if exhausted else "re_edit_once",
        "instruction": (
            "Retry cap reached. Stop editing and report the remaining diagnostics."
            if exhausted else
            "Make one coherent edit addressing all diagnostics, then call verify_edit once with the same cycle_id."
        ),
    }


def verify_edit(
    paths: list[str] | None = None,
    line_range: list[int] | None = None,
    *,
    run_tests: bool = True,
    hook: bool = False,
    cwd: str | None = None,
    mode: str = "fast",
    edit_cycle_id: str | None = None,
    max_retries: int = MAX_RETRIES,
    max_parallel: int = MAX_PARALLEL_GATES,
) -> dict[str, Any]:
    """
    Independent type/lint prerequisites run concurrently, then affected tests.
    Hook mode defaults to fast file-scoped checks. ``mode="coherent"`` is an
    explicit full gate intended once after an edit batch.
    """
    info = detect(cwd)
    root = Path(info["root"])
    files = _norm_paths(paths, root)
    coherent = mode == "coherent"
    if not files and (not hook or coherent):
        files = _norm_paths(git_changed_files(root), root)
    timeout = HOOK_TIMEOUT_S if hook and not coherent else 25
    max_retries = max(1, min(int(max_retries), MAX_RETRIES))
    max_parallel = max(1, min(int(max_parallel), MAX_PARALLEL_GATES))
    cycle = _cycle_id(root, files, edit_cycle_id)
    ranges = None
    if line_range and len(line_range) >= 2:
        ranges = [(int(line_range[0]), int(line_range[1]))]

    py_files = [f for f in files if f.endswith((".py", ".pyi"))]
    js_files = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))]
    java_files = [f for f in files if f.endswith(".java")]

    gates: list[dict[str, Any]] = []
    blocked = False
    jobs: list[tuple[str, Callable[[], dict[str, Any]], list[str]]] = []

    def unavailable(name: str, install: str) -> None:
        gates.append({"name": name, "available": False, "skipped": True, "install": install})

    def record(name: str, result: dict[str, Any], subset: list[str]) -> None:
        nonlocal blocked
        if result.get("available") is False:
            gates.append({"name": name, "available": False, "skipped": True, "install": result.get("install") or result.get("output")})
            return
        filtered = filter_tool_output(result.get("output") or "", subset or files, ranges, root)
        ok = bool(result.get("ok"))
        if not ok:
            blocked = True
        gates.append({
            "name": name,
            "ok": ok,
            "output": cap_text(filtered, HOOK_MAX_CHARS if hook else MAX_TOOL_CHARS),
            "timed_out": bool(result.get("timeout")),
            "memory_limited": bool(result.get("memory_limited")),
        })

    # Build independent prerequisite jobs. Captured defaults avoid late binding.
    if py_files:
        if hook and not coherent:
            jobs.append(("py-compile", lambda py_files=py_files: run_cmd(
                [info["tools"].get("python3") or "python", "-m", "py_compile", *py_files],
                cwd=root, timeout=timeout,
            ), py_files))
        elif info["tools"].get("mypy"):
            jobs.append(("mypy", lambda py_files=py_files: run_cmd(
                [info["tools"]["mypy"], "--hide-error-context", "--no-error-summary", "--follow-imports=silent", *py_files],
                cwd=root, timeout=timeout,
            ), py_files))
        else:
            unavailable("mypy", "pip install mypy")

        if info["tools"].get("ruff"):
            jobs.append(("ruff", lambda py_files=py_files: run_cmd(
                [info["tools"]["ruff"], "check", "--quiet", *py_files], cwd=root, timeout=timeout,
            ), py_files))
        elif info["tools"].get("flake8"):
            jobs.append(("flake8", lambda py_files=py_files: run_cmd(
                [info["tools"]["flake8"], *py_files], cwd=root, timeout=timeout,
            ), py_files))
        else:
            unavailable("python-lint", "pip install ruff")

    if js_files or (not files and info["javascript"] and (not hook or coherent)):
        if (not hook or coherent) and (info["flags"].get("tsconfig") or any(f.endswith((".ts", ".tsx")) for f in js_files)):
            jobs.append(("tsc", lambda: _npx_or_bin(
                "tsc", ["--noEmit", "--pretty", "false"], root, timeout,
            ), js_files))
        if info["flags"].get("eslint_config") or info["tools"].get("eslint") or (root / "node_modules" / ".bin" / "eslint").exists():
            eslint_args = ["--max-warnings", "0", *(js_files or ["."])]
            jobs.append(("eslint", lambda eslint_args=eslint_args: _npx_or_bin(
                "eslint", eslint_args, root, timeout,
            ), js_files))
        else:
            unavailable("eslint", "npm install --save-dev eslint")

    if java_files and (not hook or coherent) and info["java"]:
        if info["maven"] and info["tools"].get("mvn"):
            jobs.append(("mvn-compile", lambda: run_cmd(
                [info["tools"]["mvn"], "-q", "-DskipTests", "compile"],
                cwd=root, timeout=min(40, timeout + 20),
            ), java_files))
        elif info["gradle"] and info["tools"].get("gradle"):
            jobs.append(("gradle-classes", lambda: run_cmd(
                [info["tools"]["gradle"], "-q", "classes"],
                cwd=root, timeout=min(40, timeout + 20),
            ), java_files))

    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_parallel, len(jobs))) as pool:
            futures = [pool.submit(command) for _, command, _ in jobs]
            # Consume in declaration order for deterministic agent feedback.
            for (name, _, subset), future in zip(jobs, futures):
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"ok": False, "output": f"{name} failed to run: {exc}"}
                record(name, result, subset)

    tests_ran = False
    full_gate = not hook or coherent
    if run_tests and full_gate and not blocked:
        tests_ran = True
        try:
            from .tia import run_affected_tests
        except ImportError:
            from tia import run_affected_tests
        tia = run_affected_tests(changed_files=files or None, cwd=str(root), timeout=timeout)
        if tia.get("available") is False:
            tests_ran = False
            gates.append({
                "name": "tests",
                "available": False,
                "skipped": True,
                "install": tia.get("install"),
                "output": cap_text(tia.get("summary") or "", MAX_TOOL_CHARS),
            })
        else:
            gates.append({
                "name": "tests",
                "ok": bool(tia.get("ok")),
                "output": cap_text(tia.get("failures") or tia.get("summary") or "", MAX_TOOL_CHARS),
                "tia": {k: tia.get(k) for k in ("selected", "skipped_estimate", "runner")},
            })
            if not tia.get("ok"):
                blocked = True
    elif run_tests and full_gate and blocked:
        gates.append({"name": "tests", "skipped": True, "reason": "type/lint failed — fix those before tests"})

    ran = [g for g in gates if not g.get("skipped")]
    ok = not blocked and bool(ran)
    if not ran:
        result = {
            "ok": True,
            "skipped": True,
            "reason": "no typechecker/linter found for these files",
            "install": "pip install mypy ruff  |  npm i -D typescript eslint",
            "files": files,
        }
        result["feedback"] = _feedback(
            root, cycle, blocked=False, files=files, max_retries=max_retries,
        )
        return result

    feedback = _feedback(
        root, cycle, blocked=blocked, files=files, max_retries=max_retries,
    )
    return {
        "ok": ok,
        "blocked": blocked,
        "files": files,
        "tests_ran": tests_ran,
        "mode": "coherent" if coherent else "fast",
        "parallelism": min(max_parallel, len(jobs)) if jobs else 0,
        "gates": gates,
        "feedback": feedback,
        "next": feedback.get("instruction") or "Gates passed. Continue the task.",
    }
