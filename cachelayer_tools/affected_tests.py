"""Bounded test-impact analysis that never silently runs a full suite."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from .workspace_detect import detect
    from .test_selection import (
        coverage_context_tests, jacoco_covered_classes, jacoco_diff_coverage,
        fresh_java_test_reports, java_dependents, java_test_report_snapshot,
        jacoco_per_test_selection, joern_slice_selection, native_rts_selection,
        python_importers, rts_seed_plan, smart_test_picker_map,
        smart_test_picker_selection,
    )
    from .process_util import cap_text, git_changed_files, is_code_path, rel_to_root, run_cmd, which
except ImportError:
    from workspace_detect import detect
    from test_selection import (
        coverage_context_tests, jacoco_covered_classes, jacoco_diff_coverage,
        fresh_java_test_reports, java_dependents, java_test_report_snapshot,
        jacoco_per_test_selection, joern_slice_selection, native_rts_selection,
        python_importers, rts_seed_plan, smart_test_picker_map,
        smart_test_picker_selection,
    )
    from process_util import cap_text, git_changed_files, is_code_path, rel_to_root, run_cmd, which

_FAIL_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)", re.MULTILINE)
_SUMMARY_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|deselected)")


def _bounded_java_env(heap_mb: int = 512) -> dict[str, str]:
    existing = os.environ.get("JAVA_TOOL_OPTIONS", "").strip()
    limits = (
        f"-Xmx{heap_mb}m -XX:CompressedClassSpaceSize=96m "
        "-XX:MaxMetaspaceSize=256m -XX:ReservedCodeCacheSize=128m "
        "-Djdk.attach.allowAttachSelf=true -Djava.security.manager=allow"
    )
    return {"JAVA_TOOL_OPTIONS": f"{existing} {limits}".strip()}


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
    database = root / ".testmondata"
    usable = False
    try:
        if 0 < database.stat().st_size <= 50_000_000:
            conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
            try:
                usable = bool(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                ).fetchone())
            finally:
                conn.close()
    except (OSError, sqlite3.Error):
        usable = False
    if not usable:
        return {
            "ok": False,
            "available": True,
            "runner": "pytest-testmon",
            "selected": 0,
            "tests": [],
            "selection_incomplete": True,
            "summary": "Refused cold pytest-testmon: no seeded usable .testmondata database.",
            "full_suite_refused": True,
            "install": "Seed testmon explicitly with a reviewed baseline before affected-test runs.",
        }
    preview = run_cmd(
        [python, "-m", "pytest", "-q", "--collect-only", "--testmon"],
        cwd=root,
        timeout=min(timeout, 20),
    )
    output = preview.get("output") or ""
    selected_ids = list(dict.fromkeys(
        line.strip() for line in output.splitlines()
        if "::" in line and " " not in line.strip()
    ))
    _, deselected, _ = _summary(output)
    if not preview.get("ok") or deselected is None:
        return {
            "ok": False, "available": True, "runner": "pytest-testmon",
            "selected": 0, "tests": [], "selection_incomplete": True,
            "summary": "Refused pytest-testmon because collect-only did not prove a bounded selection.",
            "full_suite_refused": True,
        }
    if len(selected_ids) > 50:
        return {
            "ok": False, "available": True, "runner": "pytest-testmon",
            "selected": 0, "tests": selected_ids[:50], "selection_incomplete": True,
            "summary": "Refused pytest-testmon because selected tests exceeded the cap.",
            "full_suite_refused": True,
        }
    if not selected_ids:
        return {
            "ok": True, "available": True, "runner": "pytest-testmon",
            "selected": 0, "skipped_estimate": deselected, "tests": [],
            "selection_incomplete": False,
            "summary": "Seeded pytest-testmon selected zero tests; no suite was run.",
        }
    result = run_cmd(
        [python, "-m", "pytest", "-q", "--tb=line", *selected_ids],
        cwd=root, timeout=max(1, timeout - min(timeout, 20)),
    )
    payload = _pytest_result(result, "pytest-testmon", selected_ids)
    payload["selection_proven"] = True
    payload["selection_incomplete"] = False
    return payload


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
    if ctx.get("selection_incomplete"):
        return {
            "ok": False, "available": True, "runner": "pytest-coverage-contexts",
            "selected": 0, "tests": tests[:50], "selection_incomplete": True,
            "full_suite_refused": True,
            "summary": "Refused coverage-context selection because the test cap truncated results.",
        }
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

    if len(tests) >= 50 or slice_info.get("selection_incomplete"):
        return {
            "ok": False, "available": True, "runner": "pytest-mapped",
            "selected": 0, "tests": tests[:50], "selection_incomplete": True,
            "full_suite_refused": True, "selection_sources": sources,
            "summary": "Refused mapped pytest selection because a scan/test cap may have truncated results.",
        }

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
    if len(source_files) > 30:
        return {
            "ok": False, "available": True, "runner": "jest-findRelatedTests",
            "selected": 0, "tests": [], "selection_incomplete": True,
            "full_suite_refused": True,
            "changed_files": source_files[:30],
            "summary": "Refused Jest selection because changed JS/TS inputs exceeded the cap.",
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
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if name not in {"node_modules", ".git", "build", "target", ".gradle"}
        ]
        parts = Path(dirpath).relative_to(root).parts
        in_tests = any(
            parts[index:index + 3] in (("src", "test", "java"), ("src", "test", "kotlin"))
            for index in range(max(0, len(parts) - 2))
        )
        for name in filenames:
            if not in_tests or not name.endswith(
                ("Test.java", "Tests.java", "IT.java", "Test.kt", "Tests.kt", "IT.kt")
            ):
                continue
            visited += 1
            path = Path(dirpath) / name
            fq = _java_fq_test(path)
            if fq not in tests:
                tests.append(fq)
            if visited >= 4000:
                return tests
    return tests


def _gradle_subset_args(root: Path, gradle: str, tests: list[str]) -> list[str]:
    """Use module-qualified test tasks when test sources reveal module identity."""
    task_by_test: dict[str, str] = {}
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if name not in {"node_modules", ".git", "build", "target", ".gradle"}
        ]
        for name in filenames:
            if not name.endswith((".java", ".kt")):
                continue
            visited += 1
            if visited > 4000:
                break
            path = Path(dirpath) / name
            rel = path.relative_to(root)
            if "src" not in rel.parts or "test" not in rel.parts:
                continue
            fq = _java_fq_test(rel)
            module_parts = rel.parts[:rel.parts.index("src")]
            task_by_test[fq] = (
                ":" + ":".join(module_parts) + ":test" if module_parts else "test"
            )
        if visited > 4000:
            break
    tasks = list(dict.fromkeys(task_by_test.get(test, "test") for test in tests))
    args = [gradle, "-q", *tasks]
    for test in tests:
        args.extend(["--tests", test])
    return args


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


def _java_static_selection(
    root: Path, files: list[str], info: dict[str, Any]
) -> tuple[list[str], list[str], dict[str, Any]]:
    tests = _java_test_classes(root, files)
    sources = ["name-map"] if tests else []
    graph = java_dependents(root, files, depth=3)
    extra = [test for test in graph.get("tests") or [] if test not in tests]
    if extra:
        tests.extend(extra)
        sources.append("java-bounded-type-import-forward-slice")
    joern = joern_slice_selection(
        root,
        files,
        info.get("tools", {}).get("joern-slice"),
        info.get("tools", {}).get("joern-parse"),
    )
    joern_tests = [test for test in joern.get("tests") or [] if test not in tests]
    if joern_tests:
        tests.extend(joern_tests)
        sources.append(str(joern.get("source") or "joern-cpg-usage-dataflow-slice"))
    joern["selection_incomplete"] = bool(
        joern.get("selection_incomplete") or len(tests) >= 40
        or graph.get("selection_incomplete")
    )
    return tests[:40], sources, joern


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
    per_test = jacoco_per_test_selection(root, files)
    diff_coverage = (
        jacoco_diff_coverage(root, files, _git_diff_for_files(root, files))
        if info["flags"].get("jacoco")
        else {"available": False, "reason": "JaCoCo not configured"}
    )
    scalpel_available = bool(info.get("flags", {}).get("scalpel"))
    return {
        "jacoco": jacoco,
        "jacoco_per_test": per_test,
        "diff_coverage": diff_coverage,
        "scalpel": {
            "available": scalpel_available,
            "used": False,
            "reason": (
                "installed Scalpel exposes no verified Java test-impact call/dependency API"
                if scalpel_available else "Scalpel not available"
            ),
        },
    }


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
    output_lines = [line.strip() for line in output.splitlines() if line.strip()]
    selector_summary = next(
        (line for line in reversed(output_lines) if "Affected Tests:" in line),
        output_lines[-1] if output_lines else "",
    )
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
        "summary": cap_text(selector_summary, 400),
        "full_suite_reported": bool(_FULL_SUITE_RE.search(output)),
        "timed_out": bool(result.get("timeout")),
        "dynamic_selector": dynamic_selector,
        "jacoco_detected": bool(jacoco.get("configured")),
        "jacoco_report_parsed": jacoco["parsed"],
        "jacoco_report": jacoco.get("report"),
        "jacoco_used_for_selection": any(
            source in ("jacoco-native-per-test-map", "smart-picker-per-test-jacoco-map")
            for source in sources
        ),
        "jacoco_selection_mode": (
            "per-test-map"
            if any(source in ("jacoco-native-per-test-map", "smart-picker-per-test-jacoco-map") for source in sources)
            else "aggregate-validation-only" if jacoco.get("parsed") else "not-available"
        ),
        "uncovered_changed_classes": jacoco["uncovered"],
        "jacoco_diff_coverage": metadata["diff_coverage"],
        "scalpel": metadata["scalpel"],
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
    native_kind: str | None = None,
    native_preview: str = "",
    inventory: list[str] | None = None,
) -> dict[str, Any]:
    before = java_test_report_snapshot(root)
    result = run_cmd(
        argv, cwd=root, timeout=timeout, memory_mb=None,
        env=_bounded_java_env(),
    )
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
    if native_kind:
        native = native_rts_selection(
            root,
            native_kind,
            inventory_count,
            selector_output=native_preview or (result.get("output") or ""),
            inventory=inventory,
        )
        payload["native_selection"] = native
        if native.get("selected") is not None:
            payload["tests"] = native["tests"]
            payload["selected"] = native["selected"]
            payload["skipped_estimate"] = native["skipped_estimate"]
            payload["selection_count_source"] = native["source"]
    return payload


def _jacoco_expansion(root: Path, info: dict[str, Any], files: list[str]) -> dict[str, Any]:
    """Read aggregate JaCoCo strictly as validation metadata."""
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
    changed: list[str] = []
    for rel in files:
        path = Path(rel)
        if path.suffix.lower() not in (".java", ".kt", ".kts"):
            continue
        parts = list(path.with_suffix("").parts)
        for marker in ("java", "kotlin"):
            if marker in parts:
                parts = parts[parts.index(marker) + 1:]
                break
        changed.append(".".join(parts))
    covered = report.get("covered") or set()
    return {
        "tests": [],
        "parsed": True,
        "uncovered": [name for name in changed if name and name not in covered],
        "report": report.get("report"),
        "configured": True,
        "purpose": "aggregate coverage validation, not per-test selection",
    }


def _run_maven(root: Path, info: dict[str, Any], files: list[str], timeout: int) -> dict[str, Any]:
    mvn = info["tools"].get("mvn")
    if not mvn:
        return {"ok": False, "available": False, "runner": "maven", "install": "Install Maven or add mvnw/mvnw.cmd."}
    inventory = _java_test_inventory(root)
    tests: list[str] = []
    sources: list[str] = []
    joern: dict[str, Any] = {
        "available": False, "tests": [], "pdg": False,
        "reason": "deferred until cheap RTS selectors miss",
    }
    metadata = _java_metadata(root, info, files)
    jacoco = metadata["jacoco"]
    picker_map = smart_test_picker_map(root)
    if info["flags"].get("smart_test_picker") and picker_map:
        prior = smart_test_picker_selection(root)
        if not prior or prior.get("status") != "SELECTED" or prior.get("selection_incomplete"):
            return {
                "ok": False, "available": True, "runner": "maven-smart-test-picker",
                "selected": 0, "tests": [], "skipped_estimate": len(inventory),
                "selection_sources": ["smart-picker-per-test-jacoco-map"],
                "summary": "Refused Smart Test Picker because prior output was not a complete SELECTED result.",
                "full_suite_refused": True, "selection": prior,
            }
        mapped = [str(test).split("#", 1)[0] for test in prior.get("tests") or []]
        if not mapped:
            return {
                "ok": True, "available": True, "runner": "maven-smart-test-picker",
                "selected": 0, "tests": [], "skipped_estimate": len(inventory),
                "selection_sources": ["smart-picker-per-test-jacoco-map"],
                "summary": "Smart Test Picker explicitly selected zero tests; no suite was run.",
                "selection": prior,
            }
        result = run_cmd(
            [mvn, "-q", "-DfailIfNoTests=false",
             "-Dsurefire.failIfNoSpecifiedTests=false",
             f"-Dtest={','.join(mapped)}", "test"],
            cwd=root, timeout=timeout,
        )
        payload = _java_result(
            result, runner="maven-smart-test-picker", tests=mapped,
            sources=["smart-picker-per-test-jacoco-map"],
            inventory_count=len(inventory), metadata=metadata,
        )
        payload["coverage_map"] = picker_map
        payload["selection"] = prior
        return payload
    starts_db = bool(info["flags"].get("starts_seeded"))
    if info["flags"].get("starts") and starts_db:
        preview = run_cmd(
            [mvn, "--no-transfer-progress", "-DupdateSelectChecksums=false", "starts:select"],
            cwd=root, timeout=min(timeout, 60),
        )
        selection = native_rts_selection(
            root, "starts", len(inventory),
            selector_output=preview.get("output") or "", inventory=inventory,
        )
        mapped = selection.get("tests") or []
        if not preview.get("ok") or not mapped or selection.get("selection_incomplete"):
            return {
                "ok": False, "available": True, "runner": "maven-starts",
                "selected": 0, "tests": [], "selection_incomplete": True,
                "full_suite_refused": True,
                "summary": "Refused STARTS execution because starts:select did not prove a complete non-empty subset.",
                "selection": selection,
            }
        result = run_cmd(
            [mvn, "-q", "-DfailIfNoTests=false",
             "-Dsurefire.failIfNoSpecifiedTests=false",
             f"-Dtest={','.join(mapped)}", "test"],
            cwd=root, timeout=timeout,
        )
        payload = _java_result(
            result, runner="maven-starts", tests=mapped,
            sources=["starts-select-goal"], inventory_count=len(inventory),
            metadata=metadata,
        )
        payload["selection"] = selection
        return payload
    ekstazi_db = bool(info["flags"].get("ekstazi_seeded"))
    if info["flags"].get("ekstazi") and ekstazi_db:
        preview = run_cmd(
            [mvn, "--no-transfer-progress", "ekstazi:predict"],
            cwd=root, timeout=min(timeout, 60),
        )
        selection = native_rts_selection(
            root, "ekstazi", len(inventory),
            selector_output=preview.get("output") or "", inventory=inventory,
        )
        mapped = selection.get("tests") or []
        if not preview.get("ok") or not mapped or selection.get("selection_incomplete"):
            return {
                "ok": False, "available": True, "runner": "maven-ekstazi",
                "selected": 0, "tests": [], "selection_incomplete": True,
                "full_suite_refused": True,
                "summary": "Refused Ekstazi execution because predict did not prove a complete non-empty subset.",
                "selection": selection,
            }
        result = run_cmd(
            [mvn, "-q", "-DfailIfNoTests=false",
             "-Dsurefire.failIfNoSpecifiedTests=false",
             f"-Dtest={','.join(mapped)}", "test"],
            cwd=root, timeout=timeout,
        )
        payload = _java_result(
            result, runner="maven-ekstazi", tests=mapped,
            sources=["ekstazi-predict-goal"], inventory_count=len(inventory),
            metadata=metadata,
        )
        payload["selection"] = selection
        return payload
    native_jacoco = metadata["jacoco_per_test"]
    if native_jacoco.get("available") and native_jacoco.get("tests"):
        if native_jacoco.get("selection_incomplete"):
            return {
                "ok": False, "available": True, "runner": "maven-surefire-subset",
                "selected": 0, "tests": native_jacoco["tests"][:40],
                "selection_incomplete": True, "full_suite_refused": True,
                "summary": "Refused JaCoCo selection because the manifest test cap truncated results.",
            }
        mapped = native_jacoco["tests"][:40]
        result = run_cmd(
            [
                mvn, "-q", "-DfailIfNoTests=false",
                "-Dsurefire.failIfNoSpecifiedTests=false",
                f"-Dtest={','.join(mapped)}", "test",
            ],
            cwd=root,
            timeout=timeout,
        )
        payload = _java_result(
            result, runner="maven-surefire-subset", tests=mapped,
            sources=["jacoco-native-per-test-map"], inventory_count=len(inventory),
            metadata=metadata,
        )
        payload["coverage_map"] = native_jacoco
        payload["joern"] = joern
        return payload
    tests, sources, joern = _java_static_selection(root, files, info)
    if joern.get("selection_incomplete"):
        return {
            "ok": False, "available": True, "runner": "maven-surefire-subset",
            "selected": 0, "tests": tests, "selection_incomplete": True,
            "full_suite_refused": True, "selection_sources": sources,
            "summary": "Refused static Java selection because a scan/test cap truncated results.",
            "joern": joern,
        }
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
        payload = _java_result(
            result, runner="maven-surefire-subset", tests=tests,
            sources=sources, inventory_count=len(inventory), metadata=metadata,
        )
        payload["joern"] = joern
        return payload
    return {
        "ok": True, "available": True, "runner": "maven-surefire-subset",
        "selected": 0, "skipped_estimate": len(inventory), "tests": [],
        "selection_sources": [], "summary": "No safely mapped Java tests; full suite was not run.",
        "jacoco_detected": bool(info["flags"].get("jacoco")),
        "jacoco_report_parsed": jacoco["parsed"],
        "jacoco_used_for_selection": False,
        "jacoco_selection_mode": "aggregate-validation-only" if jacoco["parsed"] else "not-available",
        "uncovered_changed_classes": jacoco["uncovered"],
        "jacoco_diff_coverage": metadata["diff_coverage"],
        "scalpel": metadata["scalpel"],
        "ekstazi_detected": bool(info["flags"].get("ekstazi")),
        "smart_test_picker_detected": bool(info["flags"].get("smart_test_picker")),
        "starts_detected": bool(info["flags"].get("starts")),
        "joern": joern,
        "jacoco_per_test": metadata["jacoco_per_test"],
        "rts_seed": rts_seed_plan(root, info),
        "install": (
            "Seed configured RTS once (Smart Test Picker/Ekstazi/STARTS), "
            "or keep the bounded static fallback."
        ),
    }


_FULL_SUITE_RE = re.compile(
    r"\b(?:FULL_SUITE|ALL_TESTS)\b"
    r"|full[_ -]?suite\s*(?:fallback|mode|status)?\s*[:=]\s*(?:true|yes|enabled)"
    r"|fall(?:ing)?\s+back\s+to\s+(?:the\s+)?full\s+(?:test\s+)?suite",
    re.IGNORECASE,
)


def _affected_test_inspection(root: Path, gradle: str, timeout: int) -> dict[str, Any]:
    remediated_build_files: list[str] = []
    for build_name in ("build.gradle", "build.gradle.kts"):
        path = root / build_name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _FULL_SUITE_RE.search(text):
            remediated_build_files.append(build_name)
    fd, init_name = tempfile.mkstemp(prefix="cachelayer-affected-tests-", suffix=".init.gradle")
    os.close(fd)
    init_script = Path(init_name)
    init_script.write_text(
        """
