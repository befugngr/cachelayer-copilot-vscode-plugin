---
name: cachelayer-tools
description: >-
  Optional CacheLayer cache and local CRITIC/TIA/Debug tools. Use local
  loop-cutters once and avoid remote MCP calls before every native tool.
---

# CacheLayer tools

Set `CACHELAYER_KEY` to your `clct_<token>`. Existing hooks handle ordinary remote cache lookup/save.

## Local loop-cutters

- Call `verify_edit` **once after a coherent code edit** with edited paths; it gates typecheck, lint, then affected tests.
- Call `run_affected_tests` **once after edits** when targeted test evidence is needed and `verify_edit` did not already run tests.
- Call `debug_failure` **once only after a real failure**, passing the traceback or failing test output. Never call it on passing tests.
- Missing optional analyzers degrades to bounded fallbacks and install guidance. The plugin server itself uses Python 3 stdlib only.

## Remote cache MCP

Use `run_status` after interruption, `check_conflict` on risky writes, and `lookup_step` / `save_step` only for explicit expensive reuse. Use stable lowercase descriptors and one `run_id` per task.

## Do not

- MCP before every Read/Grep/native tool
- Save secrets
- Nest CacheLayer calls
