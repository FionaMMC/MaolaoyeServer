[CmdletBinding()]
param(
    [string]$SourceRoot,
    [string]$InstallRoot = "C:\hydra-live",
    [string]$EnvFile,
    [string]$PythonExe = "python",
    [ValidatePattern("^[0-9a-f]{40}$")]
    [string]$SourceCommit
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-HydraEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $found = @()
    foreach ($rawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        if ($line -notmatch "^([A-Za-z_][A-Za-z0-9_]*)=(.*)$") {
            throw "Invalid private env entry; expected NAME=value"
        }
        if ($Matches[1] -eq $Name) {
            $value = $Matches[2].Trim()
            if (
                $value.Length -ge 2 -and
                (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                 ($value.StartsWith("'") -and $value.EndsWith("'")))
            ) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $found += $value
        }
    }
    if ($found.Count -ne 1 -or -not $found[0]) {
        throw "Private env must contain exactly one non-empty $Name"
    }
    return $found[0]
}

function Get-HydraPackageHash {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)

    $hashLines = @(
        Get-ChildItem -LiteralPath $PackageRoot -Recurse -File |
            Sort-Object FullName |
            ForEach-Object {
                $relativeRaw = $_.FullName.Substring($PackageRoot.Length)
                $relative = $relativeRaw.TrimStart([char[]]@('\', '/'))
                $relative = $relative.Replace('\', '/')
                $fileHash = Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
                $hash = $fileHash.Hash.ToLowerInvariant()
                "$relative=$hash"
            }
    )
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes([string]::Join("`n", $hashLines))
        $hashText = [BitConverter]::ToString($sha.ComputeHash($bytes))
        return $hashText.Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Test-HydraPackage {
    param(
        [Parameter(Mandatory = $true)][string]$ModuleRoot,
        [Parameter(Mandatory = $true)][string]$PackageRoot,
        [Parameter(Mandatory = $true)][string]$PythonExe
    )

    $syntaxCheck = @'
import ast
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
for path in root.rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
'@
    $syntaxCheck | & $PythonExe - $PackageRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Python syntax validation failed"
    }

    $previousPythonPath = $env:PYTHONPATH
    $previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
    try {
        $env:PYTHONPATH = $ModuleRoot
        $env:PYTHONDONTWRITEBYTECODE = "1"
        & $PythonExe -m live_client.offline_acceptance
        if ($LASTEXITCODE -ne 0) {
            throw "Offline-submit acceptance failed"
        }
    }
    finally {
        $env:PYTHONPATH = $previousPythonPath
        $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
    }
}

if (-not $SourceRoot) {
    $SourceRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
}
$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
$sourcePackage = Join-Path $SourceRoot "live_client"
$sourceRunner = Join-Path $sourcePackage "windows\Run-HydraLive.ps1"
foreach ($required in @(
    (Join-Path $sourcePackage "cli.py"),
    (Join-Path $sourcePackage "offline_acceptance.py"),
    $sourceRunner
)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Hydra release source is incomplete: $required"
    }
}
if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
    throw "Python executable not found: $PythonExe"
}
if (-not $EnvFile) {
    $EnvFile = Join-Path $InstallRoot "config\hydra-live.env"
}
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Private Hydra env file must exist before install: $EnvFile"
}

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $SourceCommit) {
    if (-not $git) {
        throw "Git is unavailable; provide the full 40-character -SourceCommit"
    }
    $SourceCommit = (& $git.Source -C $SourceRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $SourceCommit -notmatch "^[0-9a-f]{40}$") {
        throw "Cannot resolve a full source commit"
    }
}
if ($git) {
    $dirty = & $git.Source -C $SourceRoot status --porcelain -- live_client
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot verify Hydra source worktree"
    }
    if ($dirty) {
        throw "Hydra live_client source has uncommitted files; install from a clean commit"
    }
}

