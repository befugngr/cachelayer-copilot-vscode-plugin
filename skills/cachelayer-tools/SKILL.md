---
name: cachelayer-tools
description: >-
  CacheLayer step-cache discipline for Copilot agents. Use when running multi-step
  coding tasks so prior safe steps can be reused via lookup_step/save_step instead
  of recomputing. Not for model-inference caching.
---

# CacheLayer tools (managed-keys / agent-loop only)

This skill caches **agent steps** (tool work), not model inference. On a Copilot subscription the model transport is unreachable — do not imply otherwise.

Hook auth uses env `CACHELAYER_KEY` (`clct_<your-token>`). MCP prompts for the same token via `${input:cachelayer-token}`.

## Shared descriptor normalization (MUST match hook + MCP)

Lowercase, trimmed, **verb + target**:

- `read file <path>`
- `write file <path>`
- `edit file <path>`
- `run command <cmd>`
- `search <query>`

Examples: `read file src/auth.ts`, `edit file package.json`, `run command npm test`.

Bad: vague phrases, full user prompts, multi-sentence plans, raw Claude-only names without a target.

## `run_id`

- One UUID per task
- Reuse it for every `lookup_step`, `save_step`, `check_conflict`, `run_status` in that task
- New UUID for a new task

## Calling order

1. `lookup_step(description, run_id)` before a native step. On hit: **reuse `result`** — do not recompute or re-verify unless `check_conflict` demands it.
2. `check_conflict(intended_action, run_id)` before file edits or destructive commands. If `safe` is false, stop.
3. `save_step(step_id, run_id, description, result)` after every completed step (including after a cache hit). `description` MUST match the lookup phrasing; `result` is the actual output.
4. `run_status(run_id)` to recover after interruption.

## Do not

- Save secrets from env files or tokens
- Use vague descriptors
- Skip `save_step` for "trivial" steps
- Call `lookup_step` before CacheLayer MCP tools themselves
