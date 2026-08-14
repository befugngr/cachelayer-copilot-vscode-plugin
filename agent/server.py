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
        "Run ONLY tests that touch the current change (pytest-testmon, mapped pytest, "
        "Jest findRelatedTests, Maven -Dtest, Gradle --tests, Ekstazi if configured). "
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
        "Returns FLITS-ranked frames, a bounded crash-function slice, optional real Ochiai ranks "
        "when a failing/passing coverage matrix is supplied, "
        "minimized input, and a self-debug rubric (input/expected/actual/fix). "
        "Do not call on passing tests."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "stack_trace": {"type": "string"},
            "test_output": {"type": "string"},
            "file": {"type": "string"},
            "line": {"type": "integer"},
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


def _debug(stack_trace="", test_output="", file=None, line=None, coverage_matrix=None, **_kw):
    return debug_failure(
        stack_trace=stack_trace or "",
        test_output=test_output or "",
        file=file,
        line=line,
        coverage_matrix=coverage_matrix,
    )


def main() -> None:
    serve({
        "verify_edit": (_VERIFY, _verify),
        "run_affected_tests": (_TIA, _tia),
        "debug_failure": (_DEBUG, _debug),
    })


if __name__ == "__main__":
    main()
