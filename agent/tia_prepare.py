"""Explicit, bounded preparation for real test-impact artifacts.

Nothing here edits a build file. Baselines can execute every test and therefore
require an explicit confirmation from the caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    from .detect import detect
    from .util import run_cmd, which
except ImportError:
    from detect import detect
    from util import run_cmd, which

JACOCO_VERSION = "0.8.13"
STARTS_VERSION = "1.4"
EKSTAZI_VERSION = "5.3.0"
TOOLCHAIN = Path.home() / ".cache" / "cachelayer-toolchain"


def _bounded_java_env(heap_mb: int = 512) -> dict[str, str]:
    existing = os.environ.get("JAVA_TOOL_OPTIONS", "").strip()
    limits = (
        f"-Xmx{heap_mb}m -XX:CompressedClassSpaceSize=96m "
        "-XX:MaxMetaspaceSize=256m -XX:ReservedCodeCacheSize=128m "
        "-Djdk.attach.allowAttachSelf=true -Djava.security.manager=allow"
    )
    return {"JAVA_TOOL_OPTIONS": f"{existing} {limits}".strip()}


def _java_tests(root: Path) -> list[str]:
    result: list[str] = []
    for marker in ("java", "kotlin"):
        for base in root.glob(f"**/src/test/{marker}"):
            if not base.is_dir():
                continue
            for pattern in ("*Test.java", "*Tests.java", "*IT.java", "*Test.kt", "*Tests.kt", "*IT.kt"):
                for path in base.rglob(pattern):
                    parts = list(path.with_suffix("").parts)
                    value = ".".join(parts[parts.index(marker) + 1:]) if marker in parts else path.stem
                    if value not in result:
                        result.append(value)
    return result


def _cache_key(root: Path) -> str:
    revision = run_cmd(
        ["git", "rev-parse", "HEAD"], cwd=root, timeout=5, memory_mb=None,
    ).get("output") or "working-tree"
    raw = f"{root.resolve()}\0{str(revision).strip()}".encode()
    return hashlib.sha256(raw).hexdigest()[:20]


def jacoco_provenance(root: Path) -> dict[str, str]:
    """Fingerprint inputs which can change per-test coverage ownership."""
    digest = hashlib.sha256()
    files: list[Path] = []
    for name in (
        "pom.xml", "build.gradle", "build.gradle.kts",
        "settings.gradle", "settings.gradle.kts", "gradle.properties",
    ):
        files.extend(path for path in root.glob(f"**/{name}") if path.is_file())
    for marker in ("src/main", "src/test"):
        for base in root.glob(f"**/{marker}"):
            if base.is_dir():
                files.extend(
                    path for path in base.rglob("*")
                    if path.is_file() and path.suffix.lower() in {".java", ".kt", ".kts"}
                )
    for path in sorted(set(files)):
        try:
            rel = path.relative_to(root).as_posix()
            digest.update(rel.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        except OSError:
            continue
    revision = run_cmd(
        ["git", "rev-parse", "HEAD"], cwd=root, timeout=5, memory_mb=None,
    ).get("output") or "working-tree"
    return {
        "root": str(root.resolve()),
        "revision": str(revision).strip(),
        "input_sha256": digest.hexdigest(),
    }


def joern_cpg_path(root: Path) -> Path:
    return TOOLCHAIN / "joern-cpg" / _cache_key(root) / "cpg.bin"


def build_joern_cpg(root: Path, timeout: int = 300) -> dict[str, Any]:
    parser = which("joern-parse")
    if not parser:
        return {"ok": False, "available": False, "reason": "joern-parse is not installed"}
    output = joern_cpg_path(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = run_cmd(
        [parser, str(root), "--language", "javasrc", "--output", str(output)],
        cwd=root, timeout=max(30, min(timeout, 900)), memory_mb=None,
        env=_bounded_java_env(1200),
    )
    return {
        "ok": bool(result.get("ok")) and output.is_file(),
        "available": True,
        "artifact": str(output) if output.is_file() else None,
        "summary": result.get("output", "")[-1000:],
        "timed_out": bool(result.get("timeout")),
    }


def _safe_test_name(test: str) -> str:
    return test.replace("$", "_").replace(".", "__").replace("#", "__")


def _jacoco_cli(root: Path, mvn: str | None, timeout: int) -> Path | None:
    candidates = [
        TOOLCHAIN / f"org.jacoco.cli-{JACOCO_VERSION}-nodeps.jar",
        Path.home() / ".m2" / "repository" / "org" / "jacoco" / "org.jacoco.cli"
        / JACOCO_VERSION / f"org.jacoco.cli-{JACOCO_VERSION}-nodeps.jar",
    ]
    found = next((path for path in candidates if path.is_file()), None)
    if found or not mvn:
        return found
    fetched = run_cmd(
        [
            mvn, "-q", "dependency:get",
            f"-Dartifact=org.jacoco:org.jacoco.cli:{JACOCO_VERSION}:jar:nodeps",
        ],
        cwd=root, timeout=timeout, memory_mb=None, env=_bounded_java_env(),
    )
    return candidates[1] if fetched.get("ok") and candidates[1].is_file() else None


def _jacoco_report_from_exec(
    root: Path,
    exec_file: Path,
    xml_file: Path,
    cli: Path,
    timeout: int,
) -> dict[str, Any]:
    class_dirs = [
        path for pattern in (
            "**/target/classes", "**/build/classes/java/main",
            "**/build/classes/kotlin/main",
        )
        for path in root.glob(pattern) if path.is_dir()
    ]
    source_dirs = [
        path for pattern in ("**/src/main/java", "**/src/main/kotlin")
        for path in root.glob(pattern) if path.is_dir()
    ]
    if not exec_file.is_file() or exec_file.stat().st_size == 0 or not class_dirs:
        return {"ok": False, "output": "missing unique exec data or compiled classes"}
    xml_file.parent.mkdir(parents=True, exist_ok=True)
    xml_file.unlink(missing_ok=True)
    java = which("java")
    if not java:
        return {"ok": False, "output": "java is unavailable"}
    argv = [java, "-jar", str(cli), "report", str(exec_file)]
    for path in class_dirs:
        argv.extend(["--classfiles", str(path)])
    for path in source_dirs:
        argv.extend(["--sourcefiles", str(path)])
    argv.extend(["--xml", str(xml_file)])
    result = run_cmd(
        argv, cwd=root, timeout=timeout, memory_mb=None, env=_bounded_java_env(),
    )
    result["ok"] = bool(result.get("ok")) and xml_file.is_file()
    return result


def _write_jacoco_manifest(
    root: Path, destination: Path, entries: dict[str, dict[str, str]],
) -> Path:
    manifest = destination / "manifest.json"
    payload = {
        "schema": "cachelayer-jacoco-per-test-v1",
        "jacoco_version": JACOCO_VERSION,
        "provenance": jacoco_provenance(root),
        "tests": entries,
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def seed_jacoco_per_test(
    root: Path, info: dict[str, Any], *, max_tests: int, timeout: int,
) -> dict[str, Any]:
    tests = _java_tests(root)
    if not tests:
        return {"ok": False, "reason": "no Java test classes discovered", "tests": 0}
    if len(tests) > max_tests:
        return {
            "ok": False,
            "reason": f"baseline has {len(tests)} tests, above max_tests={max_tests}; no tests ran",
            "tests": len(tests),
        }
    failures: list[str] = []
    diagnostics: dict[str, str] = {}
    reports = 0
    entries: dict[str, dict[str, str]] = {}
    mvn = info.get("tools", {}).get("mvn")
    cli = _jacoco_cli(root, mvn, timeout)
    if not cli:
        return {
            "ok": False,
            "reason": "JaCoCo CLI nodeps jar is unavailable; cannot derive XML from unique exec files",
        }
    if info.get("maven"):
        if not mvn:
            return {"ok": False, "reason": "Maven is unavailable"}
        destination = root / "target" / "jacoco" / "per-test"
        exec_dir = root / "target" / "jacoco" / "per-test-exec"
        for test in tests:
            safe = _safe_test_name(test)
            exec_file = exec_dir / f"{safe}.exec"
            xml_file = destination / f"session_{safe}.xml"
            exec_file.parent.mkdir(parents=True, exist_ok=True)
            exec_file.unlink(missing_ok=True)
            result = run_cmd(
                [
                    mvn, "-q", f"-Dtest={test}", "-DfailIfNoTests=false",
                    f"-Djacoco.destFile={exec_file}",
                    "-Djacoco.append=false",
                    f"org.jacoco:jacoco-maven-plugin:{JACOCO_VERSION}:prepare-agent",
                    "test",
                ],
                cwd=root, timeout=timeout, memory_mb=None,
                env=_bounded_java_env(),
            )
            report = (
                _jacoco_report_from_exec(root, exec_file, xml_file, cli, timeout)
                if result.get("ok") else result
            )
            if not report.get("ok"):
                failures.append(test)
                diagnostics[test] = str(report.get("output") or "")[-1000:]
            else:
                reports += 1
                entries[test] = {
                    "exec": exec_file.relative_to(root).as_posix(),
                    "xml": xml_file.relative_to(root).as_posix(),
                    "exec_sha256": hashlib.sha256(exec_file.read_bytes()).hexdigest(),
                    "xml_sha256": hashlib.sha256(xml_file.read_bytes()).hexdigest(),
                }
    elif info.get("gradle"):
        gradle = info["tools"].get("gradle")
        if not gradle:
            return {"ok": False, "reason": "Gradle is unavailable"}
        destination = root / "build" / "jacoco" / "per-test"
        exec_dir = root / "build" / "jacoco" / "per-test-exec"
        for test in tests:
            safe = _safe_test_name(test)
            exec_file = exec_dir / f"{safe}.exec"
            xml_file = destination / f"session_{safe}.xml"
            exec_file.parent.mkdir(parents=True, exist_ok=True)
            exec_file.unlink(missing_ok=True)
            fd, init_name = tempfile.mkstemp(prefix="cachelayer-jacoco-", suffix=".init.gradle")
            os.close(fd)
            init_script = Path(init_name)
            init_script.write_text(
                """
