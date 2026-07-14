#!/usr/bin/env bash
# CacheLayer PreToolUse hook for GitHub Copilot / VS Code (fail-open).
# Env: CACHELAYER_KEY (required for caching; missing → allow, exit 0)
# Legacy: CACHELAYER_TOKEN / CACHELAYER_CONNECT_TOKEN still accepted.
# No CLAUDE_PLUGIN_ROOT — Copilot format. Script uses BASH_SOURCE for self-location.
set -u

URL="${CACHELAYER_HOOK_URL:-https://api.cachelayer.org/hooks/pre-tool-use}"
TOKEN="${CACHELAYER_KEY:-${CACHELAYER_TOKEN:-${CACHELAYER_CONNECT_TOKEN:-}}}"
TIMEOUT="${CACHELAYER_HOOK_TIMEOUT_S:-5}"

INPUT="$(cat || true)"
if [[ -z "$INPUT" ]]; then
  printf '%s\n' '{"continue":true}'
  exit 0
fi

# Fail-open if no token (agent proceeds; no caching)
if [[ -z "$TOKEN" ]]; then
  printf '%s\n' '{"continue":true,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"cachelayer_no_token"}}'
  exit 0
fi

RESP="$(curl -sS --max-time "$TIMEOUT" \
  -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TOKEN}" \
  -d "$INPUT" 2>/dev/null || true)"

if [[ -z "$RESP" ]]; then
  printf '%s\n' '{"continue":true}'
  exit 0
fi

# Map backend JSON → VS Code hook output (allow + additionalContext on hit)
if command -v python3 >/dev/null 2>&1; then
  OUT="$(printf '%s' "$RESP" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print(json.dumps({"continue":True})); sys.exit(0)
if isinstance(d, dict) and d.get("error"):
    # 401 body sometimes still 200-shaped; treat as fail-open
    print(json.dumps({"continue":True,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"cachelayer_auth_or_error"}}))
    sys.exit(0)
hso=d.get("hookSpecificOutput") if isinstance(d, dict) else None
if not isinstance(hso, dict):
    hso={"hookEventName":"PreToolUse","permissionDecision":"allow"}
# Prefer server-provided additionalContext; else build from cachelayer.result
if "additionalContext" not in hso:
    cl=d.get("cachelayer") if isinstance(d, dict) else None
    if isinstance(cl, dict) and cl.get("hit") and cl.get("result") is not None:
        r=cl["result"]
        if not isinstance(r, str):
            r=json.dumps(r, default=str)
        hso["additionalContext"]="CacheLayer reusable result for this step: "+r
out={"continue":True,"hookSpecificOutput":hso}
if isinstance(d, dict) and "cachelayer" in d:
    out["cachelayer"]=d["cachelayer"]
print(json.dumps(out))
' 2>/dev/null || true)"
  if [[ -n "$OUT" ]]; then
    printf '%s\n' "$OUT"
    exit 0
  fi
fi

# Fallback: pass through / allow
printf '%s\n' '{"continue":true}'
exit 0
