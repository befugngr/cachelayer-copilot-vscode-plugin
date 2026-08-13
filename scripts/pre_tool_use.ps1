# CacheLayer PreToolUse hook for GitHub Copilot / VS Code (Windows, fail-open).
# Env: CACHELAYER_KEY (legacy CACHELAYER_TOKEN / CACHELAYER_CONNECT_TOKEN accepted)
$ErrorActionPreference = 'SilentlyContinue'
$Url = if ($env:CACHELAYER_HOOK_URL) { $env:CACHELAYER_HOOK_URL } else { 'https://api.cachelayer.org/hooks/pre-tool-use' }
$Token = if ($env:CACHELAYER_KEY) { $env:CACHELAYER_KEY } elseif ($env:CACHELAYER_TOKEN) { $env:CACHELAYER_TOKEN } elseif ($env:CACHELAYER_CONNECT_TOKEN) { $env:CACHELAYER_CONNECT_TOKEN } else { '' }
$TimeoutSec = 5
if ($env:CACHELAYER_HOOK_TIMEOUT_S) { [void][int]::TryParse($env:CACHELAYER_HOOK_TIMEOUT_S, [ref]$TimeoutSec) }

$InputJson = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($InputJson)) {
  Write-Output '{"continue":true}'
  exit 0
}

if ([string]::IsNullOrWhiteSpace($Token)) {
  Write-Output '{"continue":true,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"cachelayer_no_token"}}'
  exit 0
}

try {
  $headers = @{
    'Content-Type'  = 'application/json'
    'Authorization' = "Bearer $Token"
  }
  $resp = Invoke-RestMethod -Method Post -Uri $Url -Headers $headers -Body $InputJson -TimeoutSec $TimeoutSec
  $hso = @{
    hookEventName         = 'PreToolUse'
    permissionDecision    = 'allow'
    permissionDecisionReason = 'cache_miss'
  }
  if ($resp.hookSpecificOutput) {
    $hso = $resp.hookSpecificOutput
  }
  if ($resp.cachelayer -and $resp.cachelayer.hit -and $null -ne $resp.cachelayer.result) {
    $rendered = if ($resp.cachelayer.result -is [string]) { $resp.cachelayer.result } else { ($resp.cachelayer.result | ConvertTo-Json -Compress -Depth 20) }
    if (-not $hso.additionalContext) {
      $hso | Add-Member -NotePropertyName additionalContext -NotePropertyValue ("CacheLayer reusable result for this step: " + $rendered) -Force
    }
    $hso.permissionDecisionReason = 'cache_hit'
  }
  $out = @{ continue = $true; hookSpecificOutput = $hso }
  if ($resp.cachelayer) { $out.cachelayer = $resp.cachelayer }
  Write-Output ($out | ConvertTo-Json -Compress -Depth 30)
  exit 0
} catch {
  Write-Output '{"continue":true}'
  exit 0
}
