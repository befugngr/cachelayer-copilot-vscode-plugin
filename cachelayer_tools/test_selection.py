"""Test selection strategies beyond name matching. Stdlib only, bounded, honest about what it used."""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

try:
    from .test_baselines import jacoco_provenance, joern_cpg_path
    from .process_util import SKIP_PARTS, run_cmd
except ImportError:
    from test_baselines import jacoco_provenance, joern_cpg_path
    from process_util import SKIP_PARTS, run_cmd

MAX_SCAN_FILES = 4000
MAX_TESTS = 50
MAX_SQL_PARAMS = 200
MAX_MANIFEST_BYTES = 1_000_000
MAX_JACOCO_ARTIFACT_BYTES = 25_000_000
_READ = {"encoding": "utf-8", "errors": "ignore"}

_JACOCO_REPORTS = (
    "target/site/jacoco/jacoco.xml",
    "target/site/jacoco-aggregate/jacoco.xml",
    "build/reports/jacoco/test/jacocoTestReport.xml",
)
_SMART_MAPS = (
    "coverage-map.json",
    "build/test-coverage-map.json",
    "target/test-coverage-map.json",
    "build/smart-test-picker/coverage-map.json",
    "build/reports/smart-test-picker/coverage-map.json",
    "target/smart-test-picker/coverage-map.json",
    "target/reports/smart-test-picker/coverage-map.json",
)
_SMART_SELECTIONS = (
    "build/selected-tests.json",
    "target/selected-tests.json",
)
_PER_TEST_JACOCO_MANIFESTS = (
    "**/jacoco/per-test/manifest.json",
    "**/jacoco-per-test/manifest.json",
)
_JOERN_OUTPUTS = (
    "joern-slices.json",
    "build/joern-slices.json",
    "target/joern-slices.json",
    ".cache/cachelayer/joern-slices.json",
)


def _walk(root: Path, suffixes: tuple[str, ...], limit: int = MAX_SCAN_FILES) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SKIP_PARTS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in suffixes:
                found.append(path)
                if len(found) >= limit:
                    return found
    return found


def _confined_file(root: Path, raw: Any) -> Path | None:
    value = str(raw or "")
    relative = Path(value)
    if not value or relative.is_absolute() or ".." in relative.parts:
        return None
    try:
        resolved = (root / relative).resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _bounded_sha256(path: Path) -> str | None:
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_JACOCO_ARTIFACT_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            remaining = size
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    return None
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _artifact_is_fresh(root: Path, artifact: Path) -> bool:
    """Require an artifact newer than a bounded scan of relevant project inputs."""
    try:
        artifact_mtime = artifact.stat().st_mtime_ns
    except OSError:
        return False
    seen = 0
    deadline = time.monotonic() + 0.2
    input_names = {
        "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
        "settings.gradle.kts", "package.json", "pytest.ini", "pyproject.toml",
    }
    suffixes = {".py", ".pyi", ".java", ".kt", ".kts", ".js", ".jsx", ".ts", ".tsx"}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SKIP_PARTS]
        for name in filenames:
            if name not in input_names and Path(name).suffix.lower() not in suffixes:
                continue
            seen += 1
            if seen > MAX_SCAN_FILES or time.monotonic() > deadline:
                return False
            try:
                if (Path(dirpath) / name).stat().st_mtime_ns > artifact_mtime:
                    return False
            except OSError:
                return False
    return True


def _named_dirs(root: Path, wanted: str, limit: int = 20) -> list[Path]:
    found: list[Path] = []
    visited = 0
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        visited += 1
        if visited > MAX_SCAN_FILES:
            break
        current = Path(dirpath)
        if current.name == wanted:
            found.append(current)
            dirnames[:] = []
            if len(found) >= limit:
                break
            continue
        dirnames[:] = [name for name in dirnames if name not in SKIP_PARTS]
    return found


def _artifact_files(root: Path, limit: int = MAX_SCAN_FILES) -> list[Path]:
    found: list[Path] = []
    entries = 0
    deadline = time.monotonic() + 0.4
    allowed_build = {"build", "target"}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames if name not in (SKIP_PARTS - allowed_build)
        ]
        for name in filenames:
            entries += 1
            if entries > limit or time.monotonic() > deadline:
                return found
            found.append(Path(dirpath) / name)
    return found


def smart_test_picker_map(root: Path) -> str | None:
    """Return a configured per-test JaCoCo map without searching build trees unboundedly."""
    for rel in _SMART_MAPS:
        path = root / rel
        if path.is_file() and _artifact_is_fresh(root, path):
            return rel
    for path in _artifact_files(root):
        lowered = path.name.lower()
        if (
            path.suffix == ".json" and "coverage" in lowered and "map" in lowered
            and _artifact_is_fresh(root, path)
        ):
            return path.relative_to(root).as_posix()
    return None


