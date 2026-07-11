# CacheLayer for GitHub Copilot (VS Code)

Step caching for Copilot **agent** tools on a Copilot subscription (managed keys). Reuses prior safe steps via MCP tools and a PreToolUse hook. Does **not** intercept or cache model inference — that traffic is unreachable on managed keys.

Site: https://cachelayer.org/

## Prerequisites

- VS Code with GitHub Copilot (agent mode)
- A CacheLayer account and connect token (`clct_...`)

## Install

1. Command Palette → **Chat: Install Plugin From Source** → paste this repo URL.
2. Or map a local clone in settings:

```json
"chat.pluginLocations": {
  "/absolute/path/to/cachelayer-copilot-vscode-plugin": true
}
```

3. Reload / enable the plugin. Confirm under **Agent Plugins - Installed**.

## Token provisioning

**MCP:** On first use VS Code prompts for the CacheLayer connect token (`inputs` → `${input:cachelayer-token}` in `.mcp.json`). Stored securely (`password: true`).

**Hook:** set env `CACHELAYER_TOKEN` (never commit the value):

| OS | How |
|----|-----|
| macOS / Linux | `export CACHELAYER_TOKEN=clct_...` in shell profile, or set in the environment that launches VS Code |
| Windows | System Properties → Environment Variables, or `$env:CACHELAYER_TOKEN="clct_..."` before starting VS Code |

Unauthenticated MCP/hook API calls return **401**. The hook **fail-opens** (allows the tool) on missing token, timeout, or non-2xx so the agent never blocks on CacheLayer.

## Verify

- **Agent Plugins - Installed** lists `cachelayer`
- **MCP: List Servers** shows `cachelayer`
- **Configure Tools** shows `lookup_step`, `save_step`, `check_conflict`, `run_status`
- **Configure Skills** shows `cachelayer-tools` (`/cachelayer:cachelayer-tools`)
- **GitHub Copilot Chat Hooks** output channel shows PreToolUse activity

## Tools

- `lookup_step(description, run_id)` before a step; on hit reuse `result`
- `save_step(step_id, run_id, description, result)` after a step
- `check_conflict(intended_action, run_id)` before edits
- `run_status(run_id)` after interruption

One UUID `run_id` per task. Descriptors: lowercase verb + target (`read file src/auth.ts`).

## Hardening

Do not enable `chat.tools.edits.autoApprove` for edits to this plugin’s hook scripts (`scripts/pre_tool_use.sh`, `scripts/pre_tool_use.ps1`). An agent that can rewrite those scripts could execute them.

## Limits

- Fail-open: CacheLayer down/slow/401 → agent continues without cache
- Write/mutating tool results are not served as replayable
- Not model-call caching

## Compliance

1. **No impersonation.** CacheLayer only; not GitHub, Microsoft, or Copilot.
2. **No malicious code.** Does not exfiltrate data, mutate the system without consent, or bundle malware.
3. **Transparent requirement.** A CacheLayer account/subscription is required.

## Contact

https://cachelayer.org/

## Legal

Apache License 2.0. See `LICENSE`.

<!-- Forward compat: OAuth via .mcp.json oauth:{clientId} — not implemented in v1.0.0 -->
