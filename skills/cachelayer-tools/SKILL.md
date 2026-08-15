---
name: cachelayer-tools
description: >-
  Optional CacheLayer cache and local CRITIC/TIA/Debug tools. Prefer silent
  cache hooks; use each local loop-cutter once at the appropriate point.
---

# CacheLayer tools

Set `CACHELAYER_KEY` to your `clct_<token>`. Silent hooks handle bounded lookup/save for explicit read/search tools only; do not MCP-tax every step.

## Local loop-cutters

- Call `verify_edit` **once after a coherent code edit** with the edited paths, `mode: "coherent"`, and a stable `edit_cycle_id`. Independent typecheck/lint prerequisites run concurrently; affected tests run only after every prerequisite passes.
- On failure, obey `feedback.action`: make exactly one coherent corrective edit for `re_edit_once`, reuse the cycle ID, and call once again. Stop and report when it says `stop_and_report` (maximum three attempts). CRITIC returns context; it never writes user code.
- The automatic editor hook stays fast and file-scoped by default. An editor integration may explicitly send `critic_mode: "coherent"` (or `full_gate: true`) once after a batch to request the full gate.
- Call `run_affected_tests` **once after edits** when targeted test evidence is needed and `verify_edit` did not already run them.
- Java TIA never treats aggregate `jacoco.xml` as a per-test map. It prefers Smart Picker, seeded STARTS/Ekstazi, runtime-hardened affectedTest, fresh manifest-labeled per-test JaCoCo exec/XML pairs, then Joern usage/data-flow slices (not a PDG) or bounded type/import mapping; full-suite escalation is refused.
- Use `seed_rts: true` for a non-mutating plan. Use `prepare_tia` with `mode: "joern"` to build a revision-keyed CPG without running tests. For `jacoco`, `starts`, `ekstazi`, or `all`, set `confirm_full_baseline: true` because every test may run; build files are never rewritten.
- STARTS/Ekstazi selection counts prefer official plugin output and use native artifact parsing only as fallback. Artifact discovery is budgeted; `artifact_scan_complete` is false when capped. Set `CACHELAYER_TIA_ARTIFACT_PATHS` for unusual external roots. Detect pytest-testmon/Scalpel via `cachelayer-analysis-python`, not system Python alone.
- `debug_failure` — call once after a real failure with its traceback/output. Java SBFL runs Flacoco when available; GZoltar may be detected but is not invoked. For verified minimization, also pass `failing_input` and bounded `repro.argv`; do not start a second debug loop.
- Missing mypy, ruff, pytest-testmon/pytest-cov, Jest, JaCoCo per-test sessions, STARTS/Ekstazi seed data, Joern, or Flacoco is an expected degrade path; use the returned install hint or bounded fallback.

## Remote cache MCP

Use `run_status` after interruption, `check_conflict` before risky writes, and `lookup_step` / `save_step` only for explicit expensive reuse. Descriptors are lowercase verb + target, such as `read file <path>` or `run command <cmd>`; keep one `run_id` per task.

## Do not

- Call MCP before every Read/Grep/native tool
- Save secrets from environment files
- Call CacheLayer tools before other CacheLayer tools
