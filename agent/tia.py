"""Bounded test-impact analysis that never silently runs a full suite."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from .detect import detect
    from .util import cap_text, git_changed_files, is_code_path, rel_to_root, run_cmd, which
except ImportError:
    from detect import detect
    from util import cap_text, git_changed_files, is_code_path, rel_to_root, run_cmd, which

_FAIL_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)", re.MULTILINE)
_SUMMARY_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|deselected)")


def _changed(root: Path, changed_files: list[str] | None) -> list[str]:
    raw = changed_files if changed_files is not None else git_changed_files(root)
    result: list[str] = []
    for item in raw:
        rel = rel_to_root(item, root)
        if rel and is_code_path(rel) and rel not in result:
            result.append(rel)
    return result[:100]


def _summary(output: str) -> tuple[int | None, int | None, str]:
    counts: dict[str, int] = {}
    for amount, label in _SUMMARY_COUNT_RE.findall(output or ""):
        counts[label] = counts.get(label, 0) + int(amount)
    selected = sum(counts.get(k, 0) for k in ("passed", "failed", "error", "errors", "skipped"))
    selected_value = selected if counts else None
    deselected = counts.get("deselected")
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    return selected_value, deselected, cap_text(lines[-1] if lines else "", 400)


def _failures(output: str) -> str:
    matches = _FAIL_RE.findall(output or "")
    if matches:
        return cap_text("\n".join(f"{kind} {node}" for kind, node in matches), 2500)
    if "FAILURES" in output:
        return cap_text(output[output.find("FAILURES"):], 2500)
    return ""


def _pytest_result(result: dict[str, Any], runner: str, tests: list[str] | None = None) -> dict[str, Any]:
    output = result.get("output") or ""
    selected, deselected, summary = _summary(output)
    return {
        "ok": bool(result.get("ok")),
        "runner": runner,
        "selected": selected,
        "skipped_estimate": deselected,
        "tests": (tests or [])[:50],
        "failures": _failures(output) if not result.get("ok") else "",
        "summary": summary,
        "timed_out": bool(result.get("timeout")),
    }


def _run_pytest_testmon(root: Path, python: str, timeout: int) -> dict[str, Any]:
    result = run_cmd(
        [python, "-m", "pytest", "-q", "--testmon", "--tb=line"],
        cwd=root,
        timeout=timeout,
    )
    return _pytest_result(result, "pytest-testmon")


def _python_test_candidates(root: Path, files: list[str]) -> list[str]:
    candidates: list[str] = []
    all_tests = list(root.glob("tests/test_*.py"))[:1000] + list(root.glob("test/test_*.py"))[:1000]
    for changed in files:
        path = Path(changed)
        normalized = changed.replace("\\", "/")
        if path.suffix not in (".py", ".pyi"):
            continue
        if path.name.startswith("test_") or "/tests/" in f"/{normalized}":
            if (root / path).is_file():
                candidates.append(path.as_posix())
            continue
        stem = path.stem
        module_parts = [part for part in path.with_suffix("").parts if part not in ("src", "lib")]
        tokens = {stem, "_".join(module_parts[-2:]) if len(module_parts) > 1 else stem}
        for test_path in all_tests:
            name = test_path.stem.removeprefix("test_")
            if any(name == token or name.endswith("_" + token) for token in tokens):
                candidates.append(test_path.relative_to(root).as_posix())
    return list(dict.fromkeys(candidates))[:50]


def _run_pytest_mapped(root: Path, files: list[str], python: str, timeout: int) -> dict[str, Any]:
    tests = _python_test_candidates(root, files)
    if not tests:
        return {
            "ok": True,
            "available": True,
            "runner": "pytest-mapped",
            "selected": 0,
            "skipped_estimate": None,
            "tests": [],
            "failures": "",
            "summary": "No safely mapped pytest tests; full suite was not run.",
            "install": "Install pytest-testmon for coverage-guided selection: pip install pytest-testmon",
        }
    result = run_cmd([python, "-m", "pytest", "-q", "--tb=line", *tests], cwd=root, timeout=timeout)
    return _pytest_result(result, "pytest-mapped", tests)


def _jest_command(root: Path) -> list[str] | None:
    local = root / "node_modules" / ".bin" / ("jest.cmd" if os.name == "nt" else "jest")
    if local.is_file():
        return [str(local)]
    if which("jest"):
        return [which("jest") or "jest"]
    if which("npx"):
        return [which("npx") or "npx", "--no-install", "jest"]
    return None


def _run_jest(root: Path, files: list[str], timeout: int) -> dict[str, Any]:
    command = _jest_command(root)
    source_files = [f for f in files if Path(f).suffix.lower() in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")]
    if not command:
        return {"ok": False, "available": False, "runner": "jest", "install": "npm install --save-dev jest"}
    if not source_files:
        return {
            "ok": True, "available": True, "runner": "jest-findRelatedTests", "selected": 0,
            "skipped_estimate": None, "summary": "No changed JS/TS files; Jest was not run.",
        }
    result = run_cmd(
        [*command, "--findRelatedTests", *source_files[:30], "--runInBand", "--passWithNoTests"],
        cwd=root,
        timeout=timeout,
    )
    output = result.get("output") or ""
    return {
        "ok": bool(result.get("ok")),
        "runner": "jest-findRelatedTests",
        "selected": None,
        "skipped_estimate": None,
        "changed_files": source_files[:30],
        "failures": cap_text(output, 2500) if not result.get("ok") else "",
        "summary": cap_text(next((line for line in reversed(output.splitlines()) if line.strip()), ""), 400),
        "timed_out": bool(result.get("timeout")),
    }


def _java_test_classes(root: Path, files: list[str]) -> list[str]:
    tests: list[str] = []
    test_roots = (root / "src" / "test" / "java", root / "src" / "test" / "kotlin")
    existing = [path for base in test_roots if base.is_dir() for path in base.rglob("*Test.*")][:1000]
    for changed in files:
        if Path(changed).suffix.lower() not in (".java", ".kt", ".kts"):
            continue
        stem = Path(changed).stem
        if stem.endswith("Test") and (root / changed).is_file():
            tests.append(stem)
        for test_path in existing:
            if test_path.stem in (f"{stem}Test", f"{stem}Tests"):
                tests.append(test_path.stem)
    return list(dict.fromkeys(tests))[:40]


def _run_maven(root: Path, info: dict[str, Any], files: list[str], timeout: int) -> dict[str, Any]:
    mvn = info["tools"].get("mvn")
    if not mvn:
        return {"ok": False, "available": False, "runner": "maven", "install": "Install Maven or add mvnw/mvnw.cmd."}
    tests = _java_test_classes(root, files)
    ekstazi_db = (root / ".ekstazi").exists()
    if info["flags"].get("ekstazi") and ekstazi_db:
        result = run_cmd([mvn, "-q", "-DfailIfNoTests=false", "test"], cwd=root, timeout=timeout)
        runner = "maven-ekstazi-configured"
        selected = None
    elif tests:
        result = run_cmd(
            [mvn, "-q", "-DfailIfNoTests=false", f"-Dtest={','.join(tests)}", "test"],
            cwd=root,
            timeout=timeout,
        )
        runner = "maven-surefire-subset"
        selected = len(tests)
    else:
        return {
            "ok": True, "available": True, "runner": "maven-surefire-subset", "selected": 0,
            "skipped_estimate": None, "tests": [],
            "summary": "No safely mapped Java tests; full suite was not run.",
            "jacoco_detected": bool(info["flags"].get("jacoco")),
            "ekstazi_detected": bool(info["flags"].get("ekstazi")),
            "install": "Configure Ekstazi and establish its dependency database for incremental selection.",
        }
    output = result.get("output") or ""
    return {
        "ok": bool(result.get("ok")), "runner": runner, "selected": selected,
        "skipped_estimate": None, "tests": tests,
        "failures": cap_text(output, 2500) if not result.get("ok") else "",
        "summary": cap_text(next((line for line in reversed(output.splitlines()) if line.strip()), ""), 400),
        "jacoco_detected": bool(info["flags"].get("jacoco")),
        "jacoco_used_for_selection": False,
        "ekstazi_detected": bool(info["flags"].get("ekstazi")),
        "timed_out": bool(result.get("timeout")),
    }


def _run_gradle(root: Path, info: dict[str, Any], files: list[str], timeout: int) -> dict[str, Any]:
    gradle = info["tools"].get("gradle")
    if not gradle:
        return {"ok": False, "available": False, "runner": "gradle", "install": "Install Gradle or add gradlew/gradlew.bat."}
    tests = _java_test_classes(root, files)
    if not tests:
        return {
            "ok": True, "available": True, "runner": "gradle-tests-subset", "selected": 0,
            "skipped_estimate": None, "tests": [], "summary": "No safely mapped Java tests; full suite was not run.",
        }
    args = [gradle, "-q", "test"]
    for test in tests:
        args.extend(["--tests", test])
    result = run_cmd(args, cwd=root, timeout=timeout)
    output = result.get("output") or ""
    return {
        "ok": bool(result.get("ok")), "runner": "gradle-tests-subset", "selected": len(tests),
        "skipped_estimate": None, "tests": tests,
        "failures": cap_text(output, 2500) if not result.get("ok") else "",
        "summary": cap_text(next((line for line in reversed(output.splitlines()) if line.strip()), ""), 400),
        "timed_out": bool(result.get("timeout")),
    }


def run_affected_tests(
    changed_files: list[str] | None = None,
    *,
    cwd: str | None = None,
    timeout: int = 45,
) -> dict[str, Any]:
    info = detect(cwd)
    root = Path(info["root"])
    files = _changed(root, changed_files)
    timeout = max(1, min(int(timeout), 300))

    if info["python"]:
        python = info["tools"].get("python3")
        if not python:
            result = {"ok": False, "available": False, "runner": "pytest", "install": "Install Python 3 and pytest."}
        elif not info["tools"].get("pytest") and not _can_import_pytest():
            result = {"ok": False, "available": False, "runner": "pytest", "install": "pip install pytest pytest-testmon"}
        elif info["flags"].get("testmon"):
            result = _run_pytest_testmon(root, python, timeout)
        else:
            result = _run_pytest_mapped(root, files, python, timeout)
        result["changed_files"] = files[:30]
        return result

    if info["maven"]:
        result = _run_maven(root, info, files, timeout)
    elif info["gradle"]:
        result = _run_gradle(root, info, files, timeout)
    elif info["javascript"]:
        result = _run_jest(root, files, timeout)
    else:
        result = {
            "ok": False,
            "available": False,
            "summary": "No supported test runner detected; no suite was run.",
            "install": "Python: pytest-testmon; JS/TS: Jest; Java: Maven Surefire/Ekstazi or Gradle.",
        }
    result["changed_files"] = files[:30]
    return result


def _can_import_pytest() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("pytest") is not None
    except (ImportError, ValueError):
        return False
