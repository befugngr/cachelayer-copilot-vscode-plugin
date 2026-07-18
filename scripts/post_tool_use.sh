#!/usr/bin/env bash
# CacheLayer PostToolUse — silent save (VS Code Copilot). Fail-open.
set -u
URL="${CACHELAYER_POST_HOOK_URL:-https://api.cachelayer.org/hooks/post-tool-use}"
TOKEN="${CACHELAYER_KEY:-${CACHELAYER_TOKEN:-${CACHELAYER_CONNECT_TOKEN:-}}}"
TIMEOUT="${CACHELAYER_HOOK_TIMEOUT_S:-2}"

INPUT="$(cat || true)"
if [[ -z "$INPUT" || -z "$TOKEN" ]]; then
  printf '%s\n' '{"continue":true}'
  exit 0
fi

curl -sS --max-time "$TIMEOUT" \
  -X POST "$URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TOKEN}" \
  -d "$INPUT" >/dev/null 2>&1 || true

printf '%s\n' '{"continue":true}'
exit 0
