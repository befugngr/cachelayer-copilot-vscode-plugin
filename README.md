# CacheLayer Managed Keys for GitHub Copilot

https://cachelayer.org/

Install the VS Code plugin, add your CacheLayer connect token, and restart.

This repo is for managed keys only (`clct_…` as `CACHELAYER_KEY`).  
There is no token popup on install. Hooks and MCP both use `CACHELAYER_KEY`.  
Personal API keys: https://cachelayer.org/integrations/github-copilot

## 1. Turn on chat plugins in VS Code settings

`settings.json`:

```json
"chat.plugins.enabled": true
```

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

## Optional local loop-cutters

The plugin also bundles a local, Python 3 stdlib-only MCP server alongside the managed-keys cache MCP. It provides `verify_edit` (CRITIC), `run_affected_tests` (TIA), and `debug_failure` for compact one-call feedback in the current workspace. These tools are optional: missing project analyzers degrade gracefully with install guidance, while the remote `cachelayer` server and `CACHELAYER_KEY` flow remain unchanged.

For richer selection and diagnosis, projects may optionally install `pytest-testmon`/Scalpel, TypeScript/ESLint/Jest, or Java tooling such as JaCoCo, Ekstazi, Joern, Flacoco, and GZoltar.