def smart_test_picker_selection(root: Path) -> dict[str, Any] | None:
    """Read the selector's explicit output after a Smart Test Picker run."""
    path = next((
        root / rel for rel in _SMART_SELECTIONS
        if (root / rel).is_file() and _artifact_is_fresh(root, root / rel)
    ), None)
    if path is None:
        candidates = [
            candidate for candidate in _artifact_files(root)
            if candidate.name == "selected-tests.json"
        ]
        path = next((
            candidate for candidate in candidates[:200]
            if candidate.is_file() and _artifact_is_fresh(root, candidate)
        ), None)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(**_READ))
    except (OSError, ValueError):
        return None
    selected = payload.get("selectedTests") or []
    unmapped = payload.get("unmappedTests") or {}
    tests = list(dict.fromkeys([
        *[str(item) for item in selected],
        *[str(item) for item in (unmapped.keys() if isinstance(unmapped, dict) else unmapped)],
    ]))
    total_match = re.search(r"\bout of\s+(\d+)\s+total\b", str(payload.get("reason") or ""))
    total = int(total_match.group(1)) if total_match else None
    return {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "tests": tests[:MAX_TESTS],
        "selected": min(len(tests), MAX_TESTS),
        "selection_incomplete": len(tests) > MAX_TESTS,
        "total": total,
        "skipped_estimate": max(0, total - len(tests)) if total is not None else None,
        "file": path.relative_to(root).as_posix(),
    }


