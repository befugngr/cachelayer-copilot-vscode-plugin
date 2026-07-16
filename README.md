# CacheLayer for GitHub Copilot in VS Code

Cache completed agent steps and reuse them in future tasks.

## Requirements

- VS Code with GitHub Copilot agent mode
- A CacheLayer connect token (`clct_...`) from https://cachelayer.org/

## Install

1. Enable plugins in VS Code settings:

   ```json
   "chat.plugins.enabled": true
   ```

2. Open the Command Palette.
3. Run **Chat: Install Plugin From Source**.
4. Paste:

   ```text
   https://github.com/befugngr/cachelayer-copilot-vscode-plugin
   ```

5. Reload VS Code.

## Authenticate the MCP server

1. Open the Command Palette.
2. Run **GitHub Copilot: List MCP Servers**.
3. Select `cachelayer`.
4. Click **Show Configuration**.
5. Make sure the authorization header is:

   ```json
   "Authorization": "Bearer ${input:cachelayer-token}"
   ```

6. Start or restart the server.
7. When prompted, paste your `clct_...` token.

## Authenticate the hook

Use the same `clct_...` token. The hook reads it from `CACHELAYER_KEY`.

### Windows

Run this in PowerShell:

```powershell
[Environment]::SetEnvironmentVariable("CACHELAYER_KEY", "clct_<your-token>", "User")
```

Close every VS Code window, then reopen VS Code.

### macOS

Run this in Terminal:

```bash
echo 'export CACHELAYER_KEY="clct_<your-token>"' >> ~/.zshrc
source ~/.zshrc
code
```

### Linux

For Bash:

```bash
echo 'export CACHELAYER_KEY="clct_<your-token>"' >> ~/.bashrc
source ~/.bashrc
code
```

For Zsh, use `~/.zshrc` instead.

## Verify

- **GitHub Copilot: List MCP Servers** shows `cachelayer` as running.
- **Configure Tools** shows `lookup_step`, `save_step`, `check_conflict`, and `run_status`.
- **Configure Skills** shows `cachelayer-tools`.
