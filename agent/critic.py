"""CRITIC: typecheck → lint → tests (tests only if earlier gates pass)."""
from __future__ import annotations

import re
import os
from pathlib import Path
from typing import Any

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


def verify_edit(
    paths: list[str] | None = None,
    line_range: list[int] | None = None,
    *,
    run_tests: bool = True,
    hook: bool = False,
    cwd: str | None = None,
) -> dict[str, Any]:
    """
    Gate order: types → lint → tests.
    Hook mode: single-file lint only (fast). No full tsc/mypy/tests.
    """
    info = detect(cwd)
    root = Path(info["root"])
    files = _norm_paths(paths, root)
    if not files and not hook:
        files = _norm_paths(git_changed_files(root), root)
    timeout = HOOK_TIMEOUT_S if hook else 25
    ranges = None
    if line_range and len(line_range) >= 2:
        ranges = [(int(line_range[0]), int(line_range[1]))]

    py_files = [f for f in files if f.endswith((".py", ".pyi"))]
    js_files = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))]
    java_files = [f for f in files if f.endswith(".java")]

    gates: list[dict[str, Any]] = []
    blocked = False

    def add_gate(name: str, result: dict[str, Any], subset: list[str]) -> bool:
        nonlocal blocked
        if result.get("available") is False:
            gates.append({"name": name, "available": False, "skipped": True, "install": result.get("install") or result.get("output")})
            return True
        filtered = filter_tool_output(result.get("output") or "", subset or files, ranges, root)
        ok = bool(result.get("ok"))
        if not ok:
            blocked = True
        gates.append({
            "name": name,
            "ok": ok,
            "output": cap_text(filtered, HOOK_MAX_CHARS if hook else MAX_TOOL_CHARS),
        })
        return ok

    # --- Python types then lint ---
    if py_files:
        type_ok = True
        if hook:
            type_ok = add_gate("py-compile", run_cmd(
                [info["tools"].get("python3") or "python", "-m", "py_compile", *py_files],
                cwd=root, timeout=timeout,
            ), py_files)
        elif info["tools"].get("mypy"):
            type_ok = add_gate("mypy", run_cmd(
                [info["tools"]["mypy"], "--hide-error-context", "--no-error-summary", "--follow-imports=silent", *py_files],
                cwd=root, timeout=timeout,
            ), py_files)
        else:
            gates.append({"name": "mypy", "available": False, "skipped": True, "install": "pip install mypy"})

        if type_ok and info["tools"].get("ruff"):
            add_gate("ruff", run_cmd([info["tools"]["ruff"], "check", "--quiet", *py_files], cwd=root, timeout=timeout), py_files)
        elif type_ok and info["tools"].get("flake8"):
            add_gate("flake8", run_cmd([info["tools"]["flake8"], *py_files], cwd=root, timeout=timeout), py_files)
        elif type_ok:
            gates.append({"name": "python-lint", "available": False, "skipped": True, "install": "pip install ruff"})

    # --- JS/TS: tsc then eslint (hook: eslint on file only) ---
    if js_files or (not files and info["javascript"] and not hook):
        type_ok = True
        if not hook and (info["flags"].get("tsconfig") or any(f.endswith((".ts", ".tsx")) for f in js_files)):
            type_ok = add_gate("tsc", _npx_or_bin("tsc", ["--noEmit", "--pretty", "false"], root, timeout), js_files)
        if type_ok and (info["flags"].get("eslint_config") or info["tools"].get("eslint") or (root / "node_modules" / ".bin" / "eslint").exists()):
            eslint_args = ["--max-warnings", "0", *(js_files or ["."])]
            add_gate("eslint", _npx_or_bin("eslint", eslint_args, root, timeout), js_files)
        elif type_ok:
            gates.append({"name": "eslint", "available": False, "skipped": True, "install": "npm install --save-dev eslint"})

    # --- Java compile hint (optional, skip in hook) ---
    if java_files and not hook and info["java"]:
        if info["maven"] and info["tools"].get("mvn"):
            add_gate("mvn-compile", run_cmd([info["tools"]["mvn"], "-q", "-DskipTests", "compile"], cwd=root, timeout=min(40, timeout + 20)), java_files)
        elif info["gradle"] and info["tools"].get("gradle"):
            add_gate("gradle-classes", run_cmd([info["tools"]["gradle"], "-q", "classes"], cwd=root, timeout=min(40, timeout + 20)), java_files)

    # --- tests only if types+lint passed ---
    tests_ran = False
    if run_tests and not hook and not blocked:
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
    elif run_tests and not hook and blocked:
        gates.append({"name": "tests", "skipped": True, "reason": "type/lint failed — fix those before tests"})

    ran = [g for g in gates if not g.get("skipped")]
    ok = not blocked and bool(ran)
    if not ran:
        return {
            "ok": True,
            "skipped": True,
            "reason": "no typechecker/linter found for these files",
            "install": "pip install mypy ruff  |  npm i -D typescript eslint",
            "files": files,
        }

    return {
        "ok": ok,
        "blocked": blocked,
        "files": files,
        "tests_ran": tests_ran,
        "gates": gates,
        "next": (
            "Fix the errors above, then call verify_edit again. Do not grep."
            if blocked else
            "Gates passed. Continue the task."
        ),
    }
