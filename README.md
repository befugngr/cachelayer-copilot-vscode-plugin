# CacheLayer for GitHub Copilot (VS Code)

Step caching for Copilot **agent** tools (managed keys). Reuses prior safe steps via MCP tools and a PreToolUse hook. Does **not** intercept or cache model inference.

Site: https://cachelayer.org/

## Prerequisites

- VS Code (1.99+ for MCP support)
- GitHub Copilot extension (agent mode)
- A **CacheLayer account** and connect token (`clct_…`) — required; MCP returns **401** without it
- **python3** (3.7+) on PATH — PreToolUse hook on macOS/Linux
- On Windows: PowerShell 5+ (bundled `.ps1` hook)

## 1. Get a connect token

1. Sign up or sign in at https://cachelayer.org/
2. Create a connect token from your account (API: `POST /user/connect-token` while logged in)
3. Copy the full value once — it looks like `clct_<your-token>`

You will paste it into VS Code’s MCP prompt and set `CACHELAYER_KEY` for the hook.

## 2. Install

Enable plugins in settings if needed:

```json
"chat.plugins.enabled": true
```

Then:

1. Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`)
2. **Chat: Install Plugin From Source**
3. Paste: `https://github.com/befugngr/cachelayer-copilot-vscode-plugin`
4. Reload VS Code

Local clone mapping:

```json
"chat.pluginLocations": {
  "/absolute/path/to/cachelayer-copilot-vscode-plugin": true
}
```

Confirm under **Agent Plugins - Installed**.

## 3. Auth (required)

**MCP:** On first use VS Code prompts for the connect token (`.mcp.json` → `${input:cachelayer-token}`, stored securely). That becomes:

`Authorization: Bearer <token>` → `https://api.cachelayer.org/mcp`

**Hook:** also set `CACHELAYER_KEY` in the environment that launches VS Code (hooks cannot use the MCP input secret):

| OS | How |
|----|-----|
| macOS / Linux | `export CACHELAYER_KEY='clct_<your-token>'` in shell profile, then launch VS Code from that environment |
| Windows | System Properties → Environment Variables, or `$env:CACHELAYER_KEY='clct_<your-token>'` before starting VS Code |

Hook URL: `https://api.cachelayer.org/hooks/pre-tool-use` (fail-open, 5s timeout).

Missing MCP token → **401**. Missing hook token / downtime → fail-open (agent continues; no cache).

## 4. Verify

- **Agent Plugins - Installed** lists `cachelayer`
- **MCP: List Servers** shows `cachelayer`
- **Configure Tools** shows `lookup_step`, `save_step`, `check_conflict`, `run_status`
- **Configure Skills** shows `cachelayer-tools`
- **GitHub Copilot Chat Hooks** output channel shows PreToolUse activity
- A test `lookup_step` does not return unauthorized / 401

## Tools

- `lookup_step(description, run_id)` before a step; on hit reuse `result`
- `save_step(step_id, run_id, description, result)` after a step
- `check_conflict(intended_action, run_id)` before edits
- `run_status(run_id)` after interruption

One UUID `run_id` per task. Descriptors: lowercase verb + target (`read file src/auth.ts`).

## Hardening

Do not enable `chat.tools.edits.autoApprove` for this plugin’s hook scripts (`scripts/pre_tool_use.sh`, `scripts/pre_tool_use.ps1`).

## Limits

- Fail-open: CacheLayer down / slow / 401 on the hook → agent continues without cache
- Write/mutating tool results are not served as replayable
- Not model-call caching

## Compliance

1. **No impersonation.** CacheLayer only; not GitHub, Microsoft, or Copilot.
2. **No malicious code.**
3. **Transparent requirement.** A CacheLayer account/subscription is required.

## Contact

https://cachelayer.org/

## Legal

Apache License 2.0. See `LICENSE`.
