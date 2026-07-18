---
name: cachelayer-tools
description: >-
  Optional CacheLayer MCP tools. Prefer silent PreToolUse/PostToolUse hooks.
  Use MCP for run_status, check_conflict, or explicit expensive reuse.
---

# CacheLayer tools

Set `CACHELAYER_KEY` to your `clct_<token>`. Hooks handle lookup/save — do not MCP before every tool.

## Prefer hooks

PreToolUse → lookup · PostToolUse → save · fail-open ~2s

## When to call MCP

`run_status` · `check_conflict` on risky writes · explicit `lookup_step`/`save_step` for expensive reuse

## Descriptors

`read file <path>` · `run command <cmd>` · `search <query>` · one `run_id` per task

## Do not

MCP before every tool · save secrets · nest CacheLayer tools
