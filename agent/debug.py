"""Debug-in-one-turn: FLITS + Ochiai + slice + ddmin/HDD + self-debug rubric."""
from __future__ import annotations

import ast
import json
import math
import re
import xml.etree.ElementTree as ET
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
    p = Path(file)
    if not p.is_absolute():
        p = root / file
    help_result = run_cmd([exe, "--help"], cwd=root, timeout=5)
    help_text = help_result.get("output") or ""
    if "--file" not in help_text or "--line" not in help_text:
        return None
    args = [exe, "--file", str(p), "--line", str(line)]
    if var and "--var" in help_text:
        args += ["--var", var]
    r = run_cmd(args, cwd=root, timeout=20)
    if r.get("available") is False:
        return None
    return {
        "method": "joern-slice",
        "file": file,
        "line": line,
        "text": cap_text(r.get("output") or "", 2500),
        "ok": r.get("ok"),
    }


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


def ochiai_from_coverage(root: Path, failure_text: str) -> dict[str, Any]:
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
    data_file = root / ".coverage"
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


def java_fault_localization(root: Path, info: dict[str, Any]) -> dict[str, Any]:
    """Use Flacoco/GZoltar only when an executable adapter is installed."""
    exe = info["tools"].get("flacoco") or info["tools"].get("gzoltar")
    name = "flacoco" if info["tools"].get("flacoco") else "gzoltar"
    if not exe:
        return {
            "available": False,
            "method": "flacoco/gzoltar",
            "install": "Install Flacoco or a GZoltar CLI and put it on PATH.",
        }
    help_result = run_cmd([exe, "--help"], cwd=root, timeout=5)
    help_text = help_result.get("output") or ""
    if "--projectpath" not in help_text:
        return {
            "available": True,
            "executed": False,
            "method": name,
            "reason": "installed CLI has no supported --projectpath adapter",
        }
    result = run_cmd([exe, "--projectpath", str(root)], cwd=root, timeout=30)
    hits = []
    for raw in (result.get("output") or "").splitlines():
        match = re.search(r"([\w.$/\\-]+\.java):(\d+).*?([01](?:\.\d+)?)", raw)
        if match:
            hits.append({"file": match.group(1), "line": int(match.group(2)), "score": float(match.group(3))})
    hits.sort(key=lambda item: item["score"], reverse=True)
    return {
        "available": True,
        "executed": True,
        "method": name,
        "ok": bool(result.get("ok")),
        "timeout": bool(result.get("timeout")),
        "ranked": hits[:15],
        "diagnostic": cap_text(result.get("output") or "", 500) if not hits else "",
    }


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
        "method": "ddmin-evidence",
        "oracle": "preserves crash frame/signature; does not re-run the failure",
        "reduction_pct": round(100 * (1 - len(reduced) / max(len(blob), 1)), 1),
    }


def debug_failure(
    stack_trace: str = "",
    test_output: str = "",
    file: str | None = None,
    line: int | None = None,
    coverage_matrix: list[dict[str, Any]] | None = None,
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

    minimized, minimizer = minimize_failure_blob(blob)

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
            slice_status = {"available": True, "method": "joern-slice", "degraded": False}
        else:
            sc = scalpel_slice(root, f, ln) if lang in ("py", "python") else None
            if sc:
                slice_info = sc
                slice_status = {"available": True, "method": "scalpel", "degraded": False}
            else:
                slice_info = python_enclosing_slice(root, f, ln)
                slice_status = {
                    "available": True,
                    "method": slice_info.get("method"),
                    "degraded": True,
                    "reason": (
                        "Scalpel unavailable/unsupported; used stdlib AST."
                        if lang in ("py", "python")
                        else "Joern unavailable/unsupported; used bounded source window."
                    ),
                }
        if lang == "java":
            java_sbfl = java_fault_localization(root, info)
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

    rubric = {
        "input": cap_text(minimized, 900),
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
        "minimizer": {**minimizer, "original_chars": len(blob), "result_chars": len(minimized or "")},
        "tools_used": {
            "flits": True,
            "ddmin_or_hdd": bool(minimizer.get("applied")),
            "joern": bool(slice_info and slice_info.get("method") == "joern-slice"),
            "scalpel_available": bool(info["flags"].get("scalpel")),
            "scalpel_used": bool(slice_info and slice_info.get("method") == "scalpel"),
            "ast_slice": bool(slice_info and slice_info.get("method") == "ast-enclosing"),
            "flacoco_or_gzoltar": bool(java_sbfl.get("executed")),
        },
        "next": "Use this blob for one fix at suggested_fix_location, then call verify_edit once. Do not call debug_failure a second time.",
    }
