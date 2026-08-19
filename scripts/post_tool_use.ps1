# CacheLayer PostToolUse save for GitHub Copilot / VS Code (Windows, visible).
$ErrorActionPreference = 'SilentlyContinue'
$Url = if ($env:CACHELAYER_POST_HOOK_URL) { $env:CACHELAYER_POST_HOOK_URL } else { 'https://api.cachelayer.org/hooks/post-tool-use' }
$Token = if ($env:CACHELAYER_KEY) { $env:CACHELAYER_KEY } elseif ($env:CACHELAYER_TOKEN) { $env:CACHELAYER_TOKEN } elseif ($env:CACHELAYER_CONNECT_TOKEN) { $env:CACHELAYER_CONNECT_TOKEN } else { '' }
$TimeoutSec = 5
if ($env:CACHELAYER_HOOK_TIMEOUT_S) { [void][int]::TryParse($env:CACHELAYER_HOOK_TIMEOUT_S, [ref]$TimeoutSec) }

function Write-Note([string]$Msg) {
  Write-Output (@{
    continue = $true
    hookSpecificOutput = @{
      hookEventName = 'PostToolUse'
      additionalContext = "CacheLayer save: $Msg"
    }
  } | ConvertTo-Json -Compress -Depth 6)
}

$InputJson = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($Token) -or $InputJson.Length -gt 262144) {
  Write-Note 'no_token_or_too_large'
  exit 0
}
$Filter = Join-Path $PSScriptRoot 'filter_hook_payload.py'
$PyCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $PyCmd = 'py'; $PyArgs = @('-3', $Filter) }
elseif (Get-Command python3 -ErrorAction SilentlyContinue) { $PyCmd = 'python3'; $PyArgs = @($Filter) }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $PyCmd = 'python'; $PyArgs = @($Filter) }
else {
  Write-Note 'no_python'
  exit 0
}
$InputJson = $InputJson | & $PyCmd @PyArgs
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($InputJson)) {
  Write-Note 'skipped_non_read_or_secret'
  exit 0
}
try {
  $resp = Invoke-RestMethod -Method Post -Uri $Url -Headers @{
    'Content-Type' = 'application/json'
    'Authorization' = "Bearer $Token"
  } -Body $InputJson -TimeoutSec $TimeoutSec
  if ($resp.stored) {
    Write-Note ("SAVED " + [string]($resp.description -as [string]))
  } else {
    Write-Note ("NOT STORED: " + [string]($resp.reason -as [string]))
  }
} catch {
  Write-Note 'save_unreachable'
}
exit 0
