[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("settle", "market-backup", "trigger", "query")]
    [string]$Stage
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$installRoot = "C:\hydra-live"
$envFile = Join-Path $installRoot "config\hydra-live.env"
$runner = Join-Path $installRoot "bin\Run-HydraLive.ps1"
$pythonExe = "C:\parttime\annaconda\envs\py311_qmt\python.exe"
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
$today = Get-Date -Format "yyyyMMdd"
try {
    switch ($Stage) {
        "settle" { $output = @(& $runner -Command settle -Date $today -PythonExe $pythonExe 2>&1) | Out-String }
        "market-backup" {
            if ([string]::IsNullOrWhiteSpace($env:HYDRA_LIVE_DATA_BACKUP_API_KEY)) { throw "HYDRA_LIVE_DATA_BACKUP_API_KEY is not configured" }
            $script = Join-Path $installRoot "scripts\hydra_live_market_backup.py"
            $lines = @(& $pythonExe $script 2>&1); $exitCode = $LASTEXITCODE; $output = $lines | Out-String
            if ($exitCode -ne 0) { throw "market backup returned a non-zero exit code" }
        }
        "trigger" {
            $script = Join-Path $env:HYDRA_LIVE_CODE_DIR "client\trigger_pipeline.py"
            $lines = @(& $pythonExe $script --live 2>&1); $exitCode = $LASTEXITCODE; $output = $lines | Out-String
            if ($exitCode -ne 0) { throw "live trigger returned a non-zero exit code" }
        }
        "query" { $output = @(& $runner -Command query -Date (Get-NextTradingDate) -PythonExe $pythonExe 2>&1) | Out-String }
    }
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) $Stage succeeded`n$output"
    Send-WeComNotification "[Hydra live] $Stage completed for $today."
} catch {
    $message = "$Stage failed: $($_.Exception.Message)"
    Add-Content -LiteralPath $logFile -Value "$(Get-Date -Format o) $message"
    Send-WeComNotification "[Hydra live] $message" $true
    throw
}