def _test_name_from_report(path: Path, report: ET.Element) -> str | None:
    """Infer the owning test from an explicitly per-test JaCoCo report."""
    for key in ("test", "testName"):
        value = (report.get(key) or "").strip()
        if value and value.lower() not in {"unknown", "default"}:
            return value.split("#", 1)[0]
    test_name = re.compile(r"(?:Test|Tests|IT)(?:$|[#.])")
    for key in ("sessionid", "sessionId"):
        value = (report.get(key) or "").strip()
        if value and test_name.search(value):
            return value.split("#", 1)[0]
    session = next(iter(report.iter("sessioninfo")), None)
    if session is not None:
        value = (session.get("id") or "").strip()
        if value and test_name.search(value):
            return value.split("#", 1)[0]
    stem = path.stem
    for prefix in ("session_", "jacoco_", "coverage_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    return stem.replace("__", ".") if re.search(r"(?:Test|Tests|IT)(?:$|[#.])", stem) else None


def jacoco_per_test_selection(root: Path, changed_files: list[str]) -> dict[str, Any]:
    """Select tests only from manifest-labeled, fresh per-test JaCoCo reports.

    Filenames and XML attributes are not proof of ownership. The preparation
    harness records each test's unique exec/XML pair and hashes in a sidecar.
    """
    changed = {_fq_class(rel) for rel in changed_files if Path(rel).suffix.lower() in (".java", ".kt", ".kts")}
    changed.discard("")
    if not changed:
        return {"available": False, "tests": [], "reason": "no changed Java classes"}
    current_provenance = jacoco_provenance(root)
    tests: list[str] = []
    parsed: list[str] = []
    manifests: list[str] = []
    rejected: list[str] = []
    entries: list[tuple[str, Path]] = []
    manifest_candidates = [
        path for path in _artifact_files(root)
        if path.name == "manifest.json"
        and "jacoco" in {part.lower() for part in path.parts}
        and any(part.lower() in {"per-test", "jacoco-per-test"} for part in path.parts)
    ][:20]
    for manifest in manifest_candidates:
            try:
                if manifest.stat().st_size > MAX_MANIFEST_BYTES:
                    raise ValueError("manifest exceeds size bound")
                payload = json.loads(manifest.read_text(**_READ))
            except (OSError, ValueError):
                rejected.append(f"{manifest.relative_to(root).as_posix()}: unreadable manifest")
                continue
            if (
                payload.get("schema") != "cachelayer-jacoco-per-test-v1"
                or payload.get("provenance") != current_provenance
                or payload.get("complete") is not True
                or not isinstance(payload.get("tests"), dict)
                or payload.get("expected_tests") != len(payload.get("tests", {}))
            ):
                rejected.append(f"{manifest.relative_to(root).as_posix()}: stale or unknown manifest")
                continue
            manifests.append(manifest.relative_to(root).as_posix())
            for test, item in list(payload["tests"].items())[:200]:
                if not isinstance(item, dict):
                    continue
                exec_file = _confined_file(root, item.get("exec"))
                xml_file = _confined_file(root, item.get("xml"))
                valid = bool(
                    exec_file is not None and xml_file is not None
                    and exec_file.is_file() and xml_file.is_file()
                    and _bounded_sha256(exec_file) == item.get("exec_sha256")
                    and _bounded_sha256(xml_file) == item.get("xml_sha256")
                )
                if valid:
                    assert xml_file is not None
                    entries.append((str(test).split("#", 1)[0], xml_file))
                else:
                    rejected.append(
                        f"{test}: unsafe, oversized, missing, or hash-mismatched exec/XML pair"
                    )
    for test, path in entries:
        try:
            if path.stat().st_size > MAX_JACOCO_ARTIFACT_BYTES:
                raise OSError("report exceeds size bound")
            report = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        covered: set[str] = set()
        for cls in report.iter("class"):
            name = (cls.get("name") or "").replace("/", ".")
            if not name:
                continue
            if any(
                counter.get("type") in ("INSTRUCTION", "LINE")
                and int(counter.get("covered") or 0) > 0
                for counter in cls.findall("counter")
            ):
                covered.add(name)
        parsed.append(path.relative_to(root).as_posix())
        if any(name in covered or any(c.startswith(name + "$") for c in covered) for name in changed):
            if test not in tests:
                tests.append(test)
    exec_files = [
        path.relative_to(root).as_posix()
        for path in _artifact_files(root)
        if path.name.startswith("jacoco") and path.suffix == ".exec"
    ][:50]
    if not parsed:
        reason = "no fresh manifest-labeled per-test JaCoCo exec/XML pairs"
        if exec_files:
            reason += "; aggregate/binary exec data cannot identify owning tests"
        return {
            "available": False, "tests": [], "reason": reason,
            "exec_files": exec_files,
            "rejected": rejected[:50],
            "install": "Run prepare_tia(jacoco) to create unique exec/XML pairs and their provenance manifest.",
        }
    return {
        "available": True,
        "tests": tests[:MAX_TESTS],
        "selection_incomplete": len(tests) > MAX_TESTS,
        "reports": parsed[:200],
        "manifests": manifests,
        "rejected": rejected[:50],
        "source": "jacoco-manifest-per-test-exec",
        "reason": "fresh manifest-labeled per-test JaCoCo exec/XML pairs",
    }


def starts_selected_tests(output: str) -> list[str]:
    """Parse the official ``starts:select`` AffectedTests log section."""
    tests: list[str] = []
    active = False
    for raw in (output or "").splitlines():
        line = re.sub(r"^\s*\[[A-Z]+\]\s*", "", raw).strip()
        if "AffectedTests" in line and "*" in line:
            active = True
            continue
        if active and line.startswith("*"):
            break
        if not active or not line or line.startswith("["):
            continue
        if re.fullmatch(r"(?:[a-zA-Z_$][\w$]*\.)*[A-Z][\w$]*(?:Test|Tests|IT)", line):
            if line not in tests:
                tests.append(line)
    return tests


def ekstazi_non_affected_tests(output: str) -> list[str]:
    """Parse the official ``ekstazi:predict`` NonAffected records."""
    tests: list[str] = []
    for value in re.findall(r"NonAffected::\s+([^\s]+)", output or ""):
        test = value.removesuffix(".java").replace("/", ".").replace("\\", ".")
        if test and test not in tests:
            tests.append(test)
    return tests


def native_rts_selection(
    root: Path,
    kind: str,
    inventory_count: int | None = None,
    *,
    selector_output: str = "",
    inventory: list[str] | None = None,
) -> dict[str, Any]:
    """Read official selector output first, then native artifacts as fallback."""
    if kind == "starts":
        selected_tests = starts_selected_tests(selector_output)
        if selected_tests or "AffectedTests" in selector_output:
            incomplete = len(selected_tests) > MAX_TESTS
            selected_tests = selected_tests[:MAX_TESTS]
            selected = len(selected_tests)
            return {
                "available": True,
                "tests": selected_tests,
                "selected": selected,
                "skipped_estimate": (
                    max(0, inventory_count - selected)
                    if inventory_count is not None else None
                ),
                "artifacts": [],
                "source": "starts-select-goal",
                "selection_incomplete": incomplete,
            }
    elif selector_output and inventory is not None:
        non_affected = set(ekstazi_non_affected_tests(selector_output))
        if non_affected or "NonAffected::" in selector_output:
            selected_tests = [test for test in inventory if test not in non_affected]
            incomplete = len(selected_tests) > MAX_TESTS
            selected_tests = selected_tests[:MAX_TESTS]
            return {
                "available": True,
                "tests": selected_tests,
                "selected": len(selected_tests),
                "skipped_estimate": len(non_affected),
                "artifacts": [],
                "source": "ekstazi-predict-goal",
                "selection_incomplete": incomplete,
            }

    base_name = ".starts" if kind == "starts" else ".ekstazi"
    bases = [
        path for path in _named_dirs(root, base_name)
        if _artifact_is_fresh(root, path)
    ]
    tests: list[str] = []
    files: list[str] = []
    test_re = re.compile(r"\b(?:[a-zA-Z_$][\w$]*\.)*[A-Z][\w$]*(?:Test|Tests|IT)(?:#[\w$]+)?\b")
    for base in bases:
        visited = 0
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [name for name in dirnames if name not in SKIP_PARTS]
            for name in filenames:
                visited += 1
                if visited > 1000:
                    break
                path = Path(dirpath) / name
                files.append(path.relative_to(root).as_posix())
                names = test_re.findall(path.name.replace("-", "."))
                if path.stat().st_size <= 256_000:
                    try:
                        names.extend(test_re.findall(path.read_text(**_READ)))
                    except OSError:
                        pass
                for candidate in names:
                    value = candidate.split("#", 1)[0].removesuffix(".clz")
                    if value not in tests:
                        tests.append(value)
                    if len(tests) >= MAX_TESTS:
                        break
            if len(tests) >= MAX_TESTS or visited > 1000:
                break
    selected = len(tests) if tests else None
    return {
        "available": bool(bases),
        "tests": tests,
        "selected": selected,
        "skipped_estimate": (
            max(0, inventory_count - selected)
            if inventory_count is not None and selected is not None else None
        ),
        "artifacts": files[:100],
        "source": f"{kind}-native-artifacts" if tests else f"{kind}-report-diff-fallback",
        "selection_incomplete": len(files) >= 1000 or len(tests) >= MAX_TESTS,
    }


def rts_seed_plan(root: Path, info: dict[str, Any]) -> dict[str, Any]:
    """Return a non-mutating install/seed plan for Java RTS selectors."""
    flags = info.get("flags") or {}
    inventory = info.get("artifacts") or {}
    maven = bool(info.get("maven"))
    gradle = bool(info.get("gradle"))
    artifacts = {
        "smart_test_picker_map": smart_test_picker_map(root),
        "starts_database": next(iter(inventory.get("starts") or []), None),
        "ekstazi_database": next(iter(inventory.get("ekstazi") or []), None),
        "jacoco_per_test_reports": bool(inventory.get("jacoco_per_test")),
    }
    options: list[dict[str, Any]] = []
    if maven:
        options.extend([
            {
                "selector": "smart-test-picker",
                "configured": bool(flags.get("smart_test_picker")),
                "seeded": bool(artifacts["smart_test_picker_map"]),
                "guidance": "Add Smart Test Picker in an explicit Maven profile, then run its documented baseline goal once.",
                "additive_profile": (
                    "<profile><id>cachelayer-rts-smart-picker</id><build><plugins><plugin>"
                    "<groupId>com.sap.oss.smart-test-picker</groupId>"
                    "<artifactId>smart-test-picker-maven</artifactId>"
                    "<version>${smart-test-picker.version}</version>"
                    "</plugin></plugins></build></profile>"
                ),
            },
            {
                "selector": "starts",
                "configured": bool(flags.get("starts")),
                "seeded": bool(artifacts["starts_database"]),
                "guidance": "Add starts-maven-plugin in an explicit profile; run a baseline starts:starts only after reviewing its config.",
                "additive_profile": (
                    "<profile><id>cachelayer-rts-starts</id><build><plugins><plugin>"
                    "<groupId>edu.illinois</groupId><artifactId>starts-maven-plugin</artifactId>"
                    "<version>${starts.version}</version>"
                    "</plugin></plugins></build></profile>"
                ),
            },
            {
                "selector": "ekstazi",
                "configured": bool(flags.get("ekstazi")),
                "seeded": bool(artifacts["ekstazi_database"]),
                "guidance": "Add the Ekstazi Maven plugin in an explicit profile and run one reviewed baseline test.",
                "additive_profile": (
                    "<profile><id>cachelayer-rts-ekstazi</id><build><plugins><plugin>"
                    "<groupId>org.ekstazi</groupId><artifactId>ekstazi-maven-plugin</artifactId>"
                    "<version>${ekstazi.version}</version>"
                    "</plugin></plugins></build></profile>"
                ),
            },
        ])
    if gradle:
        options.append({
            "selector": "affectedTest",
            "configured": bool(flags.get("affected_tests")),
            "seeded": bool(flags.get("affected_tests")),
            "guidance": "Apply affectedtests explicitly, configure full-suite fallback off, and verify affectedTest --explain before execution.",
            "additive_snippet": (
                "plugins { id(\"io.github.vedanthvdev.affectedtests\") "
                "version \"[PIN_REVIEWED_VERSION]\" }\n"
                "// Configure the plugin's documented full-suite fallback setting to false."
            ),
        })
    return {
        "plan_only": True,
        "mutated_build_files": False,
        "artifacts": artifacts,
        "options": options,
        "summary": "No build files or seed databases were changed; review one additive profile/snippet and baseline command.",
    }


def _joern_record_kind(record: Any) -> str | None:
    """Classify a Joern/legacy slice record; unknown schemas are rejected."""
    if not isinstance(record, dict):
        return None
    if "callers" in record and ("target" in record or "callee" in record):
        return "legacy-callers"
    if any(key in record for key in ("objectSlices", "userDefinedTypes", "programUsageSlice")):
        return "program-usage-slice"
    if "nodes" in record and "edges" in record:
        return "data-flow-slice"
    if any(key in record for key in ("variable", "definedBy", "usedAt", "fileName", "lineNumber")):
        return "program-usage-slice"
    return None


def _joern_tests_from_record(record: dict[str, Any], changed_symbols: set[str]) -> list[str]:
    tests: list[str] = []
    kind = _joern_record_kind(record)
    if kind is None:
        return tests

    def _consider(name: str) -> None:
        if not re.fullmatch(r"(?:[a-zA-Z_$][\w$]*\.)*[A-Z][\w$]*(?:Test|Tests|IT)", name):
            return
        if name not in tests:
            tests.append(name)

    if kind == "legacy-callers":
        target = str(record.get("target") or record.get("callee") or "")
        if changed_symbols and not any(symbol in target for symbol in changed_symbols):
            # Still allow callers when the target string embeds a changed type.
            blob = target
            if not any(symbol in blob for symbol in changed_symbols):
                return tests
        for caller in record.get("callers") or []:
            if isinstance(caller, str):
                # Prefer the owning type/FQN before the method suffix.
                owner = caller.rsplit(".", 1)[0] if "." in caller else caller
                _consider(owner if owner.endswith(("Test", "Tests", "IT")) else caller)
        return tests

    # Structured Joern slices: collect candidate strings from known fields only.
    candidates: list[str] = []
    for key in ("fileName", "filename", "name", "fullName", "methodFullName", "typeFullName"):
        value = record.get(key)
        if isinstance(value, str):
            candidates.append(value)
    for key in ("nodes", "objectSlices", "definedBy", "usedAt"):
        value = record.get(key)
        if isinstance(value, list):
            for item in value[:200]:
                if isinstance(item, dict):
                    for nested in ("name", "fullName", "methodFullName", "fileName", "filename"):
                        nested_value = item.get(nested)
                        if isinstance(nested_value, str):
                            candidates.append(nested_value)
                elif isinstance(item, str):
                    candidates.append(item)
    blob = "\n".join(candidates)
    if changed_symbols and not any(symbol in blob for symbol in changed_symbols):
        return tests
    for candidate in candidates:
        stem = Path(candidate).stem if "/" in candidate or "\\" in candidate else candidate
        owner = stem.rsplit(".", 1)[0] if "." in stem and not stem.endswith(("Test", "Tests", "IT")) else stem
        _consider(owner)
        _consider(stem)
    return tests


def joern_slice_selection(
    root: Path,
    changed_files: list[str],
    joern_slice: str | None = None,
    joern_parse: str | None = None,
) -> dict[str, Any]:
    """Read or generate Joern CPG usage and data-flow slices (not a PDG)."""
    path = next((
        root / rel for rel in _JOERN_OUTPUTS
        if (root / rel).is_file() and _artifact_is_fresh(root, root / rel)
    ), None)
    generated: tempfile.TemporaryDirectory[str] | None = None
    command_result: dict[str, Any] | None = None
    cpg_candidates = [
        root / "cpg.bin", root / ".joern" / "cpg.bin",
        root / "target" / "cpg.bin", root / "build" / "cpg.bin",
        joern_cpg_path(root),
    ]
    cpg = next((
        candidate for candidate in cpg_candidates
        if candidate.is_file() and _artifact_is_fresh(root, candidate)
    ), None)
    cpg_build: dict[str, Any] | None = None
    if path is None and cpg is not None and joern_slice:
        cache = Path.home() / ".cache" / "cachelayer-toolchain"
        cache.mkdir(parents=True, exist_ok=True)
        generated = tempfile.TemporaryDirectory(prefix="tia-joern-", dir=cache)
        output_base = Path(generated.name) / "slices"
        file_filter = "|".join(
            re.escape(Path(name).name) for name in changed_files[:30]
        )
        usage = run_cmd(
            [
                joern_slice, "usages", "--parallelism", "1",
                "--file-filter", file_filter, "--out", str(output_base), str(cpg),
            ],
            cwd=root, timeout=60, memory_mb=1536,
        )
        data_flow = run_cmd(
            [
                joern_slice, "data-flow", "--parallelism", "1",
                "--slice-depth", "8", "--file-filter", file_filter,
                "--out", str(output_base) + "-dataflow", str(cpg),
            ],
            cwd=root, timeout=60, memory_mb=1536,
        )
        command_result = {
            "ok": bool(usage.get("ok")) or bool(data_flow.get("ok")),
            "output": "\n".join(
                str(item.get("output") or "") for item in (usage, data_flow)
            ),
        }
        generated_root = Path(generated.name)
        prefixes = (output_base.name, output_base.name + "-dataflow")
        output_files = sorted(
            candidate
            for candidate in generated_root.rglob("*.json")
            if any(
                part.startswith(prefix)
                for part in candidate.relative_to(generated_root).parts
                for prefix in prefixes
            )
        )
        if not command_result.get("ok") or not output_files:
            generated.cleanup()
            return {
                "available": False, "tests": [], "pdg": False,
                "reason": "Joern CPG slicing failed; using bounded static type/import slice",
                "detail": str(command_result.get("output") or "")[-500:],
                "cpg_build": cpg_build,
            }
        merged: list[Any] = []
        for output_file in output_files:
            try:
                value = json.loads(output_file.read_text(**_READ))
            except (OSError, ValueError):
                continue
            if isinstance(value, list):
                merged.extend(value)
            elif isinstance(value, dict):
                if "slices" in value and isinstance(value["slices"], list):
                    merged.extend(value["slices"])
                elif "results" in value and isinstance(value["results"], list):
                    merged.extend(value["results"])
                else:
                    merged.append(value)
        path = Path(generated.name) / "merged.json"
        path.write_text(json.dumps(merged), encoding="utf-8")
    if path is None:
        return {
            "available": False, "tests": [], "pdg": False,
            "reason": "Joern/CPG unavailable; using bounded static type/import slice",
            "cpg_build": cpg_build,
        }
    try:
        if path.stat().st_size > 5_000_000:
            return {
                "available": False, "tests": [], "pdg": False,
                "reason": "Joern slice exceeds 5 MB parse bound",
            }
        payload = json.loads(path.read_text(**_READ))
    except (OSError, ValueError) as exc:
        return {
            "available": False, "tests": [], "pdg": False,
            "reason": f"unreadable Joern slice: {exc}",
        }
    finally:
        if generated is not None:
            generated.cleanup()
    changed_symbols = {Path(rel).stem for rel in changed_files}
    tests: list[str] = []
    rejected_unknown = 0
    accepted_kinds: list[str] = []
    if isinstance(payload, dict) and _joern_record_kind(payload):
        records = [payload]
    elif isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        nested = payload.get("slices", payload.get("results"))
        records = nested if isinstance(nested, list) else []
        if not records:
            rejected_unknown += 1
    else:
        records = []
        rejected_unknown += 1
    for record in records[:2000]:
        kind = _joern_record_kind(record)
        if kind is None:
            rejected_unknown += 1
            continue
        if kind not in accepted_kinds:
            accepted_kinds.append(kind)
        for match in _joern_tests_from_record(record, changed_symbols):
            if match not in tests:
                tests.append(match)
            if len(tests) >= MAX_TESTS:
                break
        if len(tests) >= MAX_TESTS:
            break
    if not accepted_kinds:
        return {
            "available": False, "tests": [], "pdg": False,
            "reason": "Joern slice schema unrecognized; using bounded static type/import slice",
            "rejected_unknown_records": rejected_unknown,
            "cpg_build": cpg_build,
        }
    try:
        report_file = path.relative_to(root).as_posix()
    except ValueError:
        report_file = "temporary joern-slice output"
    return {
        "available": True,
        "tests": tests,
        "selection_incomplete": len(records) > 2000 or len(tests) >= MAX_TESTS,
        "file": report_file,
        "source": "joern-cpg-usage-dataflow-slice",
        "pdg": False,
        "slice_kinds": accepted_kinds,
        "rejected_unknown_records": rejected_unknown,
        "reason": (
            "generated Joern CPG usage and data-flow slices (not a PDG)"
            if command_result is not None
            else "existing Joern usage/data-flow or legacy callers slice (not a PDG)"
        ),
        "cpg": str(cpg) if cpg is not None else None,
        "cpg_build": cpg_build,
    }


# --- coverage.py dynamic contexts -------------------------------------------------

def coverage_context_tests(root: Path, changed_files: list[str]) -> dict[str, Any]:
    """Map changed files to the tests that executed them, using coverage.py contexts.

    Requires the project to record dynamic contexts (pytest --cov-context=test).
    Without contexts the database knows which lines ran but not which test ran them.
    """
    db = root / ".coverage"
    if not db.is_file():
        return {"tests": [], "available": False, "reason": "no .coverage database"}
    if not _artifact_is_fresh(root, db):
        return {"tests": [], "available": False, "reason": "stale or unprovable .coverage database"}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error as exc:
        return {"tests": [], "available": False, "reason": f"cannot open .coverage: {exc}"}
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"file", "context"} <= tables:
            return {"tests": [], "available": False, "reason": "unsupported .coverage schema"}

        wanted = {f.replace("\\", "/") for f in changed_files}
        file_ids: list[int] = []
        for file_id, path in conn.execute("SELECT id, path FROM file"):
            if not path:
                continue
            norm = str(path).replace("\\", "/")
            try:
                norm = Path(path).resolve().relative_to(root.resolve()).as_posix()
            except (OSError, ValueError):
                norm = norm.lstrip("./")
            if norm in wanted or any(norm.endswith("/" + w) for w in wanted):
                file_ids.append(int(file_id))
        if not file_ids:
            return {"tests": [], "available": True, "reason": "changed files absent from coverage data"}

        contexts: set[str] = set()
        for table in ("line_bits", "arc"):
            if table not in tables:
                continue
            for chunk_start in range(0, len(file_ids), MAX_SQL_PARAMS):
                chunk = file_ids[chunk_start:chunk_start + MAX_SQL_PARAMS]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT DISTINCT c.context FROM {table} t "
                    f"JOIN context c ON c.id = t.context_id "
                    f"WHERE t.file_id IN ({placeholders}) AND c.context != ''",
                    chunk,
                )
                contexts.update(row[0] for row in rows if row[0])
    except sqlite3.Error as exc:
        return {"tests": [], "available": False, "reason": f"coverage query failed: {exc}"}
    finally:
        conn.close()

    tests: list[str] = []
    for ctx in sorted(contexts):
        node = str(ctx).split("|", 1)[0].strip()
        if "::" in node and node not in tests:
            tests.append(node)
    if not tests:
        return {
            "tests": [],
            "available": True,
            "reason": "coverage database has no per-test contexts",
            "install": "Record contexts once: pytest --cov --cov-context=test",
        }
    return {
        "tests": tests[:MAX_TESTS],
        "available": True,
        "reason": "coverage contexts",
        "selection_incomplete": len(tests) > MAX_TESTS,
    }


# --- python import graph (reverse forward-slice) ----------------------------------

def _module_names(root: Path, rel: str) -> set[str]:
    parts = list(Path(rel).with_suffix("").parts)
    trimmed = [p for p in parts if p not in ("src", "lib")]
    names = set()
    for seq in (parts, trimmed):
        if not seq:
            continue
        names.add(".".join(seq))
        names.add(seq[-1])
        if seq[-1] == "__init__" and len(seq) > 1:
            names.add(".".join(seq[:-1]))
    return {n for n in names if n}


def python_importers(root: Path, changed_files: list[str], depth: int = 2) -> dict[str, Any]:
    """Expand changed modules to the modules that import them, bounded by depth."""
    py_changed = [f for f in changed_files if f.endswith((".py", ".pyi"))]
    if not py_changed:
        return {"files": [], "hops": 0}

    candidates = _walk(root, (".py",))
    texts: dict[str, str] = {}
    for path in candidates:
        rel = path.relative_to(root).as_posix()
        try:
            texts[rel] = path.read_text(**_READ)
        except OSError:
            continue

    frontier = {f for f in py_changed}
    seen = set(frontier)
    collected: list[str] = []
    for _ in range(max(1, depth)):
        targets: set[str] = set()
        for rel in frontier:
            targets |= _module_names(root, rel)
        if not targets:
            break
        # Cheap substring gate first, then a real import-statement match.
        stems = {t.rsplit(".", 1)[-1] for t in targets}
        patterns = [
            re.compile(rf"^\s*(?:from|import)\s+[\w.]*\b{re.escape(stem)}\b", re.MULTILINE)
            for stem in stems if stem
        ]
        found: set[str] = set()
        for rel, text in texts.items():
            if rel in seen:
                continue
            if not any(stem in text for stem in stems):
                continue
            if any(p.search(text) for p in patterns):
                found.add(rel)
        if not found:
            break
        collected.extend(sorted(found))
        seen |= found
        frontier = found

    return {
        "files": collected[:MAX_TESTS * 2],
        "hops": depth,
        "selection_incomplete": len(collected) > MAX_TESTS * 2,
    }


# --- jacoco ------------------------------------------------------------------------

def _jacoco_report(root: Path) -> Path | None:
    for rel in _JACOCO_REPORTS:
        path = root / rel
        if path.is_file():
            return path
    allowed_build = {"target", "build"}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if name not in (SKIP_PARTS - allowed_build)
            and name not in {"per-test", "sessions", "jacoco-per-test", "test-coverage"}
        ]
        for name in filenames:
            if name.startswith("jacoco") and name.endswith(".xml"):
                return Path(dirpath) / name
    return None


