#!/usr/bin/env bash
# Off unless CACHELAYER_VERIFY=1 so edits are not delayed.
if [[ "${CACHELAYER_VERIFY:-}" != "1" ]]; then
  printf '%s\n' '{}'
  exit 0
fi
# Fail-open post-edit CRITIC wrapper.
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
if command -v python3 >/dev/null 2>&1; then
  python3 "$ROOT/verify_hook.py" || printf '%s\n' '{}'
elif command -v py >/dev/null 2>&1; then
  py -3 "$ROOT/verify_hook.py" || printf '%s\n' '{}'
else
  python "$ROOT/verify_hook.py" || printf '%s\n' '{}'
fi
