<# Historical one-time task invoked by Hydra-Live-Submit-20260904-0910. #>
param(
    [switch]$NotifyOnly,
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$tradeDate = "20260904"
$installRoot = "C:\hydra-live"
$envFile = Join-Path $installRoot "config\hydra-live.env"
$runner = Join-Path $installRoot "bin\Run-HydraLive.ps1"
$pythonExe = "C:\parttime\annaconda\envs\py311_qmt\python.exe"
$logFile = Join-Path $installRoot "logs\hydra-live-submit-$tradeDate.log"

function Import-HydraPrivateEnvironment {
    foreach ($rawLine in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        if ($line -notmatch "^([A-Za-z_][A-Za-z0-9_]*)=(.*)$") { throw "Invalid private env entry" }
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($Matches[1], $value, "Process")
    }
}

function Send-WeComNotification([string]$Message) {
    if ([string]::IsNullOrWhiteSpace($env:HYDRA_LIVE_WECHAT_WEBHOOK)) { return }
    try {
        $body = @{ msgtype = "text"; text = @{ content = $Message } } | ConvertTo-Json -Compress -Depth 4
        Invoke-RestMethod -Method Post -Uri $env:HYDRA_LIVE_WECHAT_WEBHOOK -ContentType "application/json; charset=utf-8" -Body $body | Out-Null
    } catch { Write-Warning "Enterprise WeChat notification failed: $($_.Exception.Message)" }
}

Import-HydraPrivateEnvironment
if ($NotifyOnly) {
    Send-WeComNotification "[Hydra live] 2026-09-04 09:10 automatic submit task has been scheduled. It will preflight first and submit only if preflight passes."
    exit 0
}

$phase = "preflight"
try {
    # The corrected variant: runner failures throw; do not inspect
    # $LASTEXITCODE after output formatting.
    $preflightLines = @(& $runner -Command preflight -Date $tradeDate -PythonExe $pythonExe 2>&1)
    $preflightOutput = $preflightLines | Out-String
    if ($preflightOutput -notmatch '"status"\s*:\s*"READY_FOR_OFFLINE_SUBMIT"') { throw "Preflight did not return READY_FOR_OFFLINE_SUBMIT." }
    if ($PreflightOnly) {
        Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) preflight succeeded; submit intentionally skipped`n$preflightOutput"
        Send-WeComNotification "[Hydra live] 2026-09-04 preflight passed. Submit was intentionally skipped."
        exit 0
    }
    $phase = "submit"
    $submitLines = @(& $runner -Command submit -Date $tradeDate -PythonExe $pythonExe 2>&1)
    $submitOutput = $submitLines | Out-String
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) submit succeeded`n$submitOutput"
    Send-WeComNotification "[Hydra live] 2026-09-04 automatic submit completed. Please review the 15:10 order result notification for execution status."
} catch {
    $message = "$phase failed: $($_.Exception.Message)"
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) $message"
    Send-WeComNotification "[Hydra live] 2026-09-04 automatic submit was not completed. $message"
    throw
}
