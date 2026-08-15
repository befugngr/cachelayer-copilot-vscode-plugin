# CacheLayer PostToolUse hook for GitHub Copilot / VS Code (Windows, fail-open).
$ErrorActionPreference = 'SilentlyContinue'
$Url = if ($env:CACHELAYER_POST_HOOK_URL) { $env:CACHELAYER_POST_HOOK_URL } else { 'https://api.cachelayer.org/hooks/post-tool-use' }
$Token = if ($env:CACHELAYER_KEY) { $env:CACHELAYER_KEY } elseif ($env:CACHELAYER_TOKEN) { $env:CACHELAYER_TOKEN } elseif ($env:CACHELAYER_CONNECT_TOKEN) { $env:CACHELAYER_CONNECT_TOKEN } else { '' }
$TimeoutSec = 5
if ($env:CACHELAYER_HOOK_TIMEOUT_S) { [void][int]::TryParse($env:CACHELAYER_HOOK_TIMEOUT_S, [ref]$TimeoutSec) }

$InputJson = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($Token) -or $InputJson.Length -gt 262144) {
  Write-Output '{"continue":true}'
  exit 0
}
$Filter = Join-Path $PSScriptRoot 'filter_hook_payload.py'
if (Get-Command py -ErrorAction SilentlyContinue) {
  $InputJson = $InputJson | py -3 $Filter
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
  $InputJson = $InputJson | python3 $Filter
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $InputJson = $InputJson | python $Filter
} else {
  Write-Output '{"continue":true}'
  exit 0
}
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($InputJson)) {
  Write-Output '{"continue":true}'
  exit 0
}
try {
  Invoke-RestMethod -Method Post -Uri $Url -Headers @{
    'Content-Type' = 'application/json'
    'Authorization' = "Bearer $Token"
  } -Body $InputJson -TimeoutSec $TimeoutSec | Out-Null
} catch {}
Write-Output '{"continue":true}'
exit 0
