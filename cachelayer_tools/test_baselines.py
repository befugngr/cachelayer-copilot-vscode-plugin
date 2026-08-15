"""Explicit, bounded preparation for real test-impact artifacts.

Nothing here edits a build file. Baselines can execute every test and therefore
require an explicit confirmation from the caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from .workspace_detect import detect, invalidate_detect
    from .process_util import SKIP_PARTS, run_cmd, which
except ImportError:
    from workspace_detect import detect, invalidate_detect
    from process_util import SKIP_PARTS, run_cmd, which

JACOCO_VERSION = "0.8.13"
STARTS_VERSION = "1.4"
EKSTAZI_VERSION = "5.3.0"
TOOLCHAIN = Path.home() / ".cache" / "cachelayer-toolchain"
MAX_JACOCO_ARTIFACT_BYTES = 25_000_000


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
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SKIP_PARTS]
        parts = Path(dirpath).relative_to(root).parts
        if not any(
            parts[index:index + 3] in (("src", "test", "java"), ("src", "test", "kotlin"))
            for index in range(max(0, len(parts) - 2))
        ):
            continue
        for name in filenames:
            if not name.endswith(("Test.java", "Tests.java", "IT.java", "Test.kt", "Tests.kt", "IT.kt")):
                continue
            visited += 1
            path = Path(dirpath) / name
            parts = list(path.with_suffix("").parts)
            marker = "java" if "java" in parts else "kotlin"
            value = ".".join(parts[parts.index(marker) + 1:])
            if value not in result:
                result.append(value)
            if visited >= 4000:
                return result
    return result


def _cache_key(root: Path) -> str:
    revision = run_cmd(
        ["git", "rev-parse", "HEAD"], cwd=root, timeout=5, memory_mb=None,
    ).get("output") or "working-tree"
    dirty = run_cmd(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root, timeout=5, memory_mb=None,
    ).get("output") or ""
    diff = run_cmd(
        ["git", "diff", "--binary", "HEAD", "--"], cwd=root,
        timeout=8, memory_mb=None,
    ).get("output") or ""
    untracked = hashlib.sha256()
    remaining = 2_000_000
    for line in str(dirty).splitlines():
        if not line.startswith("?? "):
            continue
        candidate = root / line[3:]
        try:
            candidate.resolve().relative_to(root.resolve())
            if not candidate.is_file():
                continue
            untracked.update(line[3:].encode())
            with candidate.open("rb") as stream:
                chunk = stream.read(min(remaining, 256_000))
            untracked.update(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                break
        except (OSError, ValueError):
            continue
    raw = (
        f"{root.resolve()}\0{str(revision).strip()}\0{dirty[:200_000]}"
        f"\0{diff[:2_000_000]}\0{untracked.hexdigest()}"
    ).encode()
    return hashlib.sha256(raw).hexdigest()[:20]


def jacoco_provenance(root: Path) -> dict[str, str]:
    """Fingerprint inputs which can change per-test coverage ownership."""
    digest = hashlib.sha256()
    input_names = {
        "pom.xml", "build.gradle", "build.gradle.kts",
        "settings.gradle", "settings.gradle.kts", "gradle.properties",
    }
    files: list[Path] = []
    entries = 0
    deadline = time.monotonic() + 1.0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SKIP_PARTS]
        for name in filenames:
            entries += 1
            if entries > 20_000 or time.monotonic() > deadline:
                break
            path = Path(dirpath) / name
            if name in input_names or path.suffix.lower() in {".java", ".kt", ".kts"}:
                files.append(path)
        if entries > 20_000 or time.monotonic() > deadline:
            break
    remaining_bytes = 25_000_000
    for path in sorted(set(files)):
        try:
            rel = path.relative_to(root).as_posix()
            digest.update(rel.encode())
            digest.update(b"\0")
            size = path.stat().st_size
            if size > remaining_bytes:
                digest.update(f"oversize:{size}".encode())
                remaining_bytes = 0
                break
            with path.open("rb") as stream:
                while size:
                    chunk = stream.read(min(size, 1024 * 1024))
                    if not chunk:
                        break
                    digest.update(chunk)
                    size -= len(chunk)
                    remaining_bytes -= len(chunk)
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
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    result = run_cmd(
        [parser, str(root), "--language", "javasrc", "--output", str(temporary)],
        cwd=root, timeout=max(30, min(timeout, 900)), memory_mb=None,
        env=_bounded_java_env(1200),
    )
    ok = bool(result.get("ok")) and temporary.is_file()
    if ok:
        temporary.replace(output)
    else:
        temporary.unlink(missing_ok=True)
    return {
        "ok": ok and output.is_file(),
        "available": True,
        "artifact": str(output) if output.is_file() else None,
        "summary": result.get("output", "")[-1000:],
        "timed_out": bool(result.get("timeout")),
    }


def _safe_test_name(test: str) -> str:
    return test.replace("$", "_").replace(".", "__").replace("#", "__")


def _named_artifact_dirs(root: Path, name: str, limit: int = 50) -> list[Path]:
    found: list[Path] = []
    visited = 0
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        visited += 1
        if visited > 4000:
            break
        current = Path(dirpath)
        if current.name == name:
            found.append(current)
            dirnames[:] = []
            if len(found) >= limit:
                break
            continue
        dirnames[:] = [child for child in dirnames if child not in SKIP_PARTS]
    return found


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
    expected_tests: int,
) -> Path:
    manifest = destination / "manifest.json"
    payload = {
        "schema": "cachelayer-jacoco-per-test-v1",
        "jacoco_version": JACOCO_VERSION,
        "provenance": jacoco_provenance(root),
        "complete": True,
        "expected_tests": expected_tests,
        "tests": entries,
    }
    temporary = manifest.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(manifest)
    return manifest


def _bounded_sha256(path: Path) -> str:
    size = path.stat().st_size
    if size <= 0 or size > MAX_JACOCO_ARTIFACT_BYTES:
        raise ValueError(f"artifact size {size} is outside the allowed bound")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = MAX_JACOCO_ARTIFACT_BYTES + 1
        while remaining > 0:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _jacoco_manifest_entry(root: Path, exec_file: Path, xml_file: Path) -> dict[str, str]:
    return {
        "exec": exec_file.relative_to(root).as_posix(),
        "xml": xml_file.relative_to(root).as_posix(),
        "exec_sha256": _bounded_sha256(exec_file),
        "xml_sha256": _bounded_sha256(xml_file),
    }


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
    deadline = time.monotonic() + max(1, timeout)

    def remaining() -> int:
        return max(1, int(deadline - time.monotonic()))

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
        (destination / "manifest.json").unlink(missing_ok=True)
        for index, test in enumerate(tests):
            if time.monotonic() >= deadline:
                failures.extend(tests[index:])
                diagnostics[test] = "global JaCoCo baseline deadline exhausted"
                break
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
                cwd=root, timeout=remaining(), memory_mb=None,
                env=_bounded_java_env(),
            )
            report = (
                _jacoco_report_from_exec(root, exec_file, xml_file, cli, remaining())
                if result.get("ok") else result
            )
            if not report.get("ok"):
                failures.append(test)
                diagnostics[test] = str(report.get("output") or "")[-1000:]
            else:
                try:
                    entries[test] = _jacoco_manifest_entry(root, exec_file, xml_file)
                    reports += 1
                except (OSError, ValueError) as exc:
                    failures.append(test)
                    diagnostics[test] = str(exc)
    elif info.get("gradle"):
        gradle = info["tools"].get("gradle")
        if not gradle:
            return {"ok": False, "reason": "Gradle is unavailable"}
        destination = root / "build" / "jacoco" / "per-test"
        exec_dir = root / "build" / "jacoco" / "per-test-exec"
        (destination / "manifest.json").unlink(missing_ok=True)
        for index, test in enumerate(tests):
            if time.monotonic() >= deadline:
                failures.extend(tests[index:])
                diagnostics[test] = "global JaCoCo baseline deadline exhausted"
                break
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
                    cwd=root, timeout=remaining(), memory_mb=None,
                    env=_bounded_java_env(),
                )
            finally:
                init_script.unlink(missing_ok=True)
            report = (
                _jacoco_report_from_exec(root, exec_file, xml_file, cli, remaining())
                if result.get("ok") else result
            )
            if not report.get("ok"):
                failures.append(test)
                diagnostics[test] = str(report.get("output") or "")[-1000:]
            else:
                try:
                    entries[test] = _jacoco_manifest_entry(root, exec_file, xml_file)
                    reports += 1
                except (OSError, ValueError) as exc:
                    failures.append(test)
                    diagnostics[test] = str(exc)
    else:
        return {"ok": False, "reason": "no Maven or Gradle project detected"}
    complete = not failures and reports == len(tests) and len(entries) == len(tests)
    manifest = (
        _write_jacoco_manifest(root, destination, entries, len(tests))
        if complete else None
    )
    return {
        "ok": complete,
        "tests": len(tests),
        "reports": reports,
        "failures": failures[:50],
        "diagnostics": diagnostics,
        "artifact": str(destination),
        "manifest": str(manifest) if manifest is not None else None,
        "source": "jacoco-explicit-per-test-baseline",
    }


def seed_native_rts(
    root: Path, info: dict[str, Any], kind: str, timeout: int,
) -> dict[str, Any]:
    if not info.get("maven") or not info["tools"].get("mvn"):
        return {"ok": False, "reason": f"{kind} baseline currently requires Maven"}
    mvn = info["tools"]["mvn"]
    deadline = time.monotonic() + max(1, timeout)
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
        argv, cwd=root, timeout=max(1, int(deadline - time.monotonic())),
        env=_bounded_java_env(),
    )
    artifact = ".starts" if kind == "starts" else ".ekstazi"
    locations = [str(path) for path in _named_artifact_dirs(root, artifact)]
    if kind == "ekstazi" and not locations:
        core = (
            Path.home() / ".m2" / "repository" / "org" / "ekstazi"
            / "org.ekstazi.core" / EKSTAZI_VERSION
            / f"org.ekstazi.core-{EKSTAZI_VERSION}.jar"
        )
        if not core.is_file():
            if time.monotonic() >= deadline:
                return {
                    "ok": False, "runner": "ekstazi-baseline",
                    "artifacts": [], "summary": "global preparation deadline exhausted",
                }
            fetched = run_cmd(
                [
                    mvn, "-q", "dependency:get",
                    f"-Dartifact=org.ekstazi:org.ekstazi.core:{EKSTAZI_VERSION}",
                ],
                cwd=root, timeout=max(1, int(deadline - time.monotonic())),
                memory_mb=None, env=_bounded_java_env(),
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
        if time.monotonic() >= deadline:
            return {
                "ok": False, "runner": "ekstazi-baseline",
                "artifacts": [], "summary": "global preparation deadline exhausted",
            }
        result = run_cmd(
            [mvn, "-q", "-DfailIfNoTests=false", f"-DargLine={agent}", "test"],
            cwd=root, timeout=max(1, int(deadline - time.monotonic())),
            memory_mb=None, env=_bounded_java_env(),
        )
        locations = [str(path) for path in _named_artifact_dirs(root, ".ekstazi")]
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
    deadline = time.monotonic() + timeout
    steps: dict[str, Any] = {}
    def remaining() -> int:
        return max(1, int(deadline - time.monotonic()))

    if mode in {"jacoco", "all"}:
        steps["jacoco"] = seed_jacoco_per_test(
            root, info, max_tests=max_tests, timeout=remaining(),
        )
    if mode in {"starts", "all"}:
        if time.monotonic() >= deadline:
            steps["starts"] = {"ok": False, "reason": "global preparation deadline exhausted"}
        else:
            steps["starts"] = seed_native_rts(root, info, "starts", remaining())
    if mode in {"ekstazi", "all"}:
        if time.monotonic() >= deadline:
            steps["ekstazi"] = {"ok": False, "reason": "global preparation deadline exhausted"}
        else:
            steps["ekstazi"] = seed_native_rts(root, info, "ekstazi", remaining())
    if mode in {"joern", "all"}:
        if time.monotonic() >= deadline:
            steps["joern"] = {"ok": False, "reason": "global preparation deadline exhausted"}
        else:
            steps["joern"] = build_joern_cpg(root, remaining())
    ok = bool(steps) and all(step.get("ok") for step in steps.values())
    if ok:
        invalidate_detect()
    return {
        "ok": ok,
        "runner": "tia-prepare",
        "mode": mode,
        "steps": steps,
        "mutated_build_files": False,
    }
