#!/usr/bin/env bash
# Fail-open post-edit CRITIC wrapper.
ROOT="$(cd "$(dirname "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  python3 "$ROOT/critic_hook.py" || printf '%s\n' '{}'
elif command -v py >/dev/null 2>&1; then
  py -3 "$ROOT/critic_hook.py" || printf '%s\n' '{}'
else
  python "$ROOT/critic_hook.py" || printf '%s\n' '{}'
fi
