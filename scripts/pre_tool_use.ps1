# CacheLayer PreToolUse hook for GitHub Copilot / VS Code (Windows, fail-open, visible).
$ErrorActionPreference = 'SilentlyContinue'
$Url = if ($env:CACHELAYER_HOOK_URL) { $env:CACHELAYER_HOOK_URL } else { 'https://api.cachelayer.org/hooks/pre-tool-use' }
$Token = if ($env:CACHELAYER_KEY) { $env:CACHELAYER_KEY } elseif ($env:CACHELAYER_TOKEN) { $env:CACHELAYER_TOKEN } elseif ($env:CACHELAYER_CONNECT_TOKEN) { $env:CACHELAYER_CONNECT_TOKEN } else { '' }
$TimeoutSec = 5
if ($env:CACHELAYER_HOOK_TIMEOUT_S) { [void][int]::TryParse($env:CACHELAYER_HOOK_TIMEOUT_S, [ref]$TimeoutSec) }

function Write-Allow([string]$Reason) {
  Write-Output (@{
    continue = $true
    hookSpecificOutput = @{
      hookEventName = 'PreToolUse'
      permissionDecision = 'allow'
      permissionDecisionReason = $Reason
      additionalContext = "CacheLayer lookup: $Reason"
    }
  } | ConvertTo-Json -Compress -Depth 8)
}

$InputJson = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($InputJson) -or $InputJson.Length -gt 262144) {
  Write-Allow 'empty_or_too_large'
  exit 0
}
if ([string]::IsNullOrWhiteSpace($Token)) {
  Write-Allow 'no_token'
  exit 0
}

$Filter = Join-Path $PSScriptRoot 'filter_hook_payload.py'
$PyCmd = $null
$PyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $PyCmd = 'py'; $PyArgs = @('-3', $Filter) }
elseif (Get-Command python3 -ErrorAction SilentlyContinue) { $PyCmd = 'python3'; $PyArgs = @($Filter) }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $PyCmd = 'python'; $PyArgs = @($Filter) }
else {
  Write-Allow 'no_python'
  exit 0
}
$InputJson = $InputJson | & $PyCmd @PyArgs
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($InputJson)) {
  Write-Allow 'skipped_non_read_or_secret'
  exit 0
}

try {
  $resp = Invoke-RestMethod -Method Post -Uri $Url -Headers @{
    'Content-Type' = 'application/json'
    'Authorization' = "Bearer $Token"
  } -Body $InputJson -TimeoutSec $TimeoutSec
  $hso = @{
    hookEventName = 'PreToolUse'
    permissionDecision = 'allow'
    permissionDecisionReason = 'cache_miss'
  }
  if ($resp.hookSpecificOutput) { $hso = @{} + $resp.hookSpecificOutput }
  $cl = $resp.cachelayer
  $err = $resp.error
  if (-not $err -and $cl) { $err = $cl.error }
  $hit = [bool]$resp.hit
  if (-not $hit -and $cl) { $hit = [bool]$cl.hit }
  $result = $resp.result
  if ($null -eq $result -and $cl) { $result = $cl.result }
  if ($err) {
    $hso.permissionDecision = 'allow'
    $hso.permissionDecisionReason = [string]$err
    $hso.additionalContext = "CacheLayer lookup error: $err"
  } elseif ($hit -and $null -ne $result) {
    $rendered = if ($result -is [string]) { $result } else { ($result | ConvertTo-Json -Compress -Depth 20) }
    $hso.permissionDecision = 'deny'
    $hso.permissionDecisionReason = 'cache_hit'
    $hso.additionalContext = "CacheLayer HIT. Use this cached result and do not re-read:`n$rendered"
  } else {
    $hso.permissionDecision = 'allow'
    $hso.permissionDecisionReason = 'cache_miss'
    $hso.additionalContext = 'CacheLayer MISS. Native read will run; post-hook will try to save.'
  }
  $out = @{ continue = $true; hookSpecificOutput = $hso }
  if ($cl) { $out.cachelayer = $cl }
  Write-Output ($out | ConvertTo-Json -Compress -Depth 30)
  exit 0
} catch {
  Write-Allow "lookup_unreachable"
  exit 0
}
