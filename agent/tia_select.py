"""Test selection strategies beyond name matching. Stdlib only, bounded, honest about what it used."""
from __future__ import annotations

import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

try:
    from .util import SKIP_PARTS
except ImportError:
    from util import SKIP_PARTS

MAX_SCAN_FILES = 4000
MAX_TESTS = 50
MAX_SQL_PARAMS = 200
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


def _walk(root: Path, suffixes: tuple[str, ...], limit: int = MAX_SCAN_FILES) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*"):
        if len(found) >= limit:
            break
        if path.suffix.lower() not in suffixes or not path.is_file():
            continue
        if set(path.relative_to(root).parts) & SKIP_PARTS:
            continue
        found.append(path)
    return found


def smart_test_picker_map(root: Path) -> str | None:
    """Return a configured per-test JaCoCo map without searching build trees unboundedly."""
    for rel in _SMART_MAPS:
        path = root / rel
        if path.is_file():
            return rel
    for base_name in ("build", "target"):
        base = root / base_name
        if not base.is_dir():
            continue
        for path in base.glob("**/*coverage*map*.json"):
            if path.is_file():
                return path.relative_to(root).as_posix()
    return None


def smart_test_picker_selection(root: Path) -> dict[str, Any] | None:
    """Read the selector's explicit output after a Smart Test Picker run."""
    path = next((root / rel for rel in _SMART_SELECTIONS if (root / rel).is_file()), None)
    if path is None:
        candidates: list[Path] = []
        for build_dir in ("build", "target"):
            candidates.extend(root.glob(f"**/{build_dir}/selected-tests.json"))
        path = next((candidate for candidate in candidates[:200] if candidate.is_file()), None)
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
        "selected": len(tests),
        "total": total,
        "skipped_estimate": max(0, total - len(tests)) if total is not None else None,
        "file": path.relative_to(root).as_posix(),
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
    return {"tests": tests[:MAX_TESTS], "available": True, "reason": "coverage contexts"}


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

    return {"files": collected[:MAX_TESTS * 2], "hops": depth}


# --- jacoco ------------------------------------------------------------------------

def _jacoco_report(root: Path) -> Path | None:
    for rel in _JACOCO_REPORTS:
        path = root / rel
        if path.is_file():
            return path
    for path in root.rglob("jacoco*.xml"):
        if path.is_file() and not (set(path.relative_to(root).parts) & SKIP_PARTS - {"target", "build"}):
            return path
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


def _java_source_info(root: Path) -> dict[str, dict[str, str]]:
    """Index bounded Java/Kotlin source files by relative path."""
    result: dict[str, dict[str, str]] = {}
    for path in _walk(root, (".java", ".kt", ".kts")):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(**_READ)
        except OSError:
            continue
        result[rel] = {
            "fq": _fq_class(rel),
            "simple": path.stem,
            "text": text,
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
        targets = [(index[rel]["fq"], index[rel]["simple"]) for rel in frontier]
        found: set[str] = set()
        for rel, item in index.items():
            if rel in seen:
                continue
            text = item["text"]
            matched = False
            for fq, simple in targets:
                if not simple or simple not in text:
                    continue
                if (
                    (fq and re.search(rf"^\s*import\s+{re.escape(fq)}\s*;?", text, re.MULTILINE))
                    or re.search(rf"\b{re.escape(simple)}\b", text)
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
