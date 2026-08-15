---
name: cachelayer-tools
description: >-
  Optional CacheLayer cache and local CRITIC/TIA/Debug tools. Use local
  loop-cutters once and avoid remote MCP calls before every native tool.
---

# CacheLayer tools

Set `CACHELAYER_KEY` to your `clct_<token>`. Bundled hooks handle remote cache lookup/save and lint the file after each edit, so do not spend a turn linting that file yourself.

## Local loop-cutters

- Call `verify_edit` **once after a coherent code edit** with edited paths; it gates typecheck, lint, then affected tests.
- Call `run_affected_tests` **once after edits** when targeted test evidence is needed and `verify_edit` did not already run tests.
- `debug_failure` — call once after a real failure with its traceback/output. For verified minimization, also pass `failing_input` and bounded `repro.argv`; do not start a second debug loop.
- Missing optional analyzers degrades to bounded fallbacks and install guidance. The plugin server itself uses Python 3 stdlib only.

## Remote cache MCP

Use `run_status` after interruption, `check_conflict` on risky writes, and `lookup_step` / `save_step` only for explicit expensive reuse. Use stable lowercase descriptors and one `run_id` per task.

## Do not

- MCP before every Read/Grep/native tool
- Save secrets
- Nest CacheLayer calls
