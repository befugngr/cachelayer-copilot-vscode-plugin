#!/usr/bin/env bash
# Resolve this script even when the editor cwd is the user workspace.
# VS Code plugin MCP: do not set CACHELAYER_WORKSPACE_ROOT via ${workspaceFolder}
# in plugin .mcp.json — that variable only resolves in workspace .vscode/mcp.json.
# When omitted, VS Code launches stdio MCP with cwd = open workspace folder.
set -e
export PYTHONDONTWRITEBYTECODE=1
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="${CACHELAYER_WORKSPACE_ROOT:-${CLAUDE_PROJECT_DIR:-${CODEX_WORKSPACE_ROOT:-${CURSOR_WORKSPACE_ROOT:-${GITHUB_WORKSPACE:-}}}}}"
if [ -n "$WORKSPACE" ] && [ -d "$WORKSPACE" ]; then
  cd "$WORKSPACE"
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$ROOT/mcp_server.py"
fi
if command -v py >/dev/null 2>&1; then
  exec py -3 "$ROOT/mcp_server.py"
fi
exec python "$ROOT/mcp_server.py"