$releasesRoot = Join-Path $InstallRoot "releases"
$configRoot = Join-Path $InstallRoot "config"
$binRoot = Join-Path $InstallRoot "bin"
$scriptsRoot = Join-Path $InstallRoot "scripts"
$backupRoot = Join-Path $InstallRoot "backups"
foreach ($directory in @($releasesRoot, $configRoot, $binRoot, $scriptsRoot, $backupRoot)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$releaseRoot = Join-Path $releasesRoot $SourceCommit
$releaseManifest = Join-Path $releaseRoot "release.json"
if (-not (Test-Path -LiteralPath $releaseRoot)) {
    $stagingRoot = Join-Path $releasesRoot (".staging-" + [Guid]::NewGuid().ToString("N"))
    $stagingPackage = Join-Path $stagingRoot "live_client"
    New-Item -ItemType Directory -Path $stagingPackage -Force | Out-Null
    try {
        foreach ($file in Get-ChildItem -LiteralPath $sourcePackage -Recurse -File) {
            $relativeRaw = $file.FullName.Substring($sourcePackage.Length)
            $relative = $relativeRaw.TrimStart([char[]]@('\', '/'))
            if (
                $relative -match '(^|[\\/])(__pycache__|\.ruff_cache|tests)([\\/]|$)' -or
                $relative -match '(^|[\\/])\.DS_Store$' -or
                $relative -match '\.py[co]$'
            ) {
                continue
            }
            $destination = Join-Path $stagingPackage $relative
            New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force |
                Out-Null
            Copy-Item -LiteralPath $file.FullName -Destination $destination
        }

        Test-HydraPackage -ModuleRoot $stagingRoot `
            -PackageRoot $stagingPackage -PythonExe $PythonExe
        $packageHash = Get-HydraPackageHash -PackageRoot $stagingPackage
        @{
            source_commit = $SourceCommit
            package_sha256 = $packageHash
            installed_at = (Get-Date).ToUniversalTime().ToString("o")
            installer = "Install-HydraLiveClient.ps1"
        } | ConvertTo-Json | Set-Content -LiteralPath (
            Join-Path $stagingRoot "release.json"
        ) -Encoding UTF8
        Move-Item -LiteralPath $stagingRoot -Destination $releaseRoot
    }
    catch {
        throw "Hydra release staging failed at '$stagingRoot': $($_.Exception.Message)"
    }
}
else {
    if (-not (Test-Path -LiteralPath $releaseManifest -PathType Leaf)) {
        throw "Existing release has no manifest: $releaseRoot"
    }
    $manifest = Get-Content -LiteralPath $releaseManifest -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if ($manifest.source_commit -ne $SourceCommit) {
        throw "Existing release manifest does not match requested commit"
    }
    $actualPackageHash = Get-HydraPackageHash -PackageRoot (
        Join-Path $releaseRoot "live_client"
    )
    if ($manifest.package_sha256 -ne $actualPackageHash) {
        throw "Existing release package hash does not match its manifest"
    }
    Test-HydraPackage -ModuleRoot $releaseRoot `
        -PackageRoot (Join-Path $releaseRoot "live_client") -PythonExe $PythonExe
}

$activePointer = Join-Path $configRoot "active-release.txt"
$installedRunner = Join-Path $binRoot "Run-HydraLive.ps1"
$oldPointer = $null
$hadPointer = Test-Path -LiteralPath $activePointer -PathType Leaf
$hadRunner = Test-Path -LiteralPath $installedRunner -PathType Leaf
$runtimeScriptNames = @(
    "Invoke-HydraLiveSubmit.ps1",
    "Invoke-HydraLiveOperations.ps1",
    "Register-HydraLiveOperationsTasks.ps1",
    "hydra_live_market_backup.py"
)
$existingRuntimeScripts = @{}
if ($hadPointer) {
    $oldPointer = Get-Content -LiteralPath $activePointer -Raw -Encoding UTF8
}
$timestamp = Get-Date -Format "yyyyMMdd-HHmmssfff"
$backup = Join-Path $backupRoot $timestamp
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item -LiteralPath $EnvFile -Destination (Join-Path $backup "hydra-live.env")
if ($hadPointer) {
    Copy-Item -LiteralPath $activePointer -Destination (
        Join-Path $backup "active-release.txt"
    )
}
if ($hadRunner) {
    Copy-Item -LiteralPath $installedRunner -Destination (
        Join-Path $backup "Run-HydraLive.ps1"
    )
}
foreach ($name in $runtimeScriptNames) {
    $installed = Join-Path $scriptsRoot $name
    $exists = Test-Path -LiteralPath $installed -PathType Leaf
    $existingRuntimeScripts[$name] = $exists
    if ($exists) {
        Copy-Item -LiteralPath $installed -Destination (Join-Path $backup $name)
    }
}

$stateDb = Get-HydraEnvValue -Path $EnvFile -Name "HYDRA_LIVE_STATE_DB"
if (-not [IO.Path]::IsPathRooted($stateDb)) {
    throw "HYDRA_LIVE_STATE_DB must be an absolute private path"
}
if (Test-Path -LiteralPath $stateDb -PathType Leaf) {
    $stateBackup = Join-Path $backup "hydra-live-state-before-upgrade.db"
    $backupScript = @'
import pathlib
import sqlite3
import sys
source_path = pathlib.Path(sys.argv[1]).resolve()
backup_path = pathlib.Path(sys.argv[2])
source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
destination = sqlite3.connect(backup_path)
try:
    source.backup(destination)
    check = destination.execute("PRAGMA quick_check").fetchone()
    if check != ("ok",):
        raise RuntimeError(f"SQLite backup quick_check failed: {check}")
finally:
    destination.close()
    source.close()
'@
    $backupScript | & $PythonExe - $stateDb $stateBackup
    if ($LASTEXITCODE -ne 0) {
        throw "Hydra SQLite backup failed before activation"
    }
}

$runnerTemporary = "$installedRunner.new"
$pointerTemporary = "$activePointer.new"
Copy-Item -LiteralPath (Join-Path $releaseRoot "live_client\windows\Run-HydraLive.ps1") `
    -Destination $runnerTemporary
Move-Item -LiteralPath $runnerTemporary -Destination $installedRunner -Force
Set-Content -LiteralPath $pointerTemporary -Value $SourceCommit -Encoding ASCII
Move-Item -LiteralPath $pointerTemporary -Destination $activePointer -Force
foreach ($name in $runtimeScriptNames) {
    $source = Join-Path $releaseRoot "live_client\windows\$name"
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Release runtime script is missing: $name"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $scriptsRoot $name) -Force
}

try {
    & $installedRunner -Command doctor -InstallRoot $InstallRoot `
        -EnvFile $EnvFile -PythonExe $PythonExe
}
catch {
    if ($hadPointer) {
        Set-Content -LiteralPath $pointerTemporary -Value $oldPointer -Encoding ASCII
        Move-Item -LiteralPath $pointerTemporary -Destination $activePointer -Force
    }
    else {
        Remove-Item -LiteralPath $activePointer -Force -ErrorAction SilentlyContinue
    }
    if ($hadRunner) {
        Copy-Item -LiteralPath (Join-Path $backup "Run-HydraLive.ps1") `
            -Destination $installedRunner -Force
    }
    foreach ($name in $runtimeScriptNames) {
        $installed = Join-Path $scriptsRoot $name
        if ($existingRuntimeScripts[$name]) {
            Copy-Item -LiteralPath (Join-Path $backup $name) -Destination $installed -Force
        }
        else {
            Remove-Item -LiteralPath $installed -Force -ErrorAction SilentlyContinue
        }
    }
    throw "Local config doctor failed; active release was rolled back: $($_.Exception.Message)"
}

@{
    status = "INSTALLED"
    active_release = $SourceCommit
    release_root = $releaseRoot
    env_preserved = $true
    state_preserved = $true
    backup_root = $backup
    offline_acceptance = "PASS"
    local_doctor = "PASS"
    tasks_modified = $false
    runtime_scripts_installed = $true
} | ConvertTo-Json
