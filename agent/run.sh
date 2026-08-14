#!/usr/bin/env bash
# Resolve this script even when the editor cwd is the user workspace.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="${CACHELAYER_WORKSPACE_ROOT:-${CLAUDE_PROJECT_DIR:-${CODEX_WORKSPACE_ROOT:-${CURSOR_WORKSPACE_ROOT:-${GITHUB_WORKSPACE:-}}}}}"
if [ -n "$WORKSPACE" ] && [ -d "$WORKSPACE" ]; then
  cd "$WORKSPACE"
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$ROOT/server.py"
fi
if command -v py >/dev/null 2>&1; then
  exec py -3 "$ROOT/server.py"
fi
exec python "$ROOT/server.py"
