[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("cancel-open", "settle-close", "market-backup", "publish-execution", "retry", "query-preflight", "settle", "trigger", "query")]
    [string]$Stage
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$Stage = switch ($Stage) {
    "settle" { "settle-close" }
    "trigger" { "retry" }
    "query" { "query-preflight" }
    default { $Stage }
}
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
        $bodyBytes = [Text.Encoding]::UTF8.GetBytes($body)
        Invoke-RestMethod -Method Post -Uri $env:HYDRA_LIVE_WECHAT_WEBHOOK -ContentType "application/json; charset=utf-8" -Body $bodyBytes | Out-Null
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
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $lines = @($code | & $pythonExe -c "import sys; exec(sys.stdin.read())")
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
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
$operationPending = $false
$operationWaitingDate = $false
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
            $operationPending = $output -match '"status"\s*:\s*"(WAITING_FOR_BROKER|WAITING_RECONCILIATION)"'
            if (-not $operationPending -and $output -notmatch '"status"\s*:\s*"(ATTEMPT_CLOSED|NO_ORDERS)"') { throw "settle-close returned no recognized receipt" }
        }
        "market-backup" {
            if ([string]::IsNullOrWhiteSpace($env:HYDRA_LIVE_DATA_BACKUP_API_KEY)) { throw "HYDRA_LIVE_DATA_BACKUP_API_KEY is not configured" }
            $script = Join-Path $installRoot "scripts\hydra_live_market_backup.py"
            # Windows PowerShell 5.1 represents native stderr as ErrorRecord.
            # Logging is not failure: preserve the native exit code immediately.
            $previousErrorActionPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                $lines = @(& $pythonExe $script 2>&1)
                $exitCode = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $previousErrorActionPreference
            }
            $output = $lines | Out-String
            if ($exitCode -ne 0) { throw "market backup returned a non-zero exit code" }
            if ($output -notmatch '"status"\s*:\s*"(UPLOADED|SKIPPED_NON_TRADING)"') { throw "market backup returned no success receipt" }
        }
        "publish-execution" {
            $output = @(& $runner -Command publish-execution -Date $today -PythonExe $pythonExe 2>&1) | Out-String
            if ($output -notmatch '"status"\s*:\s*"(EXECUTION_DATA_PUBLISHED|SKIPPED_NON_TRADING)"') { throw "execution publication returned no receipt" }
        }
        "retry" {
            $publicationOutput = @(& $runner -Command publish-execution -Date $today -PythonExe $pythonExe 2>&1) | Out-String
            if ($publicationOutput -notmatch '"status"\s*:\s*"(EXECUTION_DATA_PUBLISHED|SKIPPED_NON_TRADING)"') { throw "execution publication returned no receipt" }
            if ($publicationOutput -match '"status"\s*:\s*"SKIPPED_NON_TRADING"') {
                $operationWaitingDate = $true; $output = $publicationOutput; break
            }
            # Re-observe any late broker terminal response; this is not an order retry.
            $closeOutput = @(& $runner -Command settle-close -Date $today -PythonExe $pythonExe 2>&1) | Out-String
            if ($closeOutput -match '"status"\s*:\s*"(WAITING_FOR_BROKER|WAITING_RECONCILIATION)"') {
                $operationPending = $true; $output = $closeOutput; break
            }
            if ($closeOutput -notmatch '"status"\s*:\s*"(ATTEMPT_CLOSED|NO_ORDERS)"') { throw "settle-close returned no recognized receipt" }
            $output = @(& $runner -Command advance -Date $today -PythonExe $pythonExe 2>&1) | Out-String
            if ($output -notmatch '"status"\s*:\s*"(EXECUTION_ADVANCED|NO_PENDING_EXECUTION|WAITING_EXECUTION_DATE|WAITING_EXECUTION_DATA|WAITING_RECONCILIATION)"') { throw "server execution advance returned no receipt" }
            $operationPending = $output -match '"status"\s*:\s*"(WAITING_EXECUTION_DATA|WAITING_RECONCILIATION)"'
            $operationWaitingDate = $output -match '"status"\s*:\s*"WAITING_EXECUTION_DATE"'
            $output = "close:`n$closeOutput`nadvance:`n$output"
        }
        "query-preflight" {
            # A delayed 15:30 publication is recoverable at 18:00, with fresh data.
            $publicationOutput = @(& $runner -Command publish-execution -Date $today -PythonExe $pythonExe 2>&1) | Out-String
            if ($publicationOutput -notmatch '"status"\s*:\s*"(EXECUTION_DATA_PUBLISHED|SKIPPED_NON_TRADING)"') { throw "execution publication returned no receipt" }
            if ($publicationOutput -match '"status"\s*:\s*"SKIPPED_NON_TRADING"') {
                $operationWaitingDate = $true; $output = $publicationOutput; break
            }
            $closeOutput = @(& $runner -Command settle-close -Date $today -PythonExe $pythonExe 2>&1) | Out-String
            if ($closeOutput -match '"status"\s*:\s*"(WAITING_FOR_BROKER|WAITING_RECONCILIATION)"') {
                $operationPending = $true; $output = $closeOutput; break
            }
            if ($closeOutput -notmatch '"status"\s*:\s*"(ATTEMPT_CLOSED|NO_ORDERS)"') { throw "settle-close returned no recognized receipt" }
            $advanceOutput = @(& $runner -Command advance -Date $today -PythonExe $pythonExe 2>&1) | Out-String
            if ($advanceOutput -notmatch '"status"\s*:\s*"(EXECUTION_ADVANCED|NO_PENDING_EXECUTION|WAITING_EXECUTION_DATE|WAITING_EXECUTION_DATA|WAITING_RECONCILIATION)"') { throw "server execution advance returned no receipt" }
            $operationPending = $advanceOutput -match '"status"\s*:\s*"(WAITING_EXECUTION_DATA|WAITING_RECONCILIATION)"'
            $operationWaitingDate = $advanceOutput -match '"status"\s*:\s*"WAITING_EXECUTION_DATE"'
            if ($operationPending -or $operationWaitingDate) {
                $output = $advanceOutput; break # Do not fetch/register a stale batch while waiting.
            }
            $nextDate = Get-NextTradingDate
            $queryOutput = @(& $runner -Command query -Date $nextDate -PythonExe $pythonExe 2>&1) | Out-String
            if ($queryOutput -match '"status"\s*:\s*"NO_ORDERS"') {
                $output = "advance:`n$advanceOutput`nquery:`n$queryOutput`npreflight: skipped because server returned NO_ORDERS"
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
    $operationState = if ($operationPending) { "pending evidence/data" } elseif ($operationWaitingDate) { "waiting for eligible trading pair" } else { "succeeded" }
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) $Stage $operationState`n$output" -Encoding UTF8
    if ($operationPending) {
        Send-WeComNotification "[Hydra live] $Stage is waiting for broker/reconciliation evidence or fresh execution data for $today. No new submit task was registered by this run. Review $logFile; previously frozen tasks require separate review." $true
    }
    elseif ($operationWaitingDate) {
        Send-WeComNotification "[Hydra live] $Stage: normal calendar wait for $today. No stale-price batch or new submit task was created."
    }
    elseif ($Stage -eq "cancel-open") {
        Send-WeComNotification "[Hydra live] cancel-open request phase completed for $today; cumulative fills and 15:00 expiry are observed at 15:10, then again before server execution advance."
    }
    else {
        Send-WeComNotification "[Hydra live] $Stage completed for $today."
    }
} catch {
    $message = "$Stage failed: $($_.Exception.Message)"
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) $message" -Encoding UTF8
    Send-WeComNotification "[Hydra live] $message" $true
    throw
}