def jacoco_covered_classes(root: Path) -> dict[str, Any]:
    """Read a JaCoCo XML report: which classes the suite actually exercises."""
    report = _jacoco_report(root)
    if report is None:
        return {"parsed": False, "covered": set(), "reason": "no jacoco xml report"}
    try:
        tree = ET.parse(report)
    except (ET.ParseError, OSError) as exc:
        return {"parsed": False, "covered": set(), "reason": f"unreadable jacoco report: {exc}"}
    covered: set[str] = set()
    seen: set[str] = set()
    for cls in tree.iter("class"):
        name = (cls.get("name") or "").replace("/", ".")
        if not name:
            continue
        seen.add(name)
        for counter in cls.findall("counter"):
            if counter.get("type") in ("INSTRUCTION", "LINE") and int(counter.get("covered") or 0) > 0:
                covered.add(name)
                break
    return {
        "parsed": True,
        "covered": covered,
        "all": seen,
        "report": report.relative_to(root).as_posix(),
    }


def _fq_class(rel: str) -> str:
    parts = list(Path(rel).with_suffix("").parts)
    for marker in ("java", "kotlin"):
        if marker in parts:
            parts = parts[parts.index(marker) + 1:]
            break
    return ".".join(parts)


def _strip_java_noncode(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', text)


def _java_source_info(root: Path) -> dict[str, dict[str, Any]]:
    """Index bounded Java/Kotlin source files by relative path."""
    result: dict[str, dict[str, str]] = {}
    for path in _walk(root, (".java", ".kt", ".kts")):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(**_READ)
        except OSError:
            continue
        code = _strip_java_noncode(text)
        package_match = re.search(r"^\s*package\s+([\w.]+)\s*;?", code, re.MULTILINE)
        result[rel] = {
            "fq": _fq_class(rel),
            "simple": path.stem,
            "text": code,
            "package": package_match.group(1) if package_match else "",
            "imports": set(re.findall(r"^\s*import\s+(?:static\s+)?([\w.*$]+)\s*;?", code, re.MULTILINE)),
            "test": str("/src/test/" in f"/{rel}" or path.stem.endswith(("Test", "Tests", "IT"))),
        }
    return result


def java_dependents(root: Path, changed_files: list[str], depth: int = 2) -> dict[str, Any]:
    """Bounded static forward slice: changed classes -> dependents -> test classes.

    Java has reflection and dependency injection, so this is deliberately labelled
    a static over-approximation. Dynamic selectors take precedence when available.
    """
    index = _java_source_info(root)
    frontier = {
        rel for rel in changed_files
        if rel in index and Path(rel).suffix.lower() in (".java", ".kt", ".kts")
    }
    if not frontier:
        return {"tests": [], "dependents": [], "hops": 0}
    seen = set(frontier)
    dependents: list[str] = []
    tests: list[str] = []
    for _ in range(max(1, min(depth, 4))):
        targets = [
            (index[rel]["fq"], index[rel]["simple"], index[rel]["package"])
            for rel in frontier
        ]
        found: set[str] = set()
        for rel, item in index.items():
            if rel in seen:
                continue
            text = item["text"]
            matched = False
            for fq, simple, package in targets:
                if not simple or simple not in text:
                    continue
                imports = item["imports"]
                explicit_import = fq in imports or any(
                    value.endswith(".*") and fq.startswith(value[:-1]) for value in imports
                )
                same_package = bool(package and package == item["package"])
                type_use = re.search(
                    rf"\b(?:new\s+|extends\s+|implements\s+|instanceof\s+)?"
                    rf"{re.escape(simple)}(?:\s*<[^;{{}}]+>)?"
                    rf"\s*(?:[A-Za-z_$][\w$]*|\(|\.|::|\[)",
                    text,
                )
                if explicit_import or (same_package and type_use) or (
                    explicit_import and re.search(rf"\b{re.escape(simple)}\b", text)
                ):
                    matched = True
                    break
            if matched:
                found.add(rel)
        if not found:
            break
        for rel in sorted(found):
            if index[rel]["test"] == "True":
                fq = index[rel]["fq"]
                if fq and fq not in tests:
                    tests.append(fq)
            elif rel not in dependents:
                dependents.append(rel)
        seen |= found
        frontier = found
    return {
        "tests": tests[:40],
        "dependents": dependents[:100],
        "hops": min(depth, 4),
        "method": "bounded-java-type-import-forward-slice",
    }


def java_test_report_snapshot(root: Path) -> dict[str, int]:
    """Snapshot JUnit XML report mtimes before a dynamic selector runs."""
    result: dict[str, int] = {}
    for pattern in (
        "**/target/surefire-reports/TEST-*.xml",
        "**/target/failsafe-reports/TEST-*.xml",
        "**/build/test-results/**/TEST-*.xml",
    ):
        for path in root.glob(pattern):
            try:
                result[str(path)] = path.stat().st_mtime_ns
            except OSError:
                pass
    return result


def fresh_java_test_reports(root: Path, before: dict[str, int]) -> dict[str, Any]:
    """Count test classes from JUnit XML reports created or changed by one run."""
    suites: list[str] = []
    methods = failures = skipped = 0
    report_files: list[str] = []
    for pattern in (
        "**/target/surefire-reports/TEST-*.xml",
        "**/target/failsafe-reports/TEST-*.xml",
        "**/build/test-results/**/TEST-*.xml",
    ):
        for path in root.glob(pattern):
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if before.get(str(path)) == mtime:
                continue
            try:
                element = ET.parse(path).getroot()
            except (ET.ParseError, OSError):
                continue
            name = element.get("name") or path.stem.removeprefix("TEST-")
            if name not in suites:
                suites.append(name)
            methods += int(element.get("tests") or 0)
            failures += int(element.get("failures") or 0) + int(element.get("errors") or 0)
            skipped += int(element.get("skipped") or 0)
            report_files.append(path.relative_to(root).as_posix())
    return {
        "tests": suites[:MAX_TESTS],
        "selected": len(suites),
        "test_methods": methods,
        "failures": failures,
        "skipped_methods": skipped,
        "reports": report_files[:100],
    }


_DIFF_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")


def changed_line_numbers(diff_text: str, changed_files: list[str]) -> dict[str, set[int]]:
    """Parse added/modified line numbers from a zero-context unified diff."""
    wanted = {p.replace("\\", "/") for p in changed_files}
    result: dict[str, set[int]] = {p: set() for p in wanted}
    current: str | None = None
    line_no: int | None = None
    for raw in (diff_text or "").splitlines():
        if raw.startswith("+++ b/"):
            candidate = raw[6:].strip().replace("\\", "/")
            current = candidate if candidate in wanted else None
            line_no = None
            continue
        match = _DIFF_HUNK_RE.match(raw)
        if match:
            line_no = int(match.group(1))
            continue
        if current is None or line_no is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            result[current].add(line_no)
            line_no += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw.startswith(" "):
            line_no += 1
    return result


def jacoco_diff_coverage(
    root: Path, changed_files: list[str], diff_text: str
) -> dict[str, Any]:
    """Measure JaCoCo coverage of changed Java lines; this does not select tests."""
    report = _jacoco_report(root)
    if report is None:
        return {"available": False, "reason": "no JaCoCo XML report"}
    try:
        tree = ET.parse(report)
    except (ET.ParseError, OSError) as exc:
        return {"available": False, "reason": f"unreadable JaCoCo report: {exc}"}
    changed = changed_line_numbers(diff_text, changed_files)
    if not any(changed.values()):
        return {
            "available": True,
            "report": report.relative_to(root).as_posix(),
            "changed_lines": 0,
            "covered_lines": 0,
            "coverage_percent": None,
            "uncovered_lines": [],
        }
    coverage: dict[str, dict[int, bool]] = {}
    for package in tree.iter("package"):
        package_name = (package.get("name") or "").strip("/")
        for source in package.findall("sourcefile"):
            name = source.get("name") or ""
            rel = f"src/main/java/{package_name}/{name}".replace("//", "/")
            lines: dict[int, bool] = {}
            for line in source.findall("line"):
                nr = int(line.get("nr") or 0)
                covered = int(line.get("ci") or 0) > 0 or int(line.get("cb") or 0) > 0
                lines[nr] = covered
            coverage[rel] = lines
    total = 0
    covered_count = 0
    uncovered: list[str] = []
    unknown: list[str] = []
    for rel, numbers in changed.items():
        line_map = coverage.get(rel)
        if line_map is None:
            matches = [v for k, v in coverage.items() if rel.endswith(k) or k.endswith(rel)]
            line_map = matches[0] if len(matches) == 1 else None
        for number in sorted(numbers):
            if line_map is None or number not in line_map:
                unknown.append(f"{rel}:{number}")
                continue
            total += 1
            if line_map[number]:
                covered_count += 1
            else:
                uncovered.append(f"{rel}:{number}")
    return {
        "available": True,
        "report": report.relative_to(root).as_posix(),
        "changed_lines": total,
        "covered_lines": covered_count,
        "coverage_percent": round(100.0 * covered_count / total, 1) if total else None,
        "uncovered_lines": uncovered[:100],
        "unknown_lines": unknown[:100],
        "purpose": "coverage validation, not test selection",
    }


def java_tests_referencing(root: Path, changed_files: list[str], covered: set[str]) -> dict[str, Any]:
    """Select test classes that reference a changed class the suite already covers.

    Only call this with a parsed report: an empty ``covered`` set then means the
    suite exercises none of the changed classes, not that coverage is unknown.
    """
    changed_classes: dict[str, str] = {}
    for rel in changed_files:
        if Path(rel).suffix.lower() not in (".java", ".kt", ".kts"):
            continue
        fq = _fq_class(rel)
        if fq:
            changed_classes[fq] = Path(rel).stem
    if not changed_classes:
        return {"tests": [], "uncovered": [], "changed_classes": []}

    uncovered = [fq for fq in changed_classes if fq not in covered]
    eligible = {fq: simple for fq, simple in changed_classes.items() if fq in covered}
    test_roots = [root / "src" / "test" / "java", root / "src" / "test" / "kotlin"]
    tests: list[str] = []
    for base in test_roots:
        if not base.is_dir():
            continue
        for path in _walk(base, (".java", ".kt"), limit=2000):
            try:
                text = path.read_text(**_READ)
            except OSError:
                continue
            for fq, simple in eligible.items():
                if fq in text or re.search(rf"\b{re.escape(simple)}\b", text):
                    if path.stem not in tests:
                        tests.append(path.stem)
                    break
    return {
        "tests": tests[:40],
        "uncovered": uncovered,
        "changed_classes": sorted(changed_classes),
    }
