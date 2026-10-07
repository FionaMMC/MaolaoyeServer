[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("pre", "sell", "buy", "cancel", "eod", "intraday", "upload-spool", "status")]
    [string]$Command,

    [ValidatePattern("^\d{8}$")]
    [string]$Date,

    [switch]$DryRun,
    [string]$InstallRoot = "C:\hydra-live",
    [string]$EnvFile,
    [string]$PythonExe = "python"
)

# Runs one OMS agent command from the active release. Same environment, release
# and lock conventions as Run-HydraLive.ps1; the server decides every order.
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

if (-not $Date) {
    # Trading dates are China dates regardless of the Windows time zone.
    $Date = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "China Standard Time").ToString("yyyyMMdd")
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
if (-not (Test-Path -LiteralPath (Join-Path $releaseRoot "live_client\oms_agent.py") -PathType Leaf)) {
    throw "Active release $releaseId has no OMS agent"
}
Import-HydraEnvironment -Path $EnvFile
if ($PythonExe -eq "python") {
    if ([string]::IsNullOrWhiteSpace($env:HYDRA_LIVE_PYTHON)) {
        throw "HYDRA_LIVE_PYTHON is required; fallback Python is forbidden"
    }
    $PythonExe = $env:HYDRA_LIVE_PYTHON
}
if (-not [IO.Path]::IsPathRooted($PythonExe) -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Hydra Python must be an existing absolute executable path"
}

$runtimeRoot = Join-Path $InstallRoot "runtime"
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
$lockPath = Join-Path $runtimeRoot "oms-$Command-$Date.lock"
$lockStream = $null
$saved = @{ PYTHONPATH = $env:PYTHONPATH; PYTHONDONTWRITEBYTECODE = $env:PYTHONDONTWRITEBYTECODE;
            PYTHONUTF8 = $env:PYTHONUTF8; PYTHONIOENCODING = $env:PYTHONIOENCODING }
try {
    try {
        $lockStream = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    }
    catch [IO.IOException] {
        throw "Another Hydra OMS '$Command' process already holds $lockPath"
    }
    $env:PYTHONPATH = $releaseRoot
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $arguments = @("-m", "live_client.oms_agent", $Command, "--date", $Date)
    if ($DryRun) { $arguments += "--dry-run" }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # A native stderr log line is not a failed Python command on PS 5.1.
        $ErrorActionPreference = "Continue"
        & $PythonExe @arguments
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "Hydra OMS '$Command' failed with exit code $exitCode"
    }
}
finally {
    foreach ($name in $saved.Keys) {
        [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process")
    }
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
