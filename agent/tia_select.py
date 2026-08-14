"""Test selection strategies beyond name matching. Stdlib only, bounded, honest about what it used."""
from __future__ import annotations

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
            for fq, simple in changed_classes.items():
                if fq in text or re.search(rf"\b{re.escape(simple)}\b", text):
                    if path.stem not in tests:
                        tests.append(path.stem)
                    break
    return {
        "tests": tests[:40],
        "uncovered": uncovered,
        "changed_classes": sorted(changed_classes),
    }
