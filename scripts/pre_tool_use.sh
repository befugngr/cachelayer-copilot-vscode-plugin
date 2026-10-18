#!/usr/bin/env bash
# CacheLayer PreToolUse hook for GitHub Copilot / VS Code (fail-open).
set -u

URL="${CACHELAYER_HOOK_URL:-https://api.cachelayer.org/hooks/pre-tool-use}"
TOKEN="${CACHELAYER_KEY:-${CACHELAYER_TOKEN:-${CACHELAYER_CONNECT_TOKEN:-}}}"
TIMEOUT="${CACHELAYER_HOOK_TIMEOUT_S:-0.5}"

FLOW_HDR=()
if [[ -n "${CACHELAYER_FLOW_ID:-}" ]]; then
  FLOW_HDR=(-H "x-cachelayer-flow: ${CACHELAYER_FLOW_ID}")
fi


ROOT="$(cd "$(dirname "$0")" && pwd)"
allow() {
  printf '%s\n' "{\"continue\":true,\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\",\"permissionDecisionReason\":\"$1\"}}"
}

if [[ -z "$TOKEN" ]]; then
  allow "no_token"
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1 && ! command -v py >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  allow "no_python"
  exit 0
fi
PY=python3
command -v python3 >/dev/null 2>&1 || { command -v py >/dev/null 2>&1 && PY="py -3"; } || PY=python
INPUT="$(python3 "$ROOT/filter_hook_payload.py" 2>/dev/null || true)"
if [[ -z "$INPUT" ]]; then
  allow "skipped_non_read_or_secret"
  exit 0
fi

RESP="$(curl -sS --max-time "$TIMEOUT" \
  -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TOKEN}" \
  "${FLOW_HDR[@]}" \
  -d "$INPUT" 2>/dev/null || true)"

if [[ -z "$RESP" ]]; then
  allow "lookup_unreachable"
  exit 0
fi

OUT="$(printf '%s' "$RESP" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception as e:
    print(json.dumps({"continue":True,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"bad_response"}})); sys.exit(0)
if not isinstance(d, dict):
    print(json.dumps({"continue":True,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"bad_response"}})); sys.exit(0)
hso=d.get("hookSpecificOutput") if isinstance(d.get("hookSpecificOutput"), dict) else {"hookEventName":"PreToolUse","permissionDecision":"allow"}
cl=d.get("cachelayer") if isinstance(d.get("cachelayer"), dict) else {}
err=d.get("error") or cl.get("error")
hit=bool(d.get("hit") or cl.get("hit"))
result=d.get("result") if d.get("result") is not None else cl.get("result")
hso.pop("additionalContext", None)
if hit and result is not None and not err:
    rendered=result if isinstance(result, str) else json.dumps(result, default=str)
    hso["permissionDecision"]="deny"
    hso["permissionDecisionReason"]="cache_hit"
    hso["additionalContext"]="CacheLayer HIT. Use this cached result and do not re-read:\n"+rendered
    print(json.dumps({"continue":False,"hookSpecificOutput":hso,"updatedToolOutput":d.get("updatedToolOutput", result)}))
else:
    hso["permissionDecision"]="allow"
    hso["permissionDecisionReason"]=str(err or "cache_miss")
    print(json.dumps({"continue":True,"hookSpecificOutput":hso}))
' 2>/dev/null || true)"
if [[ -n "$OUT" ]]; then
  printf '%s\n' "$OUT"
  exit 0
fi
allow "lookup_parse_failed"
exit 0
