[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("doctor", "ledger", "query", "preflight", "submit", "settle", "settle-close", "retry", "cash-flow")]
    [string]$Command,

    [ValidatePattern("^\d{8}$")]
    [string]$Date,

    [ValidatePattern("^\d{8}$")]
    [string]$NextDate,

    [string]$InstallRoot = "C:\hydra-live",
    [string]$EnvFile,
    [string]$PythonExe = "python",
    [string]$MockState,

    [ValidateSet("DIVIDEND", "DEPOSIT", "WITHDRAWAL", "CAPITAL_ALLOCATION", "CAPITAL_DEALLOCATION", "OTHER")]
    [string]$CashFlowType,

    [double]$Amount,
    [string]$Source,
    [string]$SourceEventId,
    [ValidatePattern("^[0-9a-f]{64}$")]
    [string]$EvidenceSha256,
    [string]$Description,
    [switch]$TransitionToAttributed
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Import-HydraEnvironment {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Hydra private env file not found: $Path"
    }
    $seen = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        if ($line -notmatch "^([A-Za-z_][A-Za-z0-9_]*)=(.*)$") {
            throw "Invalid env entry; expected NAME=value (secret not printed)"
        }
        $name = $Matches[1]
        if ($seen.ContainsKey($name)) {
            throw "Duplicate env name is not allowed: $name"
        }
        $seen[$name] = $true
        $value = $Matches[2].Trim()
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

if ($Command -notin @("doctor", "ledger") -and -not $Date) {
    throw "-$Command requires an explicit -Date YYYYMMDD"
}
if ($Command -eq "retry" -and -not $NextDate) {
    throw "-retry requires -NextDate YYYYMMDD"
}
if ($MockState -and $Command -notin @("preflight", "submit", "settle", "settle-close", "retry", "cash-flow")) {
    throw "-MockState is not valid for this command"
}
if ($Command -eq "cash-flow" -and (
    -not $CashFlowType -or $Amount -eq 0 -or -not $Source -or
    -not $SourceEventId -or -not $EvidenceSha256
)) {
    throw "cash-flow requires type, non-zero amount, source, source-event-id and evidence SHA-256"
}
if (-not $EnvFile) {
    $EnvFile = Join-Path $InstallRoot "config\hydra-live.env"
}

$activePointer = Join-Path $InstallRoot "config\active-release.txt"
if (-not (Test-Path -LiteralPath $activePointer -PathType Leaf)) {
    throw "Hydra active release pointer not found: $activePointer"
}
$releaseId = (Get-Content -LiteralPath $activePointer -Raw -Encoding UTF8).Trim()
if ($releaseId -notmatch "^[0-9a-f]{40}$") {
    throw "Hydra active release pointer is invalid"
}
$releaseRoot = Join-Path (Join-Path $InstallRoot "releases") $releaseId
$packageRoot = Join-Path $releaseRoot "live_client"
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "cli.py") -PathType Leaf)) {
    throw "Hydra active release is incomplete: $releaseId"
}
Import-HydraEnvironment -Path $EnvFile
if ($PythonExe -eq "python") {
    if ([string]::IsNullOrWhiteSpace($env:HYDRA_LIVE_PYTHON)) {
        throw "HYDRA_LIVE_PYTHON is required; fallback Python is forbidden"
    }
    $PythonExe = $env:HYDRA_LIVE_PYTHON
}
if (-not [IO.Path]::IsPathRooted($PythonExe) -or -not (
    Test-Path -LiteralPath $PythonExe -PathType Leaf
)) {
    throw "Hydra Python must be an existing absolute executable path"
}

$runtimeRoot = Join-Path $InstallRoot "runtime"
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
$lockSuffix = if ($Date) { "$Command-$Date" } else { $Command }
$lockPath = Join-Path $runtimeRoot "$lockSuffix.lock"
$lockStream = $null
$previousPythonPath = $env:PYTHONPATH
$previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    try {
        $lockStream = [IO.File]::Open(
            $lockPath,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    }
    catch [IO.IOException] {
        throw "Another Hydra '$Command' process already holds $lockPath"
    }

    $env:PYTHONPATH = $releaseRoot
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $arguments = @("-m", "live_client.cli", $Command)
    if ($Date) {
        $arguments += @("--date", $Date)
    }
    if ($NextDate) {
        $arguments += @("--next-date", $NextDate)
    }
    if ($MockState) {
        $arguments += @("--mock-state", $MockState)
    }
    if ($Command -eq "cash-flow") {
        $arguments += @(
            "--type", $CashFlowType,
            "--amount", $Amount.ToString([Globalization.CultureInfo]::InvariantCulture),
            "--source", $Source,
            "--source-event-id", $SourceEventId,
            "--evidence-sha256", $EvidenceSha256
        )
        if ($Description) {
            $arguments += @("--description", $Description)
        }
        if ($TransitionToAttributed) {
            $arguments += "--transition-to-attributed"
        }
    }
    & $PythonExe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Hydra '$Command' failed with exit code $LASTEXITCODE"
    }
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
