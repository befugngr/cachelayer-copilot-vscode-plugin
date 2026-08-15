#!/usr/bin/env python3
"""Local stdio MCP: verify_edit, run_affected_tests, prepare_tia, debug_failure.

Launched by the editor from this file path. Workspace cwd is the user repo.
No third-party packages required.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from verify_edit import verify_edit
from debug_failure import debug_failure
from mcp_protocol import serve
from affected_tests import run_affected_tests
from test_baselines import prepare_tia

_VERIFY = {
    "name": "verify_edit",
    "description": (
        "After one coherent edit batch, call ONCE with mode=coherent and a stable edit_cycle_id. "
        "Independent typecheck/lint prerequisites run concurrently; affected tests run only if all pass. "
        "On failure, follow feedback.action with one coherent re-edit, reuse the cycle ID, and stop at the cap. "
        "The tool never edits code. Do not call on markdown, reads, or every keystroke."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Edited file paths"},
            "line_range": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2,
                "description": "Optional [start_line, end_line] to filter diagnostics",
            },
            "run_tests": {"type": "boolean", "default": True},
            "mode": {
                "type": "string",
                "enum": ["fast", "coherent"],
                "default": "coherent",
                "description": "fast is file-scoped; coherent is the explicit post-batch full gate.",
            },
            "edit_cycle_id": {
                "type": "string",
                "maxLength": 160,
                "description": "Stable ID reused only for the bounded edit/critic/re-edit cycle.",
            },
            "max_retries": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3,
                "default": 3,
            },
        },
    },
}

_TIA = {
    "name": "run_affected_tests",
    "description": (
        "Run only safely selected tests. Priority is Smart Test Picker, seeded STARTS/Ekstazi, "
        "safety-inspected Gradle affectedTest, native per-test JaCoCo reports, then Joern "
        "usage/data-flow slices (not a PDG) or bounded static type/import selection. Aggregate "
        "jacoco.xml is validation only. FULL_SUITE escalation is refused. Set seed_rts=true only "
        "to return a non-mutating install/baseline plan."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "changed_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Changed files; default git diff",
            },
            "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 45},
            "seed_rts": {
                "type": "boolean",
                "default": False,
                "description": "Return a dry-run RTS install/seed plan; never rewrites build files.",
            },
        },
    },
}

_PREPARE_TIA = {
    "name": "prepare_tia",
    "description": (
        "Create real TIA baselines without editing pom.xml/build.gradle. status is read-only; "
        "joern builds a cached Java CPG; jacoco creates one XML report per test; starts/ekstazi "
        "run their official baseline goals. Full-test baselines require confirm_full_baseline=true."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["status", "jacoco", "starts", "ekstazi", "joern", "all"],
                "default": "status",
            },
            "confirm_full_baseline": {
                "type": "boolean",
                "default": False,
                "description": "Required for modes that may execute every test.",
            },
            "max_tests": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            "timeout": {"type": "integer", "minimum": 30, "maximum": 900, "default": 300},
        },
    },
}

_DEBUG = {
    "name": "debug_failure",
    "description": (
        "When you have a stack trace or failing test output, call ONCE instead of grepping. "
        "Returns FLITS-ranked frames, a Python def-use/control slice or Joern data-flow slice, "
        "and real Ochiai ranks from supplied or automatically generated coverage contexts. "
        "With failing_input + repro argv, reruns a bounded no-shell ddmin/HDD oracle. "
        "Java projects can use Flacoco from PATH or FLACOCO_JAR. "
        "Do not call on passing tests."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "stack_trace": {"type": "string"},
            "test_output": {"type": "string"},
            "file": {"type": "string"},
            "line": {"type": "integer"},
            "auto_coverage": {
                "type": "boolean",
                "default": True,
                "description": "Rerun only parsed failing pytest files with coverage contexts when needed.",
            },
            "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 45},
            "failing_input": {
                "type": "string",
                "description": "Optional input to minimize with a real reproduction command.",
            },
            "repro": {
                "type": "object",
                "description": "Bounded no-shell failure oracle for ddmin/HDD.",
                "required": ["argv"],
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 20,
                        "description": "Command argv; use {input} for a temporary input file, otherwise input is stdin.",
                    },
                    "failure_pattern": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 15, "default": 5},
                    "max_runs": {"type": "integer", "minimum": 1, "maximum": 50, "default": 30},
                },
            },
            "coverage_matrix": {
                "type": "array",
                "maxItems": 10000,
                "description": "Optional rows with file, line, failed_covered, passed_covered.",
                "items": {
                    "type": "object",
                    "required": ["file", "line", "failed_covered", "passed_covered"],
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "failed_covered": {"type": "integer", "minimum": 0},
                        "passed_covered": {"type": "integer", "minimum": 0},
                    },
                },
            },
        },
    },
}


def _verify(
    paths=None, line_range=None, run_tests=True, mode="coherent",
    edit_cycle_id=None, max_retries=3, **_kw
):
    return verify_edit(
        paths=paths,
        line_range=line_range,
        run_tests=bool(run_tests),
        hook=False,
        mode=mode if mode in ("fast", "coherent") else "coherent",
        edit_cycle_id=edit_cycle_id,
        max_retries=max_retries,
    )


def _tia(changed_files=None, timeout=45, seed_rts=False, **_kw):
    return run_affected_tests(
        changed_files=changed_files,
        timeout=max(1, min(int(timeout), 300)),
        seed_rts=bool(seed_rts),
    )


def _prepare_tia(
    mode="status", confirm_full_baseline=False, max_tests=200, timeout=300, **_kw
):
    return prepare_tia(
        mode=mode,
        confirm_full_baseline=bool(confirm_full_baseline),
        max_tests=max_tests,
        timeout=timeout,
    )


def _debug(
    stack_trace="", test_output="", file=None, line=None, coverage_matrix=None,
    auto_coverage=True, timeout=45, failing_input="", repro=None, **_kw
):
    return debug_failure(
        stack_trace=stack_trace or "",
        test_output=test_output or "",
        file=file,
        line=line,
        coverage_matrix=coverage_matrix,
        auto_coverage=bool(auto_coverage),
        timeout=max(1, min(int(timeout), 60)),
        failing_input=failing_input or "",
        repro=repro,
    )


def main() -> None:
    serve({
        "verify_edit": (_VERIFY, _verify),
        "run_affected_tests": (_TIA, _tia),
        "prepare_tia": (_PREPARE_TIA, _prepare_tia),
        "debug_failure": (_DEBUG, _debug),
    })


if __name__ == "__main__":
    main()
