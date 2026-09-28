# CacheLayer for GitHub Copilot

https://cachelayer.org/

CacheLayer Agent OS sits in front of the LLM: it clears the agent’s memory and only gives it what the current step needs. This plugin connects your editor (managed keys, hooks, and MCP).

Personal / BYOK: https://cachelayer.org/integrations/github-copilot

## Agent OS (LLM traffic)

Point the model at CacheLayer Agent OS so it clears memory and only gives the agent what the current step needs:

```bash
export OPENAI_BASE_URL="https://api.cachelayer.org/cl-gate/v1"
export OPENAI_API_KEY="$CACHELAYER_KEY"
# Optional Anthropic-shaped clients:
# export ANTHROPIC_BASE_URL="https://api.cachelayer.org/cl-gate"
# export ANTHROPIC_API_KEY="$CACHELAYER_KEY"
```

Hooks and MCP stay on `https://api.cachelayer.org` (unchanged).

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
