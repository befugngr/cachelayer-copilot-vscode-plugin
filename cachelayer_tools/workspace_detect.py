"""Detect repo languages and available local tools. Cached per process."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

try:
    from .process_util import SKIP_PARTS, which, workspace_root
except ImportError:
    from process_util import SKIP_PARTS, which, workspace_root

_CACHE: dict[str, Any] | None = None
_CACHE_ROOT: str | None = None

# Soft skips used by other callers; artifact walk uses the hard set below.
_SKIP_ARTIFACT_DIRS = SKIP_PARTS | {
    ".idea", ".gradle", ".cache", ".m2", "vendor", "out",
}
_HARD_SKIP_ARTIFACT_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", ".tox",
    "__pycache__", ".m2", "vendor", ".idea", ".gradle", ".cache", "out",
}
# Bound discovery so large trees cannot claim a complete unbounded scan.
_MAX_ARTIFACT_DIRS = 800
_MAX_ARTIFACT_ENTRIES = 20_000
_TOOLCHAIN = Path.home() / ".cache" / "cachelayer-toolchain"


def invalidate_detect() -> None:
    """Clear process-local detection after preparation creates new artifacts."""
    global _CACHE, _CACHE_ROOT
    _CACHE = None
    _CACHE_ROOT = None


def detect(cwd: str | None = None) -> dict[str, Any]:
    global _CACHE, _CACHE_ROOT
    root = workspace_root(cwd)
    key = str(root)
    if _CACHE is not None and _CACHE_ROOT == key:
        return _CACHE

    py = any((root / name).exists() for name in (
        "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini", "tox.ini",
    ))
    pkg = root / "package.json"
    js = pkg.exists()
    pom = (root / "pom.xml").exists()
    gradle = (root / "build.gradle").exists() or (root / "build.gradle.kts").exists()
    java = pom or gradle

    tsconfig = (root / "tsconfig.json").exists()
    eslint_cfg = any((root / n).exists() for n in (
        "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
        ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml",
    ))

    pom_text = ""
    if pom:
        try:
            pom_text = (root / "pom.xml").read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pom_text = ""
    gradle_text = ""
    for g in ("build.gradle", "build.gradle.kts"):
        gp = root / g
        if gp.exists():
            try:
                gradle_text += gp.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass

    combined_java_build = (pom_text + "\n" + gradle_text).lower()
    py_sources = any(root.glob("*.py")) or any(root.glob("src/**/*.py"))
    artifacts = _artifact_inventory(root)
    analysis_python = _analysis_python()
    analysis_imports = _tool_imports(
        analysis_python, ("testmon", "scalpel", "coverage")
    )
    flacoco_jar = _flacoco_jar()
    info = {
        "root": str(root),
        # Global pytest/mypy/ruff installs do not make a Java workspace Python.
        "python": py or py_sources,
        "javascript": js,
        "typescript": tsconfig or js,
        "java": java,
        "maven": pom,
        "gradle": gradle,
        "artifacts": artifacts,
        "tools": {
            "python3": which("python3") or which("python"),
            "pytest": which("pytest"),
            "mypy": which("mypy"),
            "ruff": which("ruff"),
            "flake8": which("flake8"),
            "node": which("node"),
            "npm": which("npm"),
            "npx": which("npx"),
            "tsc": which("tsc"),
            "eslint": which("eslint"),
            "mvn": _wrapper_or_path(root, "mvn"),
            "gradle": _wrapper_or_path(root, "gradle"),
            "java": which("java"),
            "joern": which("joern"),
            "joern-slice": which("joern-slice"),
            "joern-parse": which("joern-parse"),
            "flacoco": which("flacoco"),
            "flacoco_jar": flacoco_jar,
            "gzoltar": which("gzoltar"),
            "coverage": which("coverage"),
            "analysis-python": analysis_python,
        },
        "flags": {
            "tsconfig": tsconfig,
            "eslint_config": eslint_cfg,
            # Prefer the dedicated analysis interpreter; do not treat a bare
            # system Python as proof that pytest-testmon/Scalpel are available.
            "testmon": analysis_imports["testmon"],
            "ekstazi": "ekstazi" in combined_java_build,
            "jacoco": "jacoco" in combined_java_build,
            "starts": "starts-maven-plugin" in combined_java_build,
            "smart_test_picker": "smart-test-picker" in combined_java_build,
            "affected_tests": (
                "io.github.vedanthvdev.affectedtests" in combined_java_build
                or "affectedtest" in combined_java_build
            ),
            "ekstazi_seeded": bool(artifacts["ekstazi"]),
            "starts_seeded": bool(artifacts["starts"]),
            "joern_cpg": bool(artifacts["cpg"]),
            "jacoco_per_test": bool(artifacts["jacoco_per_test"]),
            "artifact_scan_complete": bool(artifacts.get("scan_complete", True)),
            "scalpel": analysis_imports["scalpel"],
            "coverage": (
                analysis_imports["coverage"]
                or _can_import("coverage")
            ),
        },
    }
    _CACHE = info
    _CACHE_ROOT = key
    return info


def _analysis_python() -> str | None:
    """Return only an explicit analysis interpreter, never a silent system fallback."""
    for key in ("CACHELAYER_ANALYSIS_PYTHON", "CACHELAYER_TOOLCHAIN_PYTHON"):
        configured = os.environ.get(key, "").strip()
        if configured and Path(configured).exists():
            return configured
    wrapper = which("cachelayer-analysis-python")
    if wrapper:
        return wrapper
    venv_python = _TOOLCHAIN / "python-analysis" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return None


def _flacoco_jar() -> str | None:
    configured = os.environ.get("FLACOCO_JAR", "").strip()
    if configured and Path(configured).is_file():
        return configured
    candidate = (
        _TOOLCHAIN / "flacoco" / "flacoco-1.0.6-jar-with-dependencies.jar"
    )
    if candidate.is_file():
        return str(candidate)
    return None


def _can_import(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _tool_can_import(python: str | None, module: str) -> bool:
    return _tool_imports(python, (module,))[module]


def _tool_imports(python: str | None, modules: tuple[str, ...]) -> dict[str, bool]:
    """Probe optional analysis modules in one bounded interpreter startup."""
    found = {module: False for module in modules}
    if not python or not modules:
        return found
    try:
        script = (
            "import importlib.util,json;"
            f"mods={list(modules)!r};"
            "print(json.dumps({m:importlib.util.find_spec(m) is not None for m in mods}))"
        )
        result = subprocess.run(
            [python, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
            text=True,
        )
        value = json.loads(result.stdout) if result.returncode == 0 else {}
        if isinstance(value, dict):
            return {module: value.get(module) is True for module in modules}
    except (OSError, subprocess.SubprocessError):
        pass
    except (ValueError, json.JSONDecodeError):
        pass
    return found


def _artifact_inventory(root: Path) -> dict[str, Any]:
    """Find TIA artifacts with a pruned, budgeted scan.

    Dependency/VCS/cache trees are skipped. Build trees are visited, but
    class/resource fan-out is pruned after checking known report locations.
    Extra non-standard roots can be supplied through
    CACHELAYER_TIA_ARTIFACT_PATHS (os.pathsep-separated). When the directory
    or entry budget is hit, scan_complete is false.
    """
    found: dict[str, Any] = {
        "modules": [], "ekstazi": [], "starts": [], "cpg": [],
        "jacoco_per_test": [],
        "scan_complete": True,
        "dirs_visited": 0,
        "entries_seen": 0,
        "budget_dirs": _MAX_ARTIFACT_DIRS,
        "budget_entries": _MAX_ARTIFACT_ENTRIES,
    }
    scan_roots = [root]
    for raw in os.environ.get("CACHELAYER_TIA_ARTIFACT_PATHS", "").split(os.pathsep):
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        if path.is_dir() and path not in scan_roots:
            scan_roots.append(path)

    seen: set[tuple[int, int]] = set()
    dirs_visited = 0
    entries_seen = 0
    capped = False
    for scan_root in scan_roots:
        if capped:
            break
        for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=False):
            current = Path(dirpath)
            try:
                stat = current.stat()
            except OSError:
                dirnames[:] = []
                continue
            identity = (stat.st_dev, stat.st_ino)
            if identity in seen:
                dirnames[:] = []
                continue
            seen.add(identity)

            dirs_visited += 1
            entries_seen += len(dirnames) + len(filenames)
            if dirs_visited > _MAX_ARTIFACT_DIRS or entries_seen > _MAX_ARTIFACT_ENTRIES:
                capped = True
                dirnames[:] = []
                break

            if any(name in filenames for name in ("pom.xml", "build.gradle", "build.gradle.kts")):
                found["modules"].append(_display_path(current, root))
            if "cpg.bin" in filenames:
                found["cpg"].append(_display_path(current / "cpg.bin", root))
            if current.name == ".ekstazi":
                found["ekstazi"].append(_display_path(current, root))
                dirnames[:] = []
                continue
            if current.name == ".starts":
                found["starts"].append(_display_path(current, root))
                dirnames[:] = []
                continue
            if _is_per_test_jacoco_dir(current):
                for name in filenames:
                    if name.endswith(".xml") and name != "jacoco.xml":
                        found["jacoco_per_test"].append(
                            _display_path(current / name, root)
                        )
                        break

            dirnames[:] = [
                name for name in dirnames
                if name not in _HARD_SKIP_ARTIFACT_DIRS
            ]
            if current.name in {"classes", "test-classes", "generated", "tmp"}:
                dirnames[:] = []

    found["dirs_visited"] = dirs_visited
    found["entries_seen"] = entries_seen
    found["scan_complete"] = not capped
    for key in ("modules", "ekstazi", "starts", "cpg", "jacoco_per_test"):
        found[key] = list(dict.fromkeys(found[key]))
    return found


def _is_per_test_jacoco_dir(path: Path) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    return (
        path.name.lower() in {"per-test", "sessions", "jacoco-per-test", "test-coverage"}
        and any("jacoco" in part or part == "test-coverage" for part in parts)
    ) or path.name.lower() == "jacoco"  # session_*.xml is checked below


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def _wrapper_or_path(root: Path, name: str) -> str | None:
    wrapper_name = (
        ("mvnw.cmd" if name == "mvn" else "gradlew.bat")
        if os.name == "nt"
        else f"{name}w"
    )
    wrapper = root / wrapper_name
    if wrapper.is_file():
        return str(wrapper)
    return which(name)
