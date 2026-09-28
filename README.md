# CacheLayer for GitHub Copilot

https://cachelayer.org/

CacheLayer controls the agent through silent tool hooks and MCP — not by proxying the LLM. Hooks look up/save steps and put only the needed cached result back on a hit.

Personal / BYOK: https://cachelayer.org/integrations/github-copilot

## How CacheLayer controls the agent

The plugin does **not** attach your editor to the LLM proxy. Silent hooks sit on tool use:

1. **Before** allowlisted read/search tools → lookup a prior safe step result
2. On **hit** → skip the native tool and put only that cached result back into the agent
3. **After** the tool → save the result for the next step
4. Optional MCP tools (`lookup_step`, `save_step`, `check_conflict`, `run_status`) for explicit control

Set `CACHELAYER_KEY` (`cl_…` or legacy `clct_…`). For per-flow Agent OS metrics in the console:

```bash
export CACHELAYER_FLOW_ID="<flow_id_from_console>"
```

Hooks and MCP stay on `https://api.cachelayer.org`.


## 1. Required VS Code settings

Add these to your **User** `settings.json` (Command Palette → **Preferences: Open User Settings (JSON)**):

```json
{
  "chat.plugins.enabled": true,
  "extensions.autoUpdate": "on"
}
```

Both are required. Without `extensions.autoUpdate`, VS Code will not pull plugin updates from GitHub (checked about every 24 hours). On VS Code 1.124 and earlier, use `"extensions.autoUpdate": true` instead of `"on"`.

## 2. Install the plugin from GitHub

1. Open Command Palette (`Cmd+Shift+P` / `Ctrl+Shift+P`)
2. Run **Chat: Install Plugin From Source**
3. Paste: `https://github.com/befugngr/cachelayer-copilot-vscode-plugin`

## 3. Add your CacheLayer token

Use a connect token from https://cachelayer.org/ (`cl_…` or legacy `clct_…`).

### macOS / Linux

```bash
export CACHELAYER_KEY="<your-token>"
```

To persist, add the same line to `~/.zshrc` or `~/.bashrc`.

If you launch VS Code from Dock or Spotlight on macOS:

```bash
launchctl setenv CACHELAYER_KEY '<your-token>'
```

### Windows (PowerShell)

```powershell
[Environment]::SetEnvironmentVariable("CACHELAYER_KEY", "<your-token>", "User")
```

## 4. Restart VS Code

Fully quit and reopen VS Code.