allprojects {
    plugins.withId("java") {
        apply plugin: "jacoco"
        tasks.withType(Test).configureEach {
            jacoco.destinationFile = file(System.getProperty("cachelayer.jacoco.dest"))
        }
    }
}
""".strip() + "\n",
                encoding="utf-8",
            )
            try:
                result = run_cmd(
                    [
                        gradle, "--no-daemon", "-q", "-I", str(init_script),
                        f"-Dcachelayer.jacoco.dest={exec_file}", "test", "--tests", test,
                    ],
                    cwd=root, timeout=timeout, memory_mb=None,
                    env=_bounded_java_env(),
                )
            finally:
                init_script.unlink(missing_ok=True)
            report = (
                _jacoco_report_from_exec(root, exec_file, xml_file, cli, timeout)
                if result.get("ok") else result
            )
            if not report.get("ok"):
                failures.append(test)
                diagnostics[test] = str(report.get("output") or "")[-1000:]
            else:
                reports += 1
                entries[test] = {
                    "exec": exec_file.relative_to(root).as_posix(),
                    "xml": xml_file.relative_to(root).as_posix(),
                    "exec_sha256": hashlib.sha256(exec_file.read_bytes()).hexdigest(),
                    "xml_sha256": hashlib.sha256(xml_file.read_bytes()).hexdigest(),
                }
    else:
        return {"ok": False, "reason": "no Maven or Gradle project detected"}
    manifest = _write_jacoco_manifest(root, destination, entries)
    return {
        "ok": not failures and reports == len(tests),
        "tests": len(tests),
        "reports": reports,
        "failures": failures[:50],
        "diagnostics": diagnostics,
        "artifact": str(destination),
        "manifest": str(manifest),
        "source": "jacoco-explicit-per-test-baseline",
    }


def seed_native_rts(
    root: Path, info: dict[str, Any], kind: str, timeout: int,
) -> dict[str, Any]:
    if not info.get("maven") or not info["tools"].get("mvn"):
        return {"ok": False, "reason": f"{kind} baseline currently requires Maven"}
    mvn = info["tools"]["mvn"]
    if kind == "starts":
        argv = [
            mvn, "-q", "-DfailIfNoTests=false",
            f"edu.illinois:starts-maven-plugin:{STARTS_VERSION}:starts",
        ]
    else:
        argv = [
            mvn, "-q", "-DfailIfNoTests=false", "-Dekstazi.forceall=true",
            f"org.ekstazi:ekstazi-maven-plugin:{EKSTAZI_VERSION}:select", "test",
        ]
    result = run_cmd(
        argv, cwd=root, timeout=timeout, memory_mb=None,
        env=_bounded_java_env(),
    )
    artifact = ".starts" if kind == "starts" else ".ekstazi"
    locations = [str(path) for path in root.glob(f"**/{artifact}") if path.is_dir()]
    if kind == "ekstazi" and not locations:
        core = (
            Path.home() / ".m2" / "repository" / "org" / "ekstazi"
            / "org.ekstazi.core" / EKSTAZI_VERSION
            / f"org.ekstazi.core-{EKSTAZI_VERSION}.jar"
        )
        if not core.is_file():
            fetched = run_cmd(
                [
                    mvn, "-q", "dependency:get",
                    f"-Dartifact=org.ekstazi:org.ekstazi.core:{EKSTAZI_VERSION}",
                ],
                cwd=root, timeout=timeout, memory_mb=None, env=_bounded_java_env(),
            )
            if not fetched.get("ok"):
                return {
                    "ok": False, "runner": "ekstazi-baseline",
                    "artifacts": [], "summary": (fetched.get("output") or "")[-1000:],
                }
        root_uri = (root / ".ekstazi").resolve().as_uri()
        agent = (
            f"-javaagent:{core}=mode=JUNITFORK,force.all=true,"
            f"root.dir={root_uri}"
        )
        result = run_cmd(
            [mvn, "-q", "-DfailIfNoTests=false", f"-DargLine={agent}", "test"],
            cwd=root, timeout=timeout, memory_mb=None, env=_bounded_java_env(),
        )
        locations = [str(path) for path in root.glob("**/.ekstazi") if path.is_dir()]
    return {
        "ok": bool(result.get("ok")) and bool(locations),
        "runner": f"{kind}-baseline",
        "artifacts": locations[:50],
        "summary": (result.get("output") or "")[-1000:],
    }


def prepare_tia(
    *,
    mode: str = "status",
    confirm_full_baseline: bool = False,
    max_tests: int = 200,
    timeout: int = 300,
    cwd: str | None = None,
) -> dict[str, Any]:
    info = detect(cwd)
    root = Path(info["root"])
    mode = str(mode or "status").lower()
    if mode == "status":
        return {
            "ok": True,
            "runner": "tia-prepare-status",
            "root": str(root),
            "tools": info.get("tools", {}),
            "artifacts": info.get("artifacts", {}),
            "java": bool(info.get("java")),
        }
    if mode not in {"jacoco", "starts", "ekstazi", "joern", "all"}:
        return {"ok": False, "reason": f"unsupported prepare mode: {mode}"}
    full_modes = {"jacoco", "starts", "ekstazi", "all"}
    if mode in full_modes and not confirm_full_baseline:
        return {
            "ok": False,
            "confirmation_required": True,
            "reason": "this baseline may execute every test; set confirm_full_baseline=true",
        }
    max_tests = max(1, min(int(max_tests), 1000))
    timeout = max(30, min(int(timeout), 900))
    steps: dict[str, Any] = {}
    if mode in {"jacoco", "all"}:
        steps["jacoco"] = seed_jacoco_per_test(
            root, info, max_tests=max_tests, timeout=timeout,
        )
    if mode in {"starts", "all"}:
        steps["starts"] = seed_native_rts(root, info, "starts", timeout)
    if mode in {"ekstazi", "all"}:
        steps["ekstazi"] = seed_native_rts(root, info, "ekstazi", timeout)
    if mode in {"joern", "all"}:
        steps["joern"] = build_joern_cpg(root, timeout)
    return {
        "ok": bool(steps) and all(step.get("ok") for step in steps.values()),
        "runner": "tia-prepare",
        "mode": mode,
        "steps": steps,
        "mutated_build_files": False,
    }
