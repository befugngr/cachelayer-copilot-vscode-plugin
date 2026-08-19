#!/usr/bin/env bash
# CacheLayer PostToolUse save for VS Code Copilot. Fail-open, visible result.
set -u
URL="${CACHELAYER_POST_HOOK_URL:-https://api.cachelayer.org/hooks/post-tool-use}"
TOKEN="${CACHELAYER_KEY:-${CACHELAYER_TOKEN:-${CACHELAYER_CONNECT_TOKEN:-}}}"
TIMEOUT="${CACHELAYER_HOOK_TIMEOUT_S:-2}"

ROOT="$(cd "$(dirname "$0")" && pwd)"
note() {
  printf '%s\n' "{\"continue\":true,\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"CacheLayer save: $1\"}}"
}

if [[ -z "$TOKEN" ]]; then
  note "no_token"
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
  note "no_python"
  exit 0
fi
INPUT="$(python3 "$ROOT/filter_hook_payload.py" 2>/dev/null || true)"
if [[ -z "$INPUT" ]]; then
  note "skipped_non_read_or_secret"
  exit 0
fi

RESP="$(curl -sS --max-time "$TIMEOUT" \
  -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TOKEN}" \
  -d "$INPUT" 2>/dev/null || true)"

if [[ -z "$RESP" ]]; then
  note "save_unreachable"
  exit 0
fi

OUT="$(printf '%s' "$RESP" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception as e:
    print(json.dumps({"continue":True,"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"CacheLayer save error: "+str(e)}})); sys.exit(0)
if not isinstance(d, dict):
    print(json.dumps({"continue":True,"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"CacheLayer save: invalid response"}})); sys.exit(0)
if d.get("stored"):
    msg="CacheLayer SAVED step "+str(d.get("description") or d.get("step_key") or "")
else:
    msg="CacheLayer NOT STORED: "+str(d.get("reason") or d.get("error") or "unknown")
print(json.dumps({"continue":True,"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":msg}}))
' 2>/dev/null || true)"
if [[ -n "$OUT" ]]; then
  printf '%s\n' "$OUT"
  exit 0
fi
note "save_parse_failed"
exit 0
