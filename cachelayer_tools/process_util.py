"""Shared helpers for local coding-agent tools. Stdlib only."""
from __future__ import annotations

import json
import os
import atexit
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

MAX_TOOL_CHARS = 3500
HOOK_MAX_CHARS = 1800
DEFAULT_TIMEOUT_S = 20
HOOK_TIMEOUT_S = 6
MAX_CAPTURE_BYTES = 256_000
DEFAULT_MEMORY_MB = 768
_PROCESSES: set[subprocess.Popen[bytes]] = set()
_PROCESS_LOCK = threading.Lock()

CODE_EXTS = {
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".go", ".rs",
}
RISK_CONFIG_NAMES = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "package.json", "pytest.ini", "pyproject.toml",
    "setup.cfg", "tox.ini", "tsconfig.json",
}

SKIP_PARTS = {
    "node_modules", ".git", "dist", "build", ".venv", "venv",
    "__pycache__", ".tox", "target", "coverage",
}


def which(name: str) -> str | None:
    return shutil.which(name)


def workspace_root(cwd: str | None = None) -> Path:
    p = Path(cwd or os.getcwd()).resolve()
    if p.is_file():
        p = p.parent
    for cand in [p, *p.parents]:
        if any((cand / m).exists() for m in (
            ".git", "pyproject.toml", "package.json", "pom.xml",
            "build.gradle", "build.gradle.kts", "Cargo.toml", "go.mod",
        )):
            return cand
        if cand.parent == cand:
            break
    return p


def is_code_path(path: str | Path) -> bool:
    p = Path(path)
    if p.suffix.lower() not in CODE_EXTS and p.name not in RISK_CONFIG_NAMES:
        return False
    parts = set(p.parts)
    return not (parts & SKIP_PARTS)


def cap_text(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n…[truncated]"


def capped_json(data: Any, limit: int = MAX_TOOL_CHARS) -> str:
    """Serialize to valid JSON while bounding the MCP text payload."""
    value = deepcopy(data)
    for _ in range(20):
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
        if len(text) <= limit:
            return text
        strings: list[tuple[int, tuple[Any, ...]]] = []

        def visit(item: Any, path: tuple[Any, ...] = ()) -> None:
            if isinstance(item, str):
                strings.append((len(item), path))
            elif isinstance(item, dict):
                for key, child in item.items():
                    visit(child, path + (key,))
            elif isinstance(item, list):
                for index, child in enumerate(item):
                    visit(child, path + (index,))

        visit(value)
        if not strings:
            break
        _, path = max(strings)
        if not path:
            break
        parent = value
        for part in path[:-1]:
            parent = parent[part]
        key = path[-1]
        old = parent[key]
        keep = max(80, len(old) // 2)
        parent[key] = cap_text(old, keep)
    return json.dumps(
        {"ok": False, "truncated": True, "summary": "tool result exceeded output cap"},
        separators=(",", ":"),
    )


def run_cmd(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    memory_mb: int | None = DEFAULT_MEMORY_MB,
) -> dict[str, Any]:
    if not argv or not argv[0]:
        return {"ok": False, "code": 127, "output": "empty command", "argv": argv, "available": False}
    creationflags = 0
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
        limiter = _memory_preexec(memory_mb)
        if limiter is not None:
            popen_kwargs["preexec_fn"] = limiter
    try:
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd else None,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env={**os.environ, **(env or {})},
                creationflags=creationflags,
                **popen_kwargs,
            )
            with _PROCESS_LOCK:
                _PROCESSES.add(proc)
            memory_limited = bool(popen_kwargs.get("preexec_fn"))
            if not memory_limited:
                memory_limited = _limit_process_memory(proc, memory_mb)
            try:
                proc.communicate(
                    input=input_text.encode("utf-8") if input_text is not None else None,
                    timeout=max(1, timeout),
                )
            except subprocess.TimeoutExpired:
                _terminate_process(proc)
                try:
                    proc.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    _kill_process(proc)
                    try:
                        proc.communicate(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                return {
                    "ok": False,
                    "code": 124,
                    "output": f"timeout after {timeout}s: {format_argv(argv[:4])}",
                    "argv": argv,
                    "timeout": True,
                    "memory_limited": memory_limited,
                }
            finally:
                if proc.poll() is not None:
                    with _PROCESS_LOCK:
                        _PROCESSES.discard(proc)
            output.seek(0, os.SEEK_END)
            size = output.tell()
            truncated = size > MAX_CAPTURE_BYTES
            if truncated:
                half = MAX_CAPTURE_BYTES // 2
                output.seek(0)
                first = output.read(half)
                output.seek(max(0, size - half))
                raw = first + b"\n...[command output truncated]...\n" + output.read(half)
            else:
                output.seek(0)
                raw = output.read()
        out = raw.decode("utf-8", errors="replace").strip()
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "output": out,
            "argv": argv,
            "truncated": truncated,
            "memory_limited": memory_limited,
        }
    except (FileNotFoundError, PermissionError) as exc:
        return {
            "ok": False,
            "code": 127,
            "output": f"cannot execute {argv[0]}: {exc}",
            "argv": argv,
            "available": False,
        }
    except OSError as exc:
        return {"ok": False, "code": 126, "output": f"command failed to start: {exc}", "argv": argv}


def _limit_process_memory(proc: subprocess.Popen[bytes], memory_mb: int | None) -> bool:
    """Apply a small per-process address-space cap where the OS supports prlimit."""
    if os.name == "nt" or memory_mb is None:
        return False
    try:
        import resource

        limit_mb = max(256, min(int(memory_mb), 1536))
        limit = limit_mb * 1024 * 1024
        resource.prlimit(proc.pid, resource.RLIMIT_AS, (limit, limit))
        return True
    except (AttributeError, ImportError, OSError, PermissionError, ProcessLookupError, ValueError):
        return False


def _memory_preexec(memory_mb: int | None):
    if os.name == "nt" or memory_mb is None:
        return None
    try:
        import resource
    except ImportError:
        return None
    limit_mb = max(256, min(int(memory_mb), 1536))
    limit = limit_mb * 1024 * 1024

    def apply_limit() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return apply_limit


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=1, check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _kill_process(proc)


def _kill_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=1, check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _cleanup_processes() -> None:
    with _PROCESS_LOCK:
        processes = list(_PROCESSES)
    for proc in processes:
        _terminate_process(proc)


atexit.register(_cleanup_processes)


def format_argv(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    import shlex
    return shlex.join(argv)


def git_changed_files(root: Path) -> list[str]:
    r = run_cmd(
        ["git", "diff", "--name-only", "--diff-filter=ACMRD", "HEAD", "--"],
        cwd=root, timeout=8,
    )
    if not r.get("ok"):
        r = run_cmd(
            ["git", "diff", "--name-only", "--diff-filter=ACMRD", "--"],
            cwd=root, timeout=8,
        )
    files = []
    for line in (r.get("output") or "").splitlines():
        line = line.strip()
        if line and is_code_path(line):
            files.append(line)
    untracked = run_cmd(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root, timeout=8,
    )
    if untracked.get("ok"):
        for line in (untracked.get("output") or "").splitlines():
            line = line.strip()
            if line and is_code_path(line) and line not in files:
                files.append(line)
    return files


def rel_to_root(path: str | Path, root: Path) -> str:
    p = Path(path)
    try:
        resolved = p.resolve() if p.is_absolute() else (root / p).resolve()
        return resolved.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def dump(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def py_bin() -> str:
    return sys.executable or "python3"
