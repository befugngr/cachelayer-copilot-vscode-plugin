#!/usr/bin/env python3
"""Local stdio MCP: verify_edit, run_affected_tests, debug_failure.

Launched by the editor from this file path. Workspace cwd is the user repo.
No third-party packages required.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from critic import verify_edit
from debug import debug_failure
from protocol import serve
from tia import run_affected_tests

_VERIFY = {
    "name": "verify_edit",
    "description": (
        "After you edit code, call ONCE to catch type/lint errors before more LLM turns. "
        "Runs mypy then ruff (Python), tsc --noEmit then eslint (JS/TS). "
        "Runs affected tests only if typecheck and lint pass. "
        "Pass the files you just edited. Do not call on markdown, reads, or every step."
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
        },
    },
}

_TIA = {
    "name": "run_affected_tests",
    "description": (
        "Run ONLY tests that touch the current change: pytest-testmon or coverage contexts, "
        "Jest findRelatedTests, Maven Surefire/STARTS/Ekstazi/Smart Test Picker, or Gradle "
        "--tests/affectedTest/Smart Test Picker. Reports JaCoCo changed-line coverage separately "
        "from test selection and uses a bounded static Java forward-slice when no dynamic map exists. "
        "Use this instead of the full test suite after edits. Do not poll a long full run."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "changed_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Changed files; default git diff",
            }
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


def _verify(paths=None, line_range=None, run_tests=True, **_kw):
    return verify_edit(paths=paths, line_range=line_range, run_tests=bool(run_tests), hook=False)


def _tia(changed_files=None, **_kw):
    return run_affected_tests(changed_files=changed_files)


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
        "debug_failure": (_DEBUG, _debug),
    })


if __name__ == "__main__":
    main()
