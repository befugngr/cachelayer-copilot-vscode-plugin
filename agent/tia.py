"""Bounded test-impact analysis that never silently runs a full suite."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from .detect import detect
    from .tia_select import (
        coverage_context_tests, jacoco_covered_classes, jacoco_diff_coverage,
        fresh_java_test_reports, java_dependents, java_test_report_snapshot,
        java_tests_referencing, python_importers, smart_test_picker_map,
        smart_test_picker_selection,
    )
    from .util import cap_text, git_changed_files, is_code_path, rel_to_root, run_cmd, which
except ImportError:
    from detect import detect
    from tia_select import (
        coverage_context_tests, jacoco_covered_classes, jacoco_diff_coverage,
        fresh_java_test_reports, java_dependents, java_test_report_snapshot,
        java_tests_referencing, python_importers, smart_test_picker_map,
        smart_test_picker_selection,
    )
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


def _run_pytest_coverage_contexts(
    root: Path, files: list[str], python: str, timeout: int
) -> dict[str, Any] | None:
    """Precise selection when coverage.py recorded which test executed each line."""
    ctx = coverage_context_tests(root, files)
    tests = ctx.get("tests") or []
    if not tests:
        return None
    result = run_cmd([python, "-m", "pytest", "-q", "--tb=line", *tests], cwd=root, timeout=timeout)
    payload = _pytest_result(result, "pytest-coverage-contexts", tests)
    payload["selection_sources"] = ["coverage-contexts"]
    payload["coverage_contexts_used"] = True
    return payload


def _run_pytest_mapped(root: Path, files: list[str], python: str, timeout: int) -> dict[str, Any]:
    sources: list[str] = []
    tests = _python_test_candidates(root, files)
    if tests:
        sources.append("name-map")

    # Forward slice: a changed module is also exercised through its importers.
    slice_info = python_importers(root, files)
    importers = [f for f in slice_info.get("files") or [] if f not in files]
    if importers:
        extra = [t for t in _python_test_candidates(root, importers) if t not in tests]
        if extra:
            tests.extend(extra)
            sources.append("import-graph")

    coverage_hint = coverage_context_tests(root, files)
    if not tests:
        return {
            "ok": True,
            "available": True,
            "runner": "pytest-mapped",
            "selected": 0,
            "skipped_estimate": None,
            "tests": [],
            "failures": "",
            "selection_sources": [],
            "coverage_contexts_used": False,
            "summary": "No safely mapped pytest tests; full suite was not run.",
            "install": coverage_hint.get("install")
            or "pip install pytest-testmon, or record contexts with pytest --cov --cov-context=test",
        }
    result = run_cmd([python, "-m", "pytest", "-q", "--tb=line", *tests[:50]], cwd=root, timeout=timeout)
    payload = _pytest_result(result, "pytest-mapped", tests)
    payload["selection_sources"] = sources
    payload["coverage_contexts_used"] = False
    if coverage_hint.get("install"):
        payload["install"] = coverage_hint["install"]
    return payload


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


def _java_fq_test(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    for marker in ("java", "kotlin"):
        if marker in parts:
            return ".".join(parts[parts.index(marker) + 1:])
    return path.stem


def _java_test_inventory(root: Path) -> list[str]:
    tests: list[str] = []
    patterns = ("*Test.java", "*Tests.java", "*IT.java", "*Test.kt", "*Tests.kt", "*IT.kt")
    for marker in ("java", "kotlin"):
        for base in root.glob(f"**/src/test/{marker}"):
            if not base.is_dir():
                continue
            for pattern in patterns:
                for path in base.rglob(pattern):
                    fq = _java_fq_test(path)
                    if fq not in tests:
                        tests.append(fq)
                    if len(tests) >= 4000:
                        return tests
    return tests


def _java_test_classes(root: Path, files: list[str]) -> list[str]:
    tests: list[str] = []
    inventory = _java_test_inventory(root)
    for changed in files:
        if Path(changed).suffix.lower() not in (".java", ".kt", ".kts"):
            continue
        stem = Path(changed).stem
        if stem.endswith(("Test", "Tests", "IT")) and (root / changed).is_file():
            fq = _java_fq_test(Path(changed))
            if fq not in tests:
                tests.append(fq)
        for test in inventory:
            simple = test.rsplit(".", 1)[-1]
            if simple in (f"{stem}Test", f"{stem}Tests", f"{stem}IT"):
                tests.append(test)
    return list(dict.fromkeys(tests))[:40]


def _java_static_selection(root: Path, files: list[str]) -> tuple[list[str], list[str]]:
    tests = _java_test_classes(root, files)
    sources = ["name-map"] if tests else []
    graph = java_dependents(root, files, depth=3)
    extra = [test for test in graph.get("tests") or [] if test not in tests]
    if extra:
        tests.extend(extra)
        sources.append("java-static-forward-slice")
    return tests[:40], sources


def _git_diff_for_files(root: Path, files: list[str]) -> str:
    args = ["git", "diff", "--unified=0", "HEAD", "--", *files[:100]]
    result = run_cmd(args, cwd=root, timeout=8)
    text = result.get("output") or ""
    if text:
        return text
    # A caller may pass files from the latest committed change.
    prior = run_cmd(
        ["git", "diff", "--unified=0", "HEAD^", "HEAD", "--", *files[:100]],
        cwd=root,
        timeout=8,
    )
    return prior.get("output") or ""


def _java_metadata(root: Path, info: dict[str, Any], files: list[str]) -> dict[str, Any]:
    jacoco = _jacoco_expansion(root, info, files)
    diff_coverage = (
        jacoco_diff_coverage(root, files, _git_diff_for_files(root, files))
        if info["flags"].get("jacoco")
        else {"available": False, "reason": "JaCoCo not configured"}
    )
    return {"jacoco": jacoco, "diff_coverage": diff_coverage}


def _java_result(
    result: dict[str, Any],
    *,
    runner: str,
    tests: list[str],
    sources: list[str],
    inventory_count: int,
    metadata: dict[str, Any],
    dynamic_selector: bool = False,
) -> dict[str, Any]:
    output = result.get("output") or ""
    jacoco = metadata["jacoco"]
    selected = None if dynamic_selector else len(tests)
    skipped = None if selected is None else max(0, inventory_count - selected)
    return {
        "ok": bool(result.get("ok")),
        "runner": runner,
        "selected": selected,
        "skipped_estimate": skipped,
        "tests": tests,
        "selection_sources": sources,
        "failures": cap_text(output, 2500) if not result.get("ok") else "",
        "summary": cap_text(
            next((line for line in reversed(output.splitlines()) if line.strip()), ""),
            400,
        ),
        "timed_out": bool(result.get("timeout")),
        "dynamic_selector": dynamic_selector,
        "jacoco_detected": bool(jacoco.get("configured")),
        "jacoco_report_parsed": jacoco["parsed"],
        "jacoco_report": jacoco.get("report"),
        "jacoco_used_for_selection": bool(jacoco.get("tests")),
        "uncovered_changed_classes": jacoco["uncovered"],
        "jacoco_diff_coverage": metadata["diff_coverage"],
    }


def _run_dynamic_java(
    root: Path,
    argv: list[str],
    timeout: int,
    *,
    runner: str,
    sources: list[str],
    inventory_count: int,
    metadata: dict[str, Any],
    smart_picker: bool = False,
) -> dict[str, Any]:
    before = java_test_report_snapshot(root)
    result = run_cmd(argv, cwd=root, timeout=timeout)
    reports = fresh_java_test_reports(root, before)
    payload = _java_result(
        result, runner=runner, tests=reports["tests"], sources=sources,
        inventory_count=inventory_count, metadata=metadata, dynamic_selector=True,
    )
    if reports["selected"] or reports["reports"]:
        payload["selected"] = reports["selected"]
        payload["skipped_estimate"] = max(0, inventory_count - reports["selected"])
        payload["test_methods"] = reports["test_methods"]
        payload["test_report_files"] = reports["reports"]
    if smart_picker:
        selection = smart_test_picker_selection(root)
        if selection:
            payload["selection"] = selection
            payload["tests"] = selection["tests"]
            payload["selected"] = selection["selected"]
            payload["skipped_estimate"] = selection["skipped_estimate"]
            payload["selector_status"] = selection["status"]
            payload["selector_reason"] = selection["reason"]
            payload["selector_fell_back_full_suite"] = selection["status"] == "FULL_SUITE"
    return payload


def _jacoco_expansion(root: Path, info: dict[str, Any], files: list[str]) -> dict[str, Any]:
    """Gate a static test-reference expansion with aggregate JaCoCo class coverage."""
    if not info["flags"].get("jacoco"):
        return {
            "tests": [], "parsed": False, "uncovered": [], "report": None,
            "configured": False,
        }
    report = jacoco_covered_classes(root)
    if not report.get("parsed"):
        return {
            "tests": [], "parsed": False, "uncovered": [], "report": None,
            "configured": True, "reason": report.get("reason"),
        }
    refs = java_tests_referencing(root, files, report.get("covered") or set())
    return {
        "tests": refs["tests"],
        "parsed": True,
        "uncovered": refs["uncovered"],
        "report": report.get("report"),
        "configured": True,
    }


def _run_maven(root: Path, info: dict[str, Any], files: list[str], timeout: int) -> dict[str, Any]:
    mvn = info["tools"].get("mvn")
    if not mvn:
        return {"ok": False, "available": False, "runner": "maven", "install": "Install Maven or add mvnw/mvnw.cmd."}
    inventory = _java_test_inventory(root)
    tests, sources = _java_static_selection(root, files)
    metadata = _java_metadata(root, info, files)
    jacoco = metadata["jacoco"]
    extra = [t for t in jacoco["tests"] if t not in tests]
    if extra:
        tests.extend(extra)
        sources.append("jacoco-coverage-gated-static")
    picker_map = smart_test_picker_map(root)
    if info["flags"].get("smart_test_picker") and picker_map:
        payload = _run_dynamic_java(
            root,
            [
                mvn, "-q", "-DfailIfNoTests=false",
                "com.sap.oss.smart-test-picker:smart-test-picker-maven:0.1.0:smart-test",
            ],
            timeout,
            runner="maven-smart-test-picker", sources=["jacoco-per-test-map"],
            inventory_count=len(inventory), metadata=metadata, smart_picker=True,
        )
        payload["coverage_map"] = picker_map
        return payload
    starts_db = any(root.glob("**/.starts"))
    if info["flags"].get("starts") and starts_db:
        return _run_dynamic_java(
            root,
            [mvn, "-q", "-DfailIfNoTests=false", "starts:starts"],
            timeout,
            runner="maven-starts", sources=["starts-static-rts"],
            inventory_count=len(inventory), metadata=metadata,
        )
    ekstazi_db = any(root.glob("**/.ekstazi"))
    if info["flags"].get("ekstazi") and ekstazi_db:
        return _run_dynamic_java(
            root,
            [mvn, "-q", "-DfailIfNoTests=false", "test"],
            timeout,
            runner="maven-ekstazi", sources=["ekstazi-dependency-rts"],
            inventory_count=len(inventory), metadata=metadata,
        )
    if tests:
        result = run_cmd(
            [
                mvn, "-q", "-DfailIfNoTests=false",
                "-Dsurefire.failIfNoSpecifiedTests=false",
                f"-Dtest={','.join(tests)}", "test",
            ],
            cwd=root,
            timeout=timeout,
        )
        return _java_result(
            result, runner="maven-surefire-subset", tests=tests,
            sources=sources, inventory_count=len(inventory), metadata=metadata,
        )
    return {
        "ok": True, "available": True, "runner": "maven-surefire-subset",
        "selected": 0, "skipped_estimate": len(inventory), "tests": [],
        "selection_sources": [], "summary": "No safely mapped Java tests; full suite was not run.",
        "jacoco_detected": bool(info["flags"].get("jacoco")),
        "jacoco_report_parsed": jacoco["parsed"],
        "jacoco_used_for_selection": False,
        "uncovered_changed_classes": jacoco["uncovered"],
        "jacoco_diff_coverage": metadata["diff_coverage"],
        "ekstazi_detected": bool(info["flags"].get("ekstazi")),
        "smart_test_picker_detected": bool(info["flags"].get("smart_test_picker")),
        "starts_detected": bool(info["flags"].get("starts")),
        "install": (
            "Seed configured RTS once (Smart Test Picker/Ekstazi/STARTS), "
            "or keep the bounded static fallback."
        ),
    }


def _run_gradle(root: Path, info: dict[str, Any], files: list[str], timeout: int) -> dict[str, Any]:
    gradle = info["tools"].get("gradle")
    if not gradle:
        return {"ok": False, "available": False, "runner": "gradle", "install": "Install Gradle or add gradlew/gradlew.bat."}
    inventory = _java_test_inventory(root)
    tests, sources = _java_static_selection(root, files)
    metadata = _java_metadata(root, info, files)
    jacoco = metadata["jacoco"]
    extra = [t for t in jacoco["tests"] if t not in tests]
    if extra:
        tests.extend(extra)
        sources.append("jacoco-coverage-gated-static")
    picker_map = smart_test_picker_map(root)
    if info["flags"].get("smart_test_picker") and picker_map:
        payload = _run_dynamic_java(
            root,
            [gradle, "-q", "selectTests", "smartTest"],
            timeout,
            runner="gradle-smart-test-picker", sources=["jacoco-per-test-map"],
            inventory_count=len(inventory), metadata=metadata, smart_picker=True,
        )
        payload["coverage_map"] = picker_map
        return payload
    if info["flags"].get("affected_tests"):
        payload = _run_dynamic_java(
            root, [gradle, "-q", "affectedTest"], timeout,
            runner="gradle-affected-test", sources=["gradle-affected-tests"],
            inventory_count=len(inventory), metadata=metadata,
        )
        payload["selector_may_fallback_full_suite"] = True
        return payload
    if info["flags"].get("ekstazi") and any(root.glob("**/.ekstazi")):
        return _run_dynamic_java(
            root, [gradle, "-q", "test"], timeout,
            runner="gradle-ekstazi", sources=["ekstazi-dependency-rts"],
            inventory_count=len(inventory), metadata=metadata,
        )
    if not tests:
        return {
            "ok": True, "available": True, "runner": "gradle-tests-subset", "selected": 0,
            "skipped_estimate": len(inventory), "tests": [], "selection_sources": [],
            "summary": "No safely mapped Java tests; full suite was not run.",
            "jacoco_detected": bool(info["flags"].get("jacoco")),
            "jacoco_diff_coverage": metadata["diff_coverage"],
        }
    args = [gradle, "-q", "test"]
    for test in tests:
        args.extend(["--tests", test])
    result = run_cmd(args, cwd=root, timeout=timeout)
    return _java_result(
        result, runner="gradle-tests-subset", tests=tests,
        sources=sources, inventory_count=len(inventory), metadata=metadata,
    )


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
            result = (
                _run_pytest_coverage_contexts(root, files, python, timeout)
                or _run_pytest_mapped(root, files, python, timeout)
            )
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
