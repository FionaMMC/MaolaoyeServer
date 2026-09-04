[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^\d{8}$")]
    [string]$TradeDate,
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$installRoot = "C:\hydra-live"
$runner = Join-Path $installRoot "bin\Run-HydraLive.ps1"
$pythonExe = if ($env:HYDRA_LIVE_PYTHON) { $env:HYDRA_LIVE_PYTHON } else { "python" }
$logFile = Join-Path $installRoot "logs\hydra-live-submit-$TradeDate.log"

function Send-WeComNotification([string]$Message) {
    if ([string]::IsNullOrWhiteSpace($env:HYDRA_LIVE_WECHAT_WEBHOOK)) { return }
    try {
        $body = @{ msgtype = "text"; text = @{ content = $Message } } |
            ConvertTo-Json -Compress -Depth 4
        Invoke-RestMethod -Method Post -Uri $env:HYDRA_LIVE_WECHAT_WEBHOOK `
            -ContentType "application/json; charset=utf-8" -Body $body | Out-Null
    } catch { Write-Warning "Enterprise WeChat notification failed: $($_.Exception.Message)" }
}

$phase = "preflight"
try {
    # Run-HydraLive throws on command failure. Do not inspect $LASTEXITCODE
    # after formatting a PowerShell pipeline: it is not the runner result.
    $preflightLines = @(& $runner -Command preflight -Date $TradeDate -PythonExe $pythonExe 2>&1)
    $preflightOutput = $preflightLines | Out-String
    if ($preflightOutput -notmatch '"status"\s*:\s*"READY_FOR_OFFLINE_SUBMIT"') {
        throw "Preflight did not return READY_FOR_OFFLINE_SUBMIT."
    }
    if ($PreflightOnly) {
        Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) preflight succeeded; submit intentionally skipped`n$preflightOutput"
        Send-WeComNotification "[Hydra live] $TradeDate preflight passed; submit intentionally skipped."
        exit 0
    }
    $phase = "submit"
    $submitLines = @(& $runner -Command submit -Date $TradeDate -PythonExe $pythonExe 2>&1)
    $submitOutput = $submitLines | Out-String
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) submit succeeded`n$submitOutput"
    Send-WeComNotification "[Hydra live] $TradeDate submit completed; review the 15:10 order-result notification."
} catch {
    $message = "$phase failed: $($_.Exception.Message)"
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) $message"
    Send-WeComNotification "[Hydra live] $TradeDate automatic submit was not completed. $message"
    throw
}
