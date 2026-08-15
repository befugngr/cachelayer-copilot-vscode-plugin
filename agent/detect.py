"""Detect repo languages and available local tools. Cached per process."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

try:
    from .util import SKIP_PARTS, which, workspace_root
except ImportError:
    from util import SKIP_PARTS, which, workspace_root

_CACHE: dict[str, Any] | None = None
_CACHE_ROOT: str | None = None
_MAX_ARTIFACT_VISITS = 800
_SKIP_ARTIFACT_DIRS = SKIP_PARTS | {
    ".idea", ".gradle", ".cache", ".m2", "vendor", "out",
}


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
    info = {
        "root": str(root),
        # Global pytest/mypy/ruff installs do not make a Java workspace Python.
        "python": py or py_sources,
        "javascript": js,
        "typescript": tsconfig or js,
        "java": java,
        "maven": pom,
        "gradle": gradle,
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
            "flacoco": which("flacoco"),
            "gzoltar": which("gzoltar"),
            "coverage": which("coverage"),
        },
        "flags": {
            "tsconfig": tsconfig,
            "eslint_config": eslint_cfg,
            "testmon": _has_testmon(root),
            "ekstazi": "ekstazi" in combined_java_build,
            "jacoco": "jacoco" in combined_java_build,
            "starts": "starts-maven-plugin" in combined_java_build,
            "smart_test_picker": "smart-test-picker" in combined_java_build,
            "affected_tests": (
                "io.github.vedanthvdev.affectedtests" in combined_java_build
                or "affectedtest" in combined_java_build
            ),
            "ekstazi_seeded": _has_named_dir(root, ".ekstazi"),
            "starts_seeded": _has_named_dir(root, ".starts"),
            "joern_cpg": _has_named_file(root, "cpg.bin"),
            "jacoco_per_test": _has_jacoco_per_test(root),
            "scalpel": _can_import("scalpel"),
            "coverage": _can_import("coverage"),
        },
    }
    _CACHE = info
    _CACHE_ROOT = key
    return info


def _can_import(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _has_testmon(root: Path) -> bool:
    return _can_import("testmon")


def _module_roots(root: Path) -> list[Path]:
    """Root plus immediate child modules that look like Maven/Gradle projects."""
    roots = [root]
    try:
        children = sorted(root.iterdir())
    except OSError:
        return roots
    for child in children:
        if not child.is_dir() or child.name in _SKIP_ARTIFACT_DIRS or child.name.startswith("."):
            continue
        if any((child / name).exists() for name in (
            "pom.xml", "build.gradle", "build.gradle.kts",
        )):
            roots.append(child)
        if len(roots) >= 40:
            break
    return roots


def _has_named_dir(root: Path, name: str) -> bool:
    for base in _module_roots(root):
        candidate = base / name
        if candidate.is_dir():
            return True
    visits = 0
    for dirpath, dirnames, _filenames in os.walk(root):
        visits += 1
        if visits > _MAX_ARTIFACT_VISITS:
            break
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_ARTIFACT_DIRS and not d.startswith(".")
        ]
        if (Path(dirpath) / name).is_dir():
            return True
    return False


def _has_named_file(root: Path, name: str) -> bool:
    for base in _module_roots(root):
        if (base / name).is_file():
            return True
    visits = 0
    for dirpath, dirnames, filenames in os.walk(root):
        visits += 1
        if visits > _MAX_ARTIFACT_VISITS:
            break
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_ARTIFACT_DIRS and not d.startswith(".")
        ]
        if name in filenames:
            return True
    return False


def _has_jacoco_per_test(root: Path) -> bool:
    """Check common Maven/Gradle report folders only — never a full-tree ** glob."""
    rel_dirs = (
        Path("target") / "jacoco" / "per-test",
        Path("target") / "jacoco" / "sessions",
        Path("target") / "jacoco-per-test",
        Path("build") / "jacoco" / "per-test",
        Path("build") / "jacoco" / "sessions",
        Path("build") / "jacoco-per-test",
        Path("build") / "reports" / "jacoco" / "per-test",
    )
    for base in _module_roots(root):
        for rel in rel_dirs:
            folder = base / rel
            if not folder.is_dir():
                continue
            try:
                for path in folder.iterdir():
                    if path.is_file() and path.suffix.lower() == ".xml" and path.name != "jacoco.xml":
                        return True
            except OSError:
                continue
        jacoco = base / "target" / "jacoco"
        if jacoco.is_dir():
            try:
                for path in jacoco.glob("session_*.xml"):
                    if path.is_file():
                        return True
            except OSError:
                pass
    return False


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
