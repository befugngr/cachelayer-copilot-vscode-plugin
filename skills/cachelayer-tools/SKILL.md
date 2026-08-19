---
name: cachelayer-tools
description: >-
  CacheLayer step cache plus local verify/TIA/debug. After code edits and
  failures, use these tools; do not skip them for a full test suite.
---

# CacheLayer tools

Set `CACHELAYER_KEY` to `clct_<token>`. Silent read/search hooks lookup and save. Local tools still must run after edits and failures.

## Required after code edits

Call `verify_edit` once after a coherent batch with the edited paths, `mode: "coherent"`, and a stable `edit_cycle_id`. That runs typecheck, lint, then TIA (`run_affected_tests`). Do not run a full `npm test` / pytest suite first.

On `feedback.action = re_edit_once`, make one corrective edit, reuse the cycle ID, call `verify_edit` again. Stop at `stop_and_report`.

## Required after failures

If `tsc`, tests, or a terminal command fails, call `debug_failure` once with the traceback or command output. Do not only fix by guessing.

## Before risky writes

The pre-edit hook runs `check_conflict`. If it reports UNSAFE, do not apply the same mutation again.

## Full test suites

Do not run `npm test` / bare `pytest` first. The pre-terminal hook blocks a full suite and returns TIA (`run_affected_tests`). Use that result.

## Remote cache MCP

Use `run_status` after interruption. Hooks lookup/save reads and successful command results. Use `lookup_step` / `save_step` only if a hook miss is visible and the step is expensive. Descriptors: lowercase verb + target (`read file <path>`, `run command <cmd>`). Keep one `run_id` per task.

## Do not

- Skip CacheLayer and run the full test suite instead of TIA
- Call MCP before every Read/Grep (hooks already do that)
- Save secrets from env files
