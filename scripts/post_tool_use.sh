#!/usr/bin/env bash
# CacheLayer PostToolUse save for VS Code Copilot. Fail-open. Does not wait.
set -u
URL="${CACHELAYER_POST_HOOK_URL:-https://api.cachelayer.org/hooks/post-tool-use}"
TOKEN="${CACHELAYER_KEY:-${CACHELAYER_TOKEN:-${CACHELAYER_CONNECT_TOKEN:-}}}"

FLOW_HDR=()
if [[ -n "${CACHELAYER_FLOW_ID:-}" ]]; then
  FLOW_HDR=(-H "x-cachelayer-flow: ${CACHELAYER_FLOW_ID}")
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
done_ok() {
  printf '%s\n' '{"continue":true,"hookSpecificOutput":{"hookEventName":"PostToolUse"}}'
}

if [[ -z "$TOKEN" ]] || ! command -v python3 >/dev/null 2>&1; then
  done_ok
  exit 0
fi
INPUT="$(python3 "$ROOT/filter_hook_payload.py" 2>/dev/null || true)"
if [[ -n "$INPUT" ]]; then
  nohup curl -sS --max-time 5 \
    -X POST "$URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${TOKEN}" \
    "${FLOW_HDR[@]}" \
    -d "$INPUT" >/dev/null 2>&1 &
fi
done_ok
exit 0
