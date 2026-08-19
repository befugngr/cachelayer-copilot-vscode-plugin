# CacheLayer Managed Keys for GitHub Copilot

https://cachelayer.org/

Install the VS Code plugin, add your CacheLayer connect token, and restart.

This repo is for managed keys only (`clct_…` as `CACHELAYER_KEY`).  
There is no token popup on install. Hooks and MCP both use `CACHELAYER_KEY`.  
Personal API keys: https://cachelayer.org/integrations/github-copilot

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

Use a connect token from https://cachelayer.org/ (starts with `clct_`).

### macOS / Linux

```bash
export CACHELAYER_KEY="clct_<your-token>"
```

To persist, add the same line to `~/.zshrc` or `~/.bashrc`.

If you launch VS Code from Dock or Spotlight on macOS:

```bash
launchctl setenv CACHELAYER_KEY 'clct_<your-token>'
```

### Windows (PowerShell)

```powershell
[Environment]::SetEnvironmentVariable("CACHELAYER_KEY", "clct_<your-token>", "User")
```

## 4. Restart VS Code

Fully quit and reopen VS Code.
