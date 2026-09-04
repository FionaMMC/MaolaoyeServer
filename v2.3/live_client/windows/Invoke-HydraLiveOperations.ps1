[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("cancel-open", "settle-close", "market-backup", "retry", "query-preflight")]
    [string]$Stage
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$installRoot = "C:\hydra-live"
$envFile = Join-Path $installRoot "config\hydra-live.env"
$runner = Join-Path $installRoot "bin\Run-HydraLive.ps1"
$pythonExe = $null
$logFile = Join-Path $installRoot "logs\hydra-live-$Stage.log"

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

function Send-WeComNotification([string]$Message, [bool]$Alert = $false) {
    if ([string]::IsNullOrWhiteSpace($env:HYDRA_LIVE_WECHAT_WEBHOOK)) { return }
    try {
        $prefix = if ($Alert) { "[报警] " } else { "" }
        $body = @{ msgtype = "text"; text = @{ content = "$prefix$Message" } } | ConvertTo-Json -Compress -Depth 4
        Invoke-RestMethod -Method Post -Uri $env:HYDRA_LIVE_WECHAT_WEBHOOK -ContentType "application/json; charset=utf-8" -Body $body | Out-Null
    } catch { Write-Warning "WeCom notification failed: $($_.Exception.Message)" }
}

function Get-NextTradingDate {
    $code = @'
from datetime import datetime, timedelta
import os
from xtquant import xtdata
xtdata.data_dir = os.environ["HYDRA_LIVE_QMT_USERDATA_DIR"]
today = datetime.now().strftime("%Y%m%d")
dates = xtdata.get_trading_calendar("SH", start_time=(datetime.now()-timedelta(days=30)).strftime("%Y%m%d"), end_time=(datetime.now()+timedelta(days=14)).strftime("%Y%m%d"))
print(next(date for date in dates if date > today))
'@
    $lines = @($code | & $pythonExe -c "import sys; exec(sys.stdin.read())")
    $exitCode = $LASTEXITCODE
    $date = $lines | Select-Object -Last 1
    if ($exitCode -ne 0 -or $date -notmatch '^\d{8}$') { throw "Unable to determine next QMT trading date" }
    return $date.Trim()
}

Import-HydraPrivateEnvironment
$requiredPython = $env:HYDRA_LIVE_PYTHON
if ([string]::IsNullOrWhiteSpace($requiredPython) -or -not (Test-Path -LiteralPath $requiredPython -PathType Leaf)) {
    throw "HYDRA_LIVE_PYTHON must be an existing absolute executable path"
}
$pythonExe = $requiredPython
$today = Get-Date -Format "yyyyMMdd"
try {
    switch ($Stage) {
        "cancel-open" {
            $output = @(& $runner -Command cancel-open -Date $today -PythonExe $pythonExe 2>&1) | Out-String
            if ($output -notmatch '"status"\s*:\s*"(CANCEL_REQUESTED|NO_ACTIVE_ORDERS|NO_ORDERS)"') {
                throw "cancel-open returned no complete cancellation-request receipt"
            }
        }
        "settle-close" {
            $output = @(& $runner -Command settle-close -Date $today -PythonExe $pythonExe 2>&1) | Out-String
            if ($output -notmatch '"status"\s*:\s*"(ATTEMPT_CLOSED|NO_ORDERS)"') { throw "settle-close returned no terminal receipt" }
        }
        "market-backup" {
            if ([string]::IsNullOrWhiteSpace($env:HYDRA_LIVE_DATA_BACKUP_API_KEY)) { throw "HYDRA_LIVE_DATA_BACKUP_API_KEY is not configured" }
            $script = Join-Path $installRoot "scripts\hydra_live_market_backup.py"
            $lines = @(& $pythonExe $script 2>&1); $exitCode = $LASTEXITCODE; $output = $lines | Out-String
            if ($exitCode -ne 0) { throw "market backup returned a non-zero exit code" }
            if ($output -notmatch '"status"\s*:\s*"(UPLOADED|SKIPPED_NON_TRADING)"') { throw "market backup returned no success receipt" }
        }
        "retry" {
            $nextDate = Get-NextTradingDate
            $output = @(& $runner -Command retry -Date $today -NextDate $nextDate -PythonExe $pythonExe 2>&1) | Out-String
            if ($output -notmatch '"status"\s*:\s*"(RETRY_STAGED|NO_RESIDUAL|NO_ATTEMPT|ALREADY_STAGED)"') { throw "retry returned no terminal receipt" }
        }
        "query-preflight" {
            $nextDate = Get-NextTradingDate
            $queryOutput = @(& $runner -Command query -Date $nextDate -PythonExe $pythonExe 2>&1) | Out-String
            if ($queryOutput -match '"status"\s*:\s*"NO_ORDERS"') {
                $output = "query:`n$queryOutput`npreflight: skipped because server returned NO_ORDERS"
            } else {
                if ($queryOutput -notmatch '"status"\s*:\s*"(FETCHED|ALREADY_FETCHED)"') { throw "query did not freeze a batch" }
                $preflightOutput = @(& $runner -Command preflight -Date $nextDate -PythonExe $pythonExe 2>&1) | Out-String
                if ($preflightOutput -notmatch '"status"\s*:\s*"READY_FOR_OFFLINE_SUBMIT"') { throw "preflight did not return READY_FOR_OFFLINE_SUBMIT" }
                $registerSubmit = Join-Path $installRoot "scripts\Register-HydraLiveSubmitTask.ps1"
                if (-not (Test-Path -LiteralPath $registerSubmit -PathType Leaf)) { throw "submit task registrar is missing" }
                $taskOutput = @(& $registerSubmit -TradeDate $nextDate 2>&1) | Out-String
                if ($taskOutput -notmatch '"status"\s*:\s*"(REGISTERED|ALREADY_REGISTERED)"') { throw "09:10 submit task was not registered" }
                $output = "query:`n$queryOutput`npreflight:`n$preflightOutput`nsubmit-task:`n$taskOutput"
            }
        }
    }
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) $Stage succeeded`n$output"
    if ($Stage -eq "cancel-open") {
        Send-WeComNotification "[Hydra live] cancel-open request phase completed for $today; final broker status remains pending until the 16:05 settlement task."
    }
    else {
        Send-WeComNotification "[Hydra live] $Stage completed for $today."
    }
} catch {
    $message = "$Stage failed: $($_.Exception.Message)"
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) $message"
    Send-WeComNotification "[Hydra live] $message" $true
    throw
}
