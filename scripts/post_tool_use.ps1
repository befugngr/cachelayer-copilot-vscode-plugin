# CacheLayer PostToolUse save for GitHub Copilot / VS Code (Windows, visible).
$ErrorActionPreference = 'SilentlyContinue'
$Url = if ($env:CACHELAYER_POST_HOOK_URL) { $env:CACHELAYER_POST_HOOK_URL } else { 'https://api.cachelayer.org/hooks/post-tool-use' }
$Token = if ($env:CACHELAYER_KEY) { $env:CACHELAYER_KEY } elseif ($env:CACHELAYER_TOKEN) { $env:CACHELAYER_TOKEN } elseif ($env:CACHELAYER_CONNECT_TOKEN) { $env:CACHELAYER_CONNECT_TOKEN } else { '' }
$TimeoutSec = 5
if ($env:CACHELAYER_HOOK_TIMEOUT_S) { [void][int]::TryParse($env:CACHELAYER_HOOK_TIMEOUT_S, [ref]$TimeoutSec) }

function Write-Done {
  Write-Output '{"continue":true,"hookSpecificOutput":{"hookEventName":"PostToolUse"}}'
}

$InputJson = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($Token) -or $InputJson.Length -gt 262144) {
  Write-Done
  exit 0
}
$Filter = Join-Path $PSScriptRoot 'filter_hook_payload.py'
$PyCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $PyCmd = 'py'; $PyArgs = @('-3', $Filter) }
elseif (Get-Command python3 -ErrorAction SilentlyContinue) { $PyCmd = 'python3'; $PyArgs = @($Filter) }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $PyCmd = 'python'; $PyArgs = @($Filter) }
else {
  Write-Done
  exit 0
}
$InputJson = $InputJson | & $PyCmd @PyArgs
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($InputJson)) {
  Write-Done
  exit 0
}
$tmp = [System.IO.Path]::GetTempFileName()
Set-Content -Path $tmp -Value $InputJson -Encoding utf8
$flow = @()
if ($env:CACHELAYER_FLOW_ID) { $flow = @('-H', "x-cachelayer-flow: $($env:CACHELAYER_FLOW_ID)") }
Start-Process -FilePath curl.exe -ArgumentList (@('-sS','--max-time','5','-X','POST',$Url,'-H','Content-Type: application/json','-H',"Authorization: Bearer $Token") + $flow + @('--data-binary',"@$tmp")) -WindowStyle Hidden | Out-Null
Write-Done
exit 0
