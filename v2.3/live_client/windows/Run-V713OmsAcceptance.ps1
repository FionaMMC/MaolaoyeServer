[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$InstallRoot = "C:\hydra-live",
    [string]$EnvFile,
    [string]$PythonExe,
    [switch]$ConnectQmt,
    [ValidatePattern('^[0-9]{6}\.(SH|SZ|BJ)$')][string[]]$Symbols = @()
)

# Portable synthetic acceptance needs neither pytest nor a live broker. Optional
# QMT verification delegates exclusively to the read-only inspection script.
# This wrapper never invokes an agent step, doctor, registration or submit/cancel.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $EnvFile) { $EnvFile = Join-Path $InstallRoot "config\hydra-live.env" }
if (-not $PythonExe) {
    $entries = @(Get-Content -LiteralPath $EnvFile -Encoding UTF8 | Where-Object { $_.Trim() -match '^HYDRA_LIVE_PYTHON=' })
    if ($entries.Count -ne 1) { throw "Specify -PythonExe or configure one HYDRA_LIVE_PYTHON entry" }
    $PythonExe = ($entries[0].Trim() -split '=', 2)[1].Trim().Trim('"').Trim("'")
}
$inspector = Join-Path $PSScriptRoot "Inspect-V713SharedOms.ps1"
& $inspector -OutputDirectory $OutputDirectory -InstallRoot $InstallRoot -EnvFile $EnvFile `
    -PythonExe $PythonExe -ConnectQmt:$ConnectQmt -Symbols $Symbols
$inspectionPath = Join-Path $OutputDirectory "shared-oms-inspection.json"
$report = Get-Content -LiteralPath $inspectionPath -Raw -Encoding UTF8 | ConvertFrom-Json
$releaseRoot = Join-Path (Join-Path $InstallRoot "releases") $report.active_release
$gatePath = Join-Path $OutputDirectory "oms-offline-acceptance.json"
$resultPath = Join-Path $OutputDirectory "acceptance.json"
foreach ($path in @($gatePath, $resultPath)) {
    if (Test-Path -LiteralPath $path) { throw "Acceptance evidence already exists; use a new directory" }
}
# Native parser checks every shipped PowerShell script on the target Windows host.
foreach ($script in Get-ChildItem -LiteralPath (Join-Path $releaseRoot "live_client\windows") -Filter "*.ps1" -File) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($script.FullName, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -gt 0) { throw "Installed PowerShell syntax validation failed" }
}
$previousPythonPath = $env:PYTHONPATH
$previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
$previousPythonUtf8 = $env:PYTHONUTF8
$previousPythonIoEncoding = $env:PYTHONIOENCODING
$previousErrorActionPreference = $ErrorActionPreference
try {
    $env:PYTHONPATH = $releaseRoot
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $ErrorActionPreference = "Continue"
    $gateOutput = & $PythonExe -m live_client.oms_offline_acceptance 2>&1
    $gateExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorActionPreference
    if ($gateExitCode -ne 0) { throw "Shared-OMS portable offline gate failed" }
    # The synthetic offline/restart case logs its expected connection failures to
    # stderr. Keep the one machine-readable JSON result separate from diagnostics.
    $gateJson = @($gateOutput | ForEach-Object { [string]$_ } | Where-Object {
        $_.TrimStart().StartsWith("{")
    })
    if ($gateJson.Count -ne 1) { throw "Offline gate did not emit exactly one JSON result" }
    $gate = $gateJson[0] | ConvertFrom-Json
    if (-not $gate.passed -or $gate.qmt_contacted -or $gate.server_contacted) {
        throw "Offline gate did not report an isolated synthetic pass"
    }
    $gate | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $gatePath -Encoding UTF8
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
    $env:PYTHONUTF8 = $previousPythonUtf8
    $env:PYTHONIOENCODING = $previousPythonIoEncoding
    $ErrorActionPreference = $previousErrorActionPreference
}
$status = if (@($report.blockers).Count -gt 0) { "BLOCKED" } else { "PASSED_LOCAL_ACCEPTANCE" }
@{
    status = $status
    offline_gate = "PASS"
    native_powershell_parser = "PASS"
    inspection = $inspectionPath
    qmt_contacted = [bool]$ConnectQmt
    qmt_connection_check = $(if ($ConnectQmt) { "PASS" } else { "NOT_REQUESTED" })
    broker_submission_acceptance = "NOT_PERFORMED"
    blockers = @($report.blockers)
    tasks_modified = $false
    configuration_modified = $false
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $resultPath -Encoding UTF8
Get-Content -LiteralPath $resultPath -Raw -Encoding UTF8 | Write-Output
if ($status -eq "BLOCKED") { throw "Local inspection blockers require operator review; no tasks or config were changed" }
