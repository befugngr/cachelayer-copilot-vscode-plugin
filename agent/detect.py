"""Detect repo languages and available local tools. Cached per process."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

try:
    from .util import which, workspace_root
except ImportError:
    from util import which, workspace_root

_CACHE: dict[str, Any] | None = None
_CACHE_ROOT: str | None = None


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
