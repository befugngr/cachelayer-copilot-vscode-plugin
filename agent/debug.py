"""Debug-in-one-turn: FLITS + Ochiai + slice + ddmin/HDD + self-debug rubric."""
from __future__ import annotations

import ast
import csv
import json
import math
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    from .detect import detect
    from .util import cap_text, run_cmd, which
except ImportError:
    from detect import detect
    from util import cap_text, run_cmd, which

# file:line from Python/JS/Java traces
_FRAME_RE = re.compile(
    r"""(?x)
    (?:File\s+\"(?P<pyfile>[^"]+)\",\s+line\s+(?P<pyline>\d+)
    |at\s+\S+\((?P<jfile>[\w./\\-]+\.java):(?P<jline>\d+)\)
    |(?:at\s+(?:\S+\s+)?\()?(?P<jsfile>(?:[A-Za-z]:)?[^()\s]+?\.(?:ts|tsx|js|jsx)):(?P<jsline>\d+)(?::\d+)?\)?)
    """
)
_PY_FILE_LINE = re.compile(r'File "([^"]+)", line (\d+)')


def parse_frames(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for m in _FRAME_RE.finditer(text or ""):
        if m.group("pyfile"):
            frames.append({"file": m.group("pyfile"), "line": int(m.group("pyline")), "lang": "py"})
        elif m.group("jfile"):
            frames.append({"file": m.group("jfile"), "line": int(m.group("jline")), "lang": "java"})
        elif m.group("jsfile"):
            frames.append({"file": m.group("jsfile"), "line": int(m.group("jsline")), "lang": "js"})
    # de-dupe
    seen = set()
    out = []
    for f in frames:
        k = (f["file"], f["line"])
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def flits_rank(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Weight stack frames: top (crash) highest, skip stdlib/site-packages."""
    n = len(frames)
    ranked = []
    for i, f in enumerate(frames):
        path = f["file"].replace("\\", "/")
        skip = any(p in path for p in (
            "site-packages", "lib/python", "node_modules", "/junit/", "/org/junit",
            "frozen importlib", "<frozen",
        ))
        # Python tracebacks end at the crash; JavaScript/Java stacks start there.
        pos_weight = ((i + 1) / max(n, 1)) if f.get("lang") == "py" else ((n - i) / max(n, 1))
        score = 0.0 if skip else 0.35 + 0.65 * pos_weight
        ranked.append({**f, "flits": round(score, 3), "stdlib": skip})
    ranked.sort(key=lambda x: x["flits"], reverse=True)
    return ranked


def _read_snippet(root: Path, file: str, line: int, pad: int = 8) -> str:
    p = Path(file)
    if not p.is_absolute():
        p = root / file
    try:
        p.resolve().relative_to(root.resolve())
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return ""
    i = max(0, line - 1)
    lo, hi = max(0, i - pad), min(len(lines), i + pad + 1)
    chunk = []
    for n in range(lo, hi):
        mark = ">>" if n == i else "  "
        chunk.append(f"{mark}{n+1:>5}| {lines[n]}")
    return "\n".join(chunk)


def python_enclosing_slice(root: Path, file: str, line: int) -> dict[str, Any]:
    """Fallback slice: enclosing function/class via ast. No extra deps."""
    p = Path(file)
    if not p.is_absolute():
        p = root / file
    try:
        p.resolve().relative_to(root.resolve())
        src = p.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (OSError, ValueError, SyntaxError):
        text = _read_snippet(root, file, line, pad=20)
        return {"method": "window", "file": file, "line": line, "text": text, "reduction_note": "parse failed; 40-line window"}

    orig_lines = src.count("\n") + 1
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if start and end and start <= line <= end:
            span = end - start
            if best is None or span < best[2]:
                best = (start, end, span, getattr(node, "name", "?"))
    if not best:
        text = _read_snippet(root, file, line, pad=15)
        return {"method": "window", "file": file, "line": line, "text": text}

    start, end, _, name = best
    lines = src.splitlines()
    text = "\n".join(f"{n:>5}| {lines[n-1]}" for n in range(start, min(end, start + 120) + 1) if 0 < n <= len(lines))
    sliced = end - start + 1
    return {
        "method": "ast-enclosing",
        "file": file,
        "line": line,
        "symbol": name,
        "text": cap_text(text, 2500),
        "slice_lines": sliced,
        "original_lines": orig_lines,
        "reduction_pct": round(100 * (1 - sliced / max(orig_lines, 1)), 1),
    }


def _ast_names(node: ast.AST, context: type[ast.expr_context]) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, context)
    }


def python_backward_slice(root: Path, file: str, line: int) -> dict[str, Any] | None:
    """Build a bounded intraprocedural def-use/control slice from a crash line."""
    path = Path(file)
    if not path.is_absolute():
        path = root / path
    try:
        path.resolve().relative_to(root.resolve())
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, ValueError, SyntaxError):
        return None

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    containing = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        and getattr(node, "lineno", 0) <= line <= getattr(node, "end_lineno", 0)
    ]
    if not containing:
        return None
    criterion = min(
        containing,
        key=lambda node: (
            getattr(node, "end_lineno", line) - getattr(node, "lineno", line),
            -getattr(node, "lineno", 0),
        ),
    )

    scope: ast.AST = criterion
    while scope in parents and not isinstance(
        scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    ):
        scope = parents[scope]
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        scope = tree

    statements = sorted(
        {
            node for node in ast.walk(scope)
            if isinstance(node, ast.stmt)
            and getattr(node, "lineno", 0) <= getattr(criterion, "lineno", line)
        },
        key=lambda node: getattr(node, "lineno", 0),
    )
    selected: set[ast.stmt] = {criterion}
    needed = _ast_names(criterion, ast.Load)
    for statement in reversed(statements):
        if statement is criterion:
            continue
        defined = _ast_names(statement, ast.Store)
        if defined & needed:
            selected.add(statement)
            needed = (needed - defined) | _ast_names(statement, ast.Load)

    control_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)
    for statement in list(selected):
        parent = parents.get(statement)
        while parent is not None and parent is not scope:
            if isinstance(parent, control_nodes):
                selected.add(parent)
                needed |= _ast_names(parent, ast.Load)
            parent = parents.get(parent)

    called = {
        call.func.id
        for statement in selected
        for call in ast.walk(statement)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in called:
            selected.add(node)

    lines = source.splitlines()
    rendered: list[str] = []
    selected_lines: set[int] = set()
    for statement in sorted(selected, key=lambda node: getattr(node, "lineno", 0)):
        start = getattr(statement, "lineno", 0)
        end = min(getattr(statement, "end_lineno", start), start + 30)
        for number in range(start, end + 1):
            if number in selected_lines or not (0 < number <= len(lines)):
                continue
            selected_lines.add(number)
            mark = ">>" if number == line else "  "
            rendered.append(f"{mark}{number:>5}| {lines[number - 1]}")
            if len(rendered) >= 120:
                break
        if len(rendered) >= 120:
            break
    if not rendered:
        return None
    return {
        "method": "ast-def-use-backward",
        "file": file,
        "line": line,
        "text": cap_text("\n".join(rendered), 3500),
        "slice_lines": len(selected_lines),
        "original_lines": len(lines),
        "reduction_pct": round(100 * (1 - len(selected_lines) / max(len(lines), 1)), 1),
        "criterion_uses": sorted(_ast_names(criterion, ast.Load))[:30],
        "unresolved_inputs": sorted(needed)[:30],
        "interprocedural_depth": 1 if called else 0,
    }


def scalpel_slice(root: Path, file: str, line: int) -> dict[str, Any] | None:
    try:
        from scalpel.core.mnode import MNode  # type: ignore
    except Exception:
        return None
    p = Path(file)
    if not p.is_absolute():
        p = root / file
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
        mnode = MNode("mod")
        mnode.source = src
        mnode.gen_ast()
        # scalpel APIs vary; enclosing fallback if slice API missing
        if hasattr(mnode, "gen_cfg"):
            mnode.gen_cfg()
    except Exception:
        return None
    # Scalpel's public slicing API differs by release. Building its CFG verifies
    # availability, but returning an invented slice would be unsafe.
    return None


def joern_slice(root: Path, file: str, line: int, var: str | None) -> dict[str, Any] | None:
    exe = which("joern-slice")
    if not exe:
        return None
    cpg_candidates = [
        Path(os.environ.get("JOERN_CPG", "")),
        root / "cpg.bin",
        root / ".joern" / "cpg.bin",
        root / "target" / "cpg.bin",
    ]
    cpg = next((path for path in cpg_candidates if str(path) and path.is_file()), None)
    if cpg is None:
        return {
            "available": False,
            "method": "joern-data-flow",
            "reason": "Joern is installed but no cpg.bin was found; run joern-parse once.",
        }

    with tempfile.TemporaryDirectory(prefix=".cachelayer-joern-", dir=root) as temp:
        output_prefix = Path(temp) / "slice"
        args = [
            exe, "data-flow",
            "--slice-depth", "8",
            "--file-filter", re.escape(Path(file).name),
            "--out", str(output_prefix),
        ]
        if var:
            args.extend(["--sink-filter", f".*{re.escape(var)}.*"])
        args.append(str(cpg))
        result = run_cmd(args, cwd=root, timeout=45)
        payload = ""
        for candidate in Path(temp).glob("slice*.json"):
            try:
                payload += candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        if not payload:
            return {
                "available": True,
                "method": "joern-data-flow",
                "ok": bool(result.get("ok")),
                "reason": cap_text(result.get("output") or "empty Joern slice", 500),
            }
        statements = _joern_statements(payload, file, line)
        return {
            "available": True,
            "method": "joern-data-flow",
            "file": file,
            "line": line,
            "text": cap_text("\n".join(statements), 3500),
            "ok": bool(result.get("ok")),
            "slice_lines": len(statements),
        }


def _joern_statements(payload: str, target_file: str, target_line: int) -> list[str]:
    try:
        value = json.loads(payload)
    except ValueError:
        return []
    found: list[tuple[str, int, str]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            raw_line = item.get("lineNumber") or item.get("line") or item.get("line_number")
            code = item.get("code") or item.get("label")
            file_name = item.get("fileName") or item.get("filename") or target_file
            try:
                line_number = int(raw_line)
            except (TypeError, ValueError):
                line_number = 0
            if line_number > 0 and isinstance(code, str) and code.strip():
                row = (str(file_name), line_number, code.strip())
                if row not in found:
                    found.append(row)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    found.sort(key=lambda row: (0 if Path(row[0]).name == Path(target_file).name else 1, abs(row[1] - target_line)))
    return [f"{name}:{number}| {code}" for name, number, code in found[:100]]


def ochiai(failed_cov: dict[tuple[str, int], int], passed_cov: dict[tuple[str, int], int]) -> list[dict[str, Any]]:
    keys = set(failed_cov) | set(passed_cov)
    ranked = []
    for k in keys:
        f = failed_cov.get(k, 0)
        p = passed_cov.get(k, 0)
        den = math.sqrt(f * (f + p)) or 1.0
        score = f / den
        ranked.append({"file": k[0], "line": k[1], "ochiai": round(score, 4)})
    ranked.sort(key=lambda x: x["ochiai"], reverse=True)
    return ranked[:15]


def _failed_test_ids(text: str) -> set[str]:
    ids = set(re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", text or "", re.M))
    ids.update(re.findall(r"^(\S+::\S+)\s+(?:FAILED|ERROR)\b", text or "", re.M))
    return {item.split("[", 1)[0] for item in ids}


def ochiai_from_coverage(
    root: Path,
    failure_text: str,
    data_file: Path | None = None,
) -> dict[str, Any]:
    """Compute Ochiai from coverage.py's per-test dynamic contexts."""
    failed_ids = _failed_test_ids(failure_text)
    if not failed_ids:
        return {"available": False, "method": "coverage-context-matrix", "reason": "no failing pytest node id in output"}
    try:
        from coverage import CoverageData  # type: ignore
    except Exception:
        return {
            "available": False,
            "method": "coverage-context-matrix",
            "install": "pip install coverage pytest-cov",
        }
    data_file = data_file or root / ".coverage"
    if not data_file.exists():
        return {
            "available": False,
            "method": "coverage-context-matrix",
            "reason": "no .coverage database",
            "next": "Run pytest --cov --cov-context=test once to seed the matrix.",
        }
    try:
        data = CoverageData(basename=str(data_file))
        data.read()
        contexts = {c for c in data.measured_contexts() if c}
        if not contexts:
            return {
                "available": False,
                "method": "coverage-context-matrix",
                "reason": "coverage database has no per-test contexts",
                "next": "Run pytest --cov --cov-context=test once.",
            }
        failed_cov: dict[tuple[str, int], int] = {}
        passed_cov: dict[tuple[str, int], int] = {}
        matched: set[str] = set()
        for measured in data.measured_files():
            try:
                rel = Path(measured).resolve().relative_to(root.resolve()).as_posix()
            except (OSError, ValueError):
                rel = measured
            for line_no, line_contexts in data.contexts_by_lineno(measured).items():
                normalized = {c.split("|", 1)[0].split("[", 1)[0] for c in line_contexts if c}
                failing = {
                    c for c in normalized
                    if any(fid == c or fid in c or c in fid for fid in failed_ids)
                }
                passing = normalized - failing
                matched.update(failing)
                if failing:
                    failed_cov[(rel, int(line_no))] = len(failing)
                if passing:
                    passed_cov[(rel, int(line_no))] = len(passing)
        if not matched:
            return {
                "available": False,
                "method": "coverage-context-matrix",
                "reason": "failing tests were absent from coverage contexts",
            }
        return {
            "available": True,
            "method": "coverage-context-matrix",
            "failed_tests": len(matched),
            "passed_tests": len(contexts - matched),
            "ranked": ochiai(failed_cov, passed_cov),
        }
    except Exception as exc:
        return {
            "available": False,
            "method": "coverage-context-matrix",
            "reason": cap_text(str(exc), 300),
        }


def generate_python_ochiai(
    root: Path,
    failure_text: str,
    python: str,
    timeout: int,
) -> dict[str, Any]:
    """Rerun only failing pytest files with dynamic contexts, then compute Ochiai."""
    failed_ids = _failed_test_ids(failure_text)
    test_files: list[str] = []
    for node_id in sorted(failed_ids):
        rel = node_id.split("::", 1)[0]
        path = (root / rel).resolve()
        try:
            path.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if path.is_file() and rel not in test_files:
            test_files.append(rel)
    if not test_files:
        return {
            "available": False,
            "method": "pytest-cov-context-matrix",
            "reason": "no local failing pytest file parsed from output",
        }

    data_file = root / ".cachelayer-debug-coverage"
    try:
        data_file.unlink(missing_ok=True)
        result = run_cmd(
            [
                python, "-m", "pytest", "-q", "--tb=line",
                "--cov=.", "--cov-context=test", "--cov-report=",
                *test_files[:5],
            ],
            cwd=root,
            timeout=max(1, min(timeout, 60)),
            env={"COVERAGE_FILE": str(data_file)},
        )
        ranked = ochiai_from_coverage(root, failure_text, data_file)
        ranked["runner"] = "pytest-cov"
        ranked["test_files"] = test_files[:5]
        ranked["test_exit_code"] = result.get("code")
        ranked["timed_out"] = bool(result.get("timeout"))
        if not ranked.get("available") and result.get("output"):
            ranked["diagnostic"] = cap_text(result["output"], 600)
        return ranked
    finally:
        data_file.unlink(missing_ok=True)


def _parse_fault_locations(text: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for row in csv.reader((text or "").splitlines()):
        joined = ",".join(row)
        match = re.search(
            r"([A-Za-z_][\w.$/\\-]*(?:\.java)?)[#:,;](\d+).*?([01](?:\.\d+)?)",
            joined,
        )
        if not match:
            continue
        item = {
            "file": match.group(1),
            "line": int(match.group(2)),
            "score": float(match.group(3)),
        }
        if item not in hits:
            hits.append(item)
    hits.sort(key=lambda item: item["score"], reverse=True)
    return hits[:15]


def java_fault_localization(
    root: Path,
    info: dict[str, Any],
    timeout: int = 45,
) -> dict[str, Any]:
    """Run a configured Flacoco CLI or jar and parse suspicious locations."""
    exe = info["tools"].get("flacoco")
    jar = os.environ.get("FLACOCO_JAR", "")
    java = info["tools"].get("java")
    if exe:
        prefix = [exe]
    elif jar and Path(jar).is_file() and java:
        prefix = [java, "-jar", jar]
    else:
        return {
            "available": False,
            "method": "flacoco",
            "install": "Put flacoco on PATH or set FLACOCO_JAR to its standalone jar.",
        }
    help_result = run_cmd([*prefix, "--help"], cwd=root, timeout=8)
    help_text = help_result.get("output") or ""
    if "--projectpath" not in help_text:
        return {
            "available": True,
            "executed": False,
            "method": "flacoco",
            "reason": "installed CLI has no supported --projectpath adapter",
        }
    output_file = root / ".cachelayer-flacoco.csv"
    try:
        output_file.unlink(missing_ok=True)
        result = run_cmd(
            [
                *prefix, "--projectpath", str(root),
                "--format", "CSV", "--output", str(output_file),
            ],
            cwd=root,
            timeout=max(1, min(timeout, 120)),
        )
        report = ""
        try:
            report = output_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        hits = _parse_fault_locations(report + "\n" + (result.get("output") or ""))
        return {
            "available": True,
            "executed": True,
            "method": "flacoco",
            "ok": bool(result.get("ok")),
            "timeout": bool(result.get("timeout")),
            "ranked": hits,
            "diagnostic": cap_text(result.get("output") or "", 500) if not hits else "",
        }
    finally:
        output_file.unlink(missing_ok=True)


def ddmin(s: str, pred, max_rounds: int = 12) -> str:
    """Minimize string while pred(s) stays True (still interesting / still failing)."""
    if not s or len(s) < 32:
        return s
    data = s
    n = 2
    rounds = 0
    while len(data) >= 32 and rounds < max_rounds:
        rounds += 1
        chunk = max(1, len(data) // n)
        progressed = False
        i = 0
        parts = [data[j:j + chunk] for j in range(0, len(data), chunk)]
        # try dropping one part at a time
        for idx in range(len(parts)):
            trial = "".join(parts[k] for k in range(len(parts)) if k != idx)
            if trial and pred(trial):
                data = trial
                n = max(2, n - 1)
                progressed = True
                break
        if not progressed:
            if n >= len(data):
                break
            n = min(len(data), n * 2)
    return data


def hdd_json(s: str) -> str:
    try:
        obj = json.loads(s)
    except Exception:
        return s
    if isinstance(obj, dict) and obj:
        # keep first 3 keys
        slim = {k: obj[k] for k in list(obj)[:3]}
        return json.dumps(slim, indent=2)[:2000]
    if isinstance(obj, list) and len(obj) > 3:
        return json.dumps(obj[:3], indent=2)[:2000]
    return s[:2000]


def hdd_xml(s: str) -> str:
    try:
        root = ET.fromstring(s)
    except ET.ParseError:
        return s
    for node in list(root.iter()):
        children = list(node)
        for child in children[3:]:
            node.remove(child)
    return ET.tostring(root, encoding="unicode")[:2000]


def minimize_failure_blob(blob: str) -> tuple[str, dict[str, Any]]:
    if len(blob) <= 800:
        return blob, {"applied": False, "reason": "input below threshold"}
    stripped = blob.lstrip()
    if stripped.startswith(("{", "[")):
        reduced = hdd_json(blob)
        return reduced, {
            "applied": True,
            "method": "json-hdd-structural",
            "oracle": "structure only; no repro command supplied",
            "reduction_pct": round(100 * (1 - len(reduced) / max(len(blob), 1)), 1),
        }
    if stripped.startswith("<"):
        reduced = hdd_xml(blob)
        return reduced, {
            "applied": True,
            "method": "xml-hdd-structural",
            "oracle": "structure only; no repro command supplied",
            "reduction_pct": round(100 * (1 - len(reduced) / max(len(blob), 1)), 1),
        }
    frames = parse_frames(blob)
    signatures = [
        line.strip() for line in blob.splitlines()
        if re.search(r"(?:AssertionError|[A-Za-z]+(?:Error|Exception))(?::|$)", line)
    ]
    signature = signatures[-1] if signatures else ""

    def preserves_evidence(text: str) -> bool:
        has_frame = bool(parse_frames(text)) if frames else True
        has_signature = signature in text if signature else bool(re.search(r"Error|Exception|FAILED", text))
        return has_frame and has_signature

    reduced = ddmin(blob, preserves_evidence, max_rounds=18)
    return reduced, {
        "applied": True,
        "method": "evidence-compression",
        "oracle": "preserves crash frame/signature; does not re-run the failure",
        "oracle_verified": False,
        "reduction_pct": round(100 * (1 - len(reduced) / max(len(blob), 1)), 1),
    }


def _json_delete_paths(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    paths: list[tuple[Any, ...]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            paths.append(path + (key,))
            paths.extend(_json_delete_paths(child, path + (key,)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.append(path + (index,))
            paths.extend(_json_delete_paths(child, path + (index,)))
    return sorted(paths, key=len, reverse=True)


def _delete_json_path(value: Any, path: tuple[Any, ...]) -> Any:
    candidate = deepcopy(value)
    parent = candidate
    for part in path[:-1]:
        parent = parent[part]
    last = path[-1]
    if isinstance(parent, dict):
        parent.pop(last, None)
    elif isinstance(parent, list) and isinstance(last, int) and 0 <= last < len(parent):
        parent.pop(last)
    return candidate


def _hdd_json_repro(text: str, interesting, max_runs: int) -> str:
    try:
        current = json.loads(text)
    except ValueError:
        return text
    changed = True
    while changed and interesting.runs < max_runs:
        changed = False
        for path in _json_delete_paths(current):
            candidate = _delete_json_path(current, path)
            encoded = json.dumps(candidate, separators=(",", ":"))
            if interesting(encoded):
                current = candidate
                changed = True
                break
            if interesting.runs >= max_runs:
                break
    return json.dumps(current, indent=2)


def _hdd_xml_repro(text: str, interesting, max_runs: int) -> str:
    try:
        current = ET.fromstring(text)
    except ET.ParseError:
        return text
    changed = True
    while changed and interesting.runs < max_runs:
        changed = False
        parents = list(current.iter())
        for parent_index in range(len(parents) - 1, -1, -1):
            parent = parents[parent_index]
            for child_index in range(len(list(parent)) - 1, -1, -1):
                candidate = ET.fromstring(ET.tostring(current, encoding="unicode"))
                candidate_parents = list(candidate.iter())
                if parent_index >= len(candidate_parents):
                    continue
                candidate_parent = candidate_parents[parent_index]
                children = list(candidate_parent)
                if child_index >= len(children):
                    continue
                candidate_parent.remove(children[child_index])
                encoded = ET.tostring(candidate, encoding="unicode")
                if interesting(encoded):
                    current = candidate
                    changed = True
                    break
                if interesting.runs >= max_runs:
                    break
            if changed or interesting.runs >= max_runs:
                break
    return ET.tostring(current, encoding="unicode")


def minimize_with_repro(
    root: Path,
    failing_input: str,
    repro: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Minimize input while a bounded argv-based reproduction still fails."""
    argv = repro.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        return failing_input, {"applied": False, "reason": "repro.argv must be a string array"}
    max_runs = max(1, min(int(repro.get("max_runs") or 30), 50))
    timeout = max(1, min(int(repro.get("timeout") or 5), 15))
    pattern = str(repro.get("failure_pattern") or "")

    with tempfile.TemporaryDirectory(prefix=".cachelayer-repro-", dir=root) as temp:
        input_path = Path(temp) / "input.txt"

        class Interesting:
            runs = 0
            last_output = ""

            def __call__(self, candidate: str) -> bool:
                if self.runs >= max_runs:
                    return False
                self.runs += 1
                input_path.write_text(candidate, encoding="utf-8")
                command = [arg.replace("{input}", str(input_path)) for arg in argv]
                uses_file = any("{input}" in arg for arg in argv)
                result = run_cmd(
                    command,
                    cwd=root,
                    timeout=timeout,
                    input_text=None if uses_file else candidate,
                )
                self.last_output = result.get("output") or ""
                failed = result.get("code") not in (0, None)
                if pattern:
                    try:
                        return failed and re.search(pattern, self.last_output) is not None
                    except re.error:
                        return False
                return failed

        interesting = Interesting()
        if not interesting(failing_input):
            return failing_input, {
                "applied": False,
                "reason": "initial input did not reproduce the requested failure",
                "runs": interesting.runs,
                "diagnostic": cap_text(interesting.last_output, 500),
            }
        stripped = failing_input.lstrip()
        if stripped.startswith(("{", "[")):
            reduced = _hdd_json_repro(failing_input, interesting, max_runs)
            method = "json-hdd-repro"
        elif stripped.startswith("<"):
            reduced = _hdd_xml_repro(failing_input, interesting, max_runs)
            method = "xml-hdd-repro"
        else:
            reduced = ddmin(failing_input, interesting, max_rounds=max_runs)
            method = "ddmin-repro"
        return reduced, {
            "applied": len(reduced) < len(failing_input),
            "method": method,
            "oracle": "non-zero exit" + (f" matching /{pattern}/" if pattern else ""),
            "oracle_verified": True,
            "runs": interesting.runs,
            "max_runs": max_runs,
            "reduction_pct": round(
                100 * (1 - len(reduced) / max(len(failing_input), 1)),
                1,
            ),
        }


def debug_failure(
    stack_trace: str = "",
    test_output: str = "",
    file: str | None = None,
    line: int | None = None,
    coverage_matrix: list[dict[str, Any]] | None = None,
    auto_coverage: bool = True,
    timeout: int = 45,
    failing_input: str = "",
    repro: dict[str, Any] | None = None,
    *,
    cwd: str | None = None,
) -> dict[str, Any]:
    info = detect(cwd)
    root = Path(info["root"])
    blob = "\n".join(x for x in (stack_trace, test_output) if x).strip()
    if not blob and not file:
        return {
            "ok": False,
            "error": "pass stack_trace or test_output (or file+line)",
            "next": "Paste the failing test / traceback. Do not grep first.",
        }

    minimized, evidence_minimizer = minimize_failure_blob(blob)
    minimized_input = ""
    minimizer = evidence_minimizer
    if failing_input and repro:
        minimized_input, minimizer = minimize_with_repro(root, failing_input, repro)

    frames = flits_rank(parse_frames(minimized or blob))
    crash = None
    if file and line:
        crash = {"file": file, "line": int(line), "flits": 1.0}
    elif frames:
        crash = next((f for f in frames if not f.get("stdlib")), frames[0])

    slice_info = None
    slice_status: dict[str, Any] = {"available": False, "method": "none"}
    ochiai_result: dict[str, Any] = {
        "available": False,
        "method": "coverage-matrix",
        "reason": "no failing/passing coverage matrix available; FLITS is separate",
        "ranked": [],
    }
    java_sbfl: dict[str, Any] = {
        "available": False,
        "method": "flacoco/gzoltar",
        "reason": "not a Java failure",
    }
    if crash:
        f, ln = crash["file"], int(crash["line"])
        lang = crash.get("lang") or Path(f).suffix.lstrip(".")
        js = joern_slice(root, f, ln, None) if lang in ("java", "js", "ts", "tsx", "jsx") else None
        if js and js.get("text"):
            slice_info = js
            slice_status = {"available": True, "method": "joern-data-flow", "degraded": False}
        else:
            sc = python_backward_slice(root, f, ln) if lang in ("py", "python") else None
            if sc:
                slice_info = sc
                slice_status = {
                    "available": True,
                    "method": "ast-def-use-backward",
                    "degraded": False,
                }
            else:
                slice_info = python_enclosing_slice(root, f, ln)
                slice_status = {
                    "available": True,
                    "method": slice_info.get("method"),
                    "degraded": True,
                    "reason": (
                        "Def-use slice unavailable; used enclosing AST."
                        if lang in ("py", "python")
                        else (js or {}).get("reason")
                        or "Joern unavailable; used bounded source window."
                    ),
                }
        if lang == "java":
            java_sbfl = java_fault_localization(root, info, timeout=timeout)
    if coverage_matrix:
        failed_cov: dict[tuple[str, int], int] = {}
        passed_cov: dict[tuple[str, int], int] = {}
        for row in coverage_matrix[:10_000]:
            try:
                key = (str(row["file"]), int(row["line"]))
                failed_cov[key] = max(0, int(row.get("failed_covered", 0)))
                passed_cov[key] = max(0, int(row.get("passed_covered", 0)))
            except (KeyError, TypeError, ValueError):
                continue
        if failed_cov:
            ochiai_result = {"available": True, "method": "coverage-matrix", "ranked": ochiai(failed_cov, passed_cov)}
    elif info["flags"].get("coverage"):
        ochiai_result = ochiai_from_coverage(root, blob)
        if (
            not ochiai_result.get("available")
            and auto_coverage
            and info.get("python")
            and info["tools"].get("python3")
        ):
            ochiai_result = generate_python_ochiai(
                root,
                blob,
                info["tools"]["python3"],
                timeout,
            )

    rubric = {
        "input": cap_text(minimized_input or minimized, 900),
        "expected": "tests/typecheck pass",
        "actual": cap_text(
            next((ln for ln in reversed((minimized or "").splitlines()) if ln.strip()), ""),
            400,
        ),
        "hypothesis": (
            f"The highest-ranked application frame at {crash['file']}:{crash['line']} caused the observed failure."
            if crash else "No application frame was parsed; inspect the final assertion/error."
        ),
        "evidence": {
            "flits_top": f"{crash['file']}:{crash['line']}" if crash else "unknown",
            "slice_method": (slice_info or {}).get("method"),
            "ochiai_real": bool(ochiai_result.get("available")),
        },
        "suggested_fix_location": f"{crash['file']}:{crash['line']}" if crash else "unknown",
    }

    return {
        "ok": True,
        "crash": crash,
        "ranked_frames": frames[:12],
        "slice": slice_info,
        "slice_status": slice_status,
        "ochiai": ochiai_result,
        "java_sbfl": java_sbfl,
        "rubric": rubric,
        "minimizer": {
            **minimizer,
            "original_chars": len(failing_input) if failing_input and repro else len(blob),
            "result_chars": len(minimized_input) if failing_input and repro else len(minimized or ""),
        },
        "minimized_input": cap_text(minimized_input, 3500),
        "evidence_minimizer": evidence_minimizer,
        "tools_used": {
            "flits": True,
            "ddmin_or_hdd": bool(
                minimizer.get("applied") and minimizer.get("oracle_verified")
            ),
            "joern": bool(slice_info and slice_info.get("method") == "joern-data-flow"),
            "scalpel_available": bool(info["flags"].get("scalpel")),
            "scalpel_used": False,
            "python_def_use_slice": bool(
                slice_info and slice_info.get("method") == "ast-def-use-backward"
            ),
            "ast_slice": bool(slice_info and slice_info.get("method") == "ast-enclosing"),
            "flacoco_or_gzoltar": bool(java_sbfl.get("executed")),
        },
        "next": "Use this blob for one fix at suggested_fix_location, then call verify_edit once. Do not call debug_failure a second time.",
    }
