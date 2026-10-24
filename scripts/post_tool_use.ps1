# Off unless CACHELAYER_SAVE=1 so a completed tool is not followed by a save wait.
$ErrorActionPreference = 'SilentlyContinue'
Write-Output '{"continue":true,"hookSpecificOutput":{"hookEventName":"PostToolUse"}}'
if ($env:CACHELAYER_SAVE -ne '1') { exit 0 }

$Url = if ($env:CACHELAYER_POST_HOOK_URL) { $env:CACHELAYER_POST_HOOK_URL } else { 'https://api.cachelayer.org/hooks/post-tool-use' }
$Token = if ($env:CACHELAYER_KEY) { $env:CACHELAYER_KEY } elseif ($env:CACHELAYER_TOKEN) { $env:CACHELAYER_TOKEN } elseif ($env:CACHELAYER_CONNECT_TOKEN) { $env:CACHELAYER_CONNECT_TOKEN } else { '' }
$InputJson = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($Token) -or [string]::IsNullOrWhiteSpace($InputJson)) { exit 0 }
$Filter = Join-Path $PSScriptRoot 'filter_hook_payload.py'
$PyCmd = $null
$PyArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $PyCmd = 'py'; $PyArgs = @('-3', $Filter) }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $PyCmd = 'python'; $PyArgs = @($Filter) }
else { exit 0 }
$InputJson = $InputJson | & $PyCmd @PyArgs
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($InputJson)) { exit 0 }
$tmp = Join-Path $env:TEMP ("cachelayer-save-" + [guid]::NewGuid().ToString() + ".json")
Set-Content -Path $tmp -Value $InputJson -Encoding utf8
$args = @(
  "-sS", "--max-time", "2", "-X", "POST", $Url,
  "-H", "Content-Type: application/json",
  "-H", "Authorization: Bearer $Token",
  "--data-binary", "@$tmp"
)
if ($env:CACHELAYER_FLOW_ID) {
  $args += @("-H", "x-cachelayer-flow: $($env:CACHELAYER_FLOW_ID)")
}
Start-Process -WindowStyle Hidden -FilePath "curl.exe" -ArgumentList $args | Out-Null
exit 0