gradle.projectsEvaluated {
    allprojects { p ->
        def ext = p.extensions.findByName("affectedTests")
        if (ext != null) {
            [
                onEmptyDiff: "skipped",
                onAllFilesIgnored: "skipped",
                onAllFilesOutOfScope: "skipped",
                onUnmappedFile: "selected",
                onDiscoveryEmpty: "skipped",
                onDiscoveryIncomplete: "selected"
            ].each { key, value ->
                if (ext.hasProperty(key)) {
                    ext.setProperty(key, value)
                }
            }
        }
    }
}
""".strip() + "\n",
        encoding="utf-8",
    )
    inspection = run_cmd(
        [
            gradle, "--no-daemon", "-q", "-I", str(init_script),
            "affectedTest", "--explain", "--explain-format=json",
        ],
        cwd=root,
        timeout=min(timeout, 120),
        memory_mb=None,
        env=_bounded_java_env(),
    )
    output = inspection.get("output") or ""
    if not inspection.get("ok"):
        unsupported_json = re.search(
            r"(?:unknown|unrecognized|unsupported).{0,80}explain-format"
            r"|explain-format.{0,80}(?:unknown|unrecognized|unsupported)",
            output,
            re.IGNORECASE | re.DOTALL,
        )
        if unsupported_json:
            inspection = run_cmd(
                [
                    gradle, "--no-daemon", "-q", "-I", str(init_script),
                    "affectedTest", "--explain",
                ],
                cwd=root, timeout=min(timeout, 120), memory_mb=None,
                env=_bounded_java_env(),
            )
            output = inspection.get("output") or ""
    if not inspection.get("ok"):
        return {
            "safe": False,
            "reason": "affectedTest --explain failed with the safety init script",
            "output": output,
            "init_script": str(init_script),
        }
    if _FULL_SUITE_RE.search(output):
        return {
            "safe": False,
            "reason": "affectedTest --explain reported FULL_SUITE or ALL_TESTS",
            "output": output,
            "init_script": str(init_script),
        }
    statuses: list[str] = []
    selected_tests: list[str] = []
    status_keys = {"status", "mode", "selectionstatus", "selection_status"}
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        for key, child in value.items():
            if str(key).lower() in status_keys:
                statuses.append(str(child).upper())
            if str(key).lower() in {"tests", "selectedtests", "affectedtests"} and isinstance(child, list):
                selected_tests.extend(str(item).split("#", 1)[0] for item in child if isinstance(item, str))
        for container in ("selection", "result", "affectedTests", "affected_tests"):
            child = value.get(container)
            if not isinstance(child, dict):
                continue
            for key, status in child.items():
                if str(key).lower() in status_keys:
                    statuses.append(str(status).upper())
    if not statuses:
        match = re.search(r"\bAffected Tests:\s*([A-Z_]+)\b", output, re.IGNORECASE)
        if match:
            statuses.append(match.group(1).upper())
    if "SELECTED" not in statuses:
        return {
            "safe": False,
            "reason": "affectedTest --explain had an unknown or non-SELECTED schema/status",
            "output": output,
            "statuses": statuses,
            "init_script": str(init_script),
        }
    return {
        "safe": True,
        "reason": "affectedTest --explain explicitly reported SELECTED under runtime safety policy",
        "output": output,
        "statuses": statuses,
        "init_script": str(init_script),
        "runtime_overrides": remediated_build_files,
        "tests": list(dict.fromkeys(selected_tests))[:40],
        "selection_incomplete": len(set(selected_tests)) > 40,
    }


def _run_gradle(root: Path, info: dict[str, Any], files: list[str], timeout: int) -> dict[str, Any]:
    gradle = info["tools"].get("gradle")
    if not gradle:
        return {"ok": False, "available": False, "runner": "gradle", "install": "Install Gradle or add gradlew/gradlew.bat."}
    inventory = _java_test_inventory(root)
    tests: list[str] = []
    sources: list[str] = []
    joern: dict[str, Any] = {
        "available": False, "tests": [], "pdg": False,
        "reason": "deferred until cheap RTS selectors miss",
    }
    metadata = _java_metadata(root, info, files)
    jacoco = metadata["jacoco"]
    picker_map = smart_test_picker_map(root)
    if info["flags"].get("smart_test_picker") and picker_map:
        prior = smart_test_picker_selection(root)
        if not prior or prior.get("status") != "SELECTED" or prior.get("selection_incomplete"):
            return {
                "ok": False, "available": True, "runner": "gradle-smart-test-picker",
                "selected": 0, "tests": [], "skipped_estimate": len(inventory),
                "selection_sources": ["smart-picker-per-test-jacoco-map"],
                "summary": "Refused Smart Test Picker because prior output was not a complete SELECTED result.",
                "full_suite_refused": True, "selection": prior,
            }
        mapped = [str(test).split("#", 1)[0] for test in prior.get("tests") or []]
        if not mapped:
            return {
                "ok": True, "available": True, "runner": "gradle-smart-test-picker",
                "selected": 0, "tests": [], "skipped_estimate": len(inventory),
                "selection_sources": ["smart-picker-per-test-jacoco-map"],
                "summary": "Smart Test Picker explicitly selected zero tests; no suite was run.",
                "selection": prior,
            }
        args = _gradle_subset_args(root, gradle, mapped)
        result = run_cmd(args, cwd=root, timeout=timeout)
        payload = _java_result(
            result, runner="gradle-smart-test-picker", tests=mapped,
            sources=["smart-picker-per-test-jacoco-map"],
            inventory_count=len(inventory), metadata=metadata,
        )
        payload["coverage_map"] = picker_map
        payload["selection"] = prior
        return payload
    if info["flags"].get("starts") and info["flags"].get("starts_seeded"):
        return {
            "ok": False, "available": True, "runner": "gradle-starts",
            "selected": 0, "tests": [], "selection_incomplete": True,
            "full_suite_refused": True,
            "summary": "Refused Gradle STARTS: no bounded pre-execution selection/explain contract is available.",
        }
    if info["flags"].get("ekstazi") and info["flags"].get("ekstazi_seeded"):
        return {
            "ok": False, "available": True, "runner": "gradle-ekstazi",
            "selected": 0, "tests": [], "selection_incomplete": True,
            "full_suite_refused": True,
            "summary": "Refused Gradle Ekstazi: no bounded pre-execution selection/explain contract is available.",
        }
    if info["flags"].get("affected_tests"):
        inspection = _affected_test_inspection(root, gradle, timeout)
        if not inspection["safe"]:
            init_script = inspection.get("init_script")
            if init_script:
                Path(init_script).unlink(missing_ok=True)
            return {
                "ok": False, "available": True, "runner": "gradle-affected-test",
                "selected": 0, "tests": [], "skipped_estimate": len(inventory),
                "selection_sources": ["gradle-affected-tests"],
                "summary": f"Refused affectedTest: {inspection['reason']}.",
                "full_suite_refused": True,
                "inspection": inspection,
                "install": "Configure affectedTest with full-suite fallback disabled and make --explain succeed.",
            }
        mapped = inspection.get("tests") or []
        if not mapped or inspection.get("selection_incomplete"):
            Path(inspection["init_script"]).unlink(missing_ok=True)
            return {
                "ok": False, "available": True, "runner": "gradle-affected-test",
                "selected": 0, "tests": [], "selection_incomplete": True,
                "full_suite_refused": True,
                "summary": "Refused affectedTest because explain did not provide a complete explicit test subset.",
                "inspection": inspection,
            }
        try:
            args = _gradle_subset_args(root, gradle, mapped)
            args.insert(1, "--no-daemon")
            result = run_cmd(args, cwd=root, timeout=timeout)
            payload = _java_result(
                result, runner="gradle-affected-test", tests=mapped,
                sources=["gradle-affected-tests"], inventory_count=len(inventory),
                metadata=metadata,
            )
        finally:
            Path(inspection["init_script"]).unlink(missing_ok=True)
        payload["inspection"] = inspection
        if payload.get("full_suite_reported"):
            payload.update({
                "ok": False,
                "full_suite_refused": True,
                "summary": "affectedTest ignored the safety policy and reported FULL_SUITE",
            })
        return payload
    native_jacoco = metadata["jacoco_per_test"]
    if native_jacoco.get("available") and native_jacoco.get("tests"):
        if native_jacoco.get("selection_incomplete"):
            return {
                "ok": False, "available": True, "runner": "gradle-tests-subset",
                "selected": 0, "tests": native_jacoco["tests"][:40],
                "selection_incomplete": True, "full_suite_refused": True,
                "summary": "Refused JaCoCo selection because the manifest test cap truncated results.",
            }
        mapped = native_jacoco["tests"][:40]
        args = _gradle_subset_args(root, gradle, mapped)
        result = run_cmd(args, cwd=root, timeout=timeout)
        payload = _java_result(
            result, runner="gradle-tests-subset", tests=mapped,
            sources=["jacoco-native-per-test-map"], inventory_count=len(inventory),
            metadata=metadata,
        )
        payload["coverage_map"] = native_jacoco
        payload["joern"] = joern
        return payload
    tests, sources, joern = _java_static_selection(root, files, info)
    if joern.get("selection_incomplete"):
        return {
            "ok": False, "available": True, "runner": "gradle-tests-subset",
            "selected": 0, "tests": tests, "selection_incomplete": True,
            "full_suite_refused": True, "selection_sources": sources,
            "summary": "Refused static Java selection because a scan/test cap truncated results.",
            "joern": joern,
        }
    if not tests:
        return {
            "ok": True, "available": True, "runner": "gradle-tests-subset", "selected": 0,
            "skipped_estimate": len(inventory), "tests": [], "selection_sources": [],
            "summary": "No safely mapped Java tests; full suite was not run.",
            "jacoco_detected": bool(info["flags"].get("jacoco")),
            "jacoco_report_parsed": jacoco["parsed"],
            "jacoco_used_for_selection": False,
            "jacoco_selection_mode": "aggregate-validation-only" if jacoco["parsed"] else "not-available",
            "jacoco_diff_coverage": metadata["diff_coverage"],
            "scalpel": metadata["scalpel"],
            "jacoco_per_test": metadata["jacoco_per_test"],
            "joern": joern,
            "rts_seed": rts_seed_plan(root, info),
        }
    args = _gradle_subset_args(root, gradle, tests)
    result = run_cmd(args, cwd=root, timeout=timeout)
    payload = _java_result(
        result, runner="gradle-tests-subset", tests=tests,
        sources=sources, inventory_count=len(inventory), metadata=metadata,
    )
    payload["joern"] = joern
    return payload


def run_affected_tests(
    changed_files: list[str] | None = None,
    *,
    cwd: str | None = None,
    timeout: int = 45,
    seed_rts: bool = False,
) -> dict[str, Any]:
    info = detect(cwd)
    root = Path(info["root"])
    files = _changed(root, changed_files)
    timeout = max(1, min(int(timeout), 300))
    seed_requested = seed_rts or os.environ.get("CACHELAYER_TIA_SEED_RTS", "").lower() in {
        "1", "true", "yes", "plan",
    }
    if seed_requested:
        if not info.get("java"):
            return {
                "ok": False, "available": False, "changed_files": files[:30],
                "summary": "RTS seed planning applies only to Maven/Gradle Java projects.",
            }
        plan = rts_seed_plan(root, info)
        return {
            "ok": True, "available": True, "runner": "rts-seed-plan",
            "seed_executed": False, "changed_files": files[:30], **plan,
        }

    risky_configs = {
        "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
        "settings.gradle.kts", "package.json", "pytest.ini", "pyproject.toml",
        "setup.cfg", "tox.ini",
    }
    input_count = len(changed_files) if changed_files is not None else len(git_changed_files(root))
    input_incomplete = input_count > len(files)
    unmapped_risk = [
        name for name in files
        if Path(name).name in risky_configs or not (root / name).exists()
    ]
    partitions = {
        "python": [name for name in files if Path(name).suffix.lower() in {".py", ".pyi"}
                   or Path(name).name in {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}],
        "java": [name for name in files if Path(name).suffix.lower() in {".java", ".kt", ".kts"}
                 or Path(name).name in {"pom.xml", "build.gradle", "build.gradle.kts",
                                        "settings.gradle", "settings.gradle.kts"}],
        "javascript": [name for name in files if Path(name).suffix.lower() in {
            ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"
        } or Path(name).name == "package.json"],
    }
    applicable = [
        language for language, names in partitions.items()
        if names and (
            (language == "python" and info.get("python"))
            or (language == "java" and (info.get("maven") or info.get("gradle")))
            or (language == "javascript" and info.get("javascript"))
        )
    ]
    deadline = time.monotonic() + timeout
    results: list[dict[str, Any]] = []

    def remaining() -> int:
        return max(1, int(deadline - time.monotonic()))

    if "python" in applicable:
        python = info["tools"].get("python3")
        if not python:
            result = {"ok": False, "available": False, "runner": "pytest", "install": "Install Python 3 and pytest."}
        elif not info["tools"].get("pytest") and not _can_import_pytest():
            result = {"ok": False, "available": False, "runner": "pytest", "install": "pip install pytest pytest-testmon"}
        elif info["flags"].get("testmon"):
            result = _run_pytest_testmon(
                root, info["tools"].get("analysis-python") or python, remaining(),
            )
        else:
            result = (
                _run_pytest_coverage_contexts(root, partitions["python"], python, remaining())
                or _run_pytest_mapped(root, partitions["python"], python, remaining())
            )
        result["language"] = "python"
        results.append(result)

    if "java" in applicable:
        result = (
            _run_maven(root, info, partitions["java"], remaining())
            if info["maven"] else
            _run_gradle(root, info, partitions["java"], remaining())
        )
        result["language"] = "java"
        results.append(result)

    if "javascript" in applicable:
        result = _run_jest(root, partitions["javascript"], remaining())
        result["language"] = "javascript"
        results.append(result)

    if not results:
        results.append({
            "ok": False,
            "available": False,
            "summary": "No supported test runner detected; no suite was run.",
            "install": "Python: pytest-testmon; JS/TS: Jest; Java: Maven Surefire/Ekstazi or Gradle.",
        })
    if len(results) == 1:
        combined = results[0]
    else:
        selected_values = [item.get("selected") for item in results]
        combined = {
            "ok": all(item.get("ok") for item in results),
            "available": any(item.get("available", True) for item in results),
            "runner": "polyglot-bounded",
            "runners": results,
            "selected": (
                sum(int(value) for value in selected_values)
                if all(isinstance(value, int) for value in selected_values) else None
            ),
            "tests": [
                test for item in results for test in (item.get("tests") or [])
            ][:100],
            "summary": f"Ran {len(results)} bounded language selectors.",
        }
    combined["changed_files"] = files[:30]
    combined["selection_incomplete"] = bool(
        input_incomplete or combined.get("selection_incomplete")
        or any(item.get("selection_incomplete") for item in results)
    )
    if unmapped_risk and not any((item.get("selected") or 0) > 0 for item in results):
        combined.update({
            "ok": False,
            "inconclusive": True,
            "selection_incomplete": True,
            "unmapped_risk": unmapped_risk[:30],
            "summary": "Selection is inconclusive for deleted or build/config changes; zero tests is not a green result.",
        })
    if info.get("java"):
        combined.setdefault("rts_seed", rts_seed_plan(root, info))
    return combined


def _can_import_pytest() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("pytest") is not None
    except (ImportError, ValueError):
        return False
