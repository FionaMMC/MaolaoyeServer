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

    $powerShellFailures = @()
    foreach ($script in Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -Filter "*.ps1") {
        $tokens = $null
        $parseErrors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile(
            $script.FullName,
            [ref]$tokens,
            [ref]$parseErrors
        )
        foreach ($parseError in $parseErrors) {
            $relative = $script.FullName.Substring($PackageRoot.Length).TrimStart(
                [char[]]@('\', '/')
            )
            $powerShellFailures += (
                "{0}:{1}: {2}" -f
                $relative,
                $parseError.Extent.StartLineNumber,
                $parseError.Message
            )
        }
    }
    if ($powerShellFailures.Count -gt 0) {
        throw "PowerShell syntax validation failed:`n$($powerShellFailures -join "`n")"
    }

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
        & $PythonExe -m live_client.oms_offline_acceptance
        if ($LASTEXITCODE -ne 0) {
            throw "Shared-OMS offline acceptance failed"
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
    (Join-Path $sourcePackage "oms_offline_acceptance.py"),
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

function Assert-HydraTasksStopped {
    # Installation never enables, disables, or stops a scheduled task. The operator
    # must quiesce both generations first; a running order cannot be rolled back.
    $activeTasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
        ($_.TaskName -like "Hydra-Live-*" -or $_.TaskName -like "Hydra-Oms-*") -and
        ($_.State -ne "Disabled")
    })
    if ($activeTasks.Count -gt 0) {
        throw "Disable Hydra-Live/OMS tasks and wait for running tasks to finish before installing"
    }
}
Assert-HydraTasksStopped

$git = Get-Command git -ErrorAction SilentlyContinue
$sourceIsGit = $false
if ($git) {
    # ZIP deliveries deliberately omit .git. Having git.exe on the host must not
    # make an otherwise valid archive install fail its worktree-only dirty check.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $gitProbe = @(& $git.Source -C $SourceRoot rev-parse --is-inside-work-tree 2>&1)
        $gitProbeExitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousErrorActionPreference }
    $sourceIsGit = $gitProbeExitCode -eq 0 -and ($gitProbe -join "").Trim() -eq "true"
}
if (-not $SourceCommit) {
    if (-not $sourceIsGit) {
        throw "Archive installs require the full 40-character -SourceCommit from RELEASE_MANIFEST.json"
    }
    $SourceCommit = (& $git.Source -C $SourceRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $SourceCommit -notmatch "^[0-9a-f]{40}$") {
        throw "Cannot resolve a full source commit"
    }
}
if ($sourceIsGit) {
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
    "Register-HydraLiveSubmitTask.ps1",
    "Register-HydraLiveOperationsTasks.ps1",
    "Run-HydraOms.ps1",
    "Register-HydraOmsTasks.ps1",
    "Inspect-V713SharedOms.ps1",
    "Run-V713OmsAcceptance.ps1",
    "hydra_live_market_backup.py"
)
$stateDb = Get-HydraEnvValue -Path $EnvFile -Name "HYDRA_LIVE_STATE_DB"
$userdata = Get-HydraEnvValue -Path $EnvFile -Name "HYDRA_LIVE_QMT_USERDATA_DIR"
$fingerprint = Get-HydraEnvValue -Path $EnvFile -Name "HYDRA_LIVE_EXPECTED_ACCOUNT_SHA256"
if (-not [IO.Path]::IsPathRooted($stateDb) -or -not [IO.Path]::IsPathRooted($userdata)) {
    throw "State DB and QMT userdata must be absolute private paths"
}
$account = Get-HydraEnvValue -Path $EnvFile -Name "HYDRA_LIVE_QMT_ACCOUNT_ID"
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $actualFingerprint = [BitConverter]::ToString(
        $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($account))
    ).Replace("-", "").ToLowerInvariant()
}
finally { $sha.Dispose(); $account = $null }
if ($fingerprint -cnotmatch "^[0-9a-f]{64}$" -or $fingerprint -cne $actualFingerprint) {
    throw "Private account fingerprint mismatch"
}
# This is the same byte-range lock used by execution_queue.account_submission_lock.
# Hold it across the backup and pointer swap, including config doctor/rollback.
$lockRoot = Join-Path $userdata "hydra_execution_locks"
New-Item -ItemType Directory -Path $lockRoot -Force | Out-Null
$accountLock = [IO.File]::Open((Join-Path $lockRoot "$fingerprint.lock"),
    [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
$accountLocked = $false
try {
    if ($accountLock.Length -eq 0) { $accountLock.SetLength(1) }
    $accountLock.Lock(0, 1)
    $accountLocked = $true
    Assert-HydraTasksStopped
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

    $backupScript = @'
import hashlib
import json
import pathlib
import shutil
import sqlite3
import sys
state_path, backup_path = map(pathlib.Path, sys.argv[1:3])
fingerprint = sys.argv[3]
private_values = {}
for raw in pathlib.Path(sys.argv[4]).read_text(encoding="utf-8-sig").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    key, value = line.split("=", 1)
    if key in private_values:
        raise RuntimeError("Duplicate private config key")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    private_values[key] = value
expected_binding = {"account_alias": private_values["HYDRA_LIVE_ACCOUNT_ALIAS"],
                    "execution_domain": private_values["HYDRA_LIVE_EXECUTION_DOMAIN"],
                    "account_fingerprint": fingerprint,
                    "client_id": private_values["HYDRA_LIVE_TASK_PREFIX"]}
manifest = {"account_fingerprint": fingerprint, "databases": {}, "files": {}}
for source_path, name in ((state_path, "hydra-live-state-before-upgrade.db"),
                          (state_path.with_name("oms-agent.db"), "oms-agent-before-upgrade.db")):
    if not source_path.exists():
        manifest["databases"][name] = {"present": False}
        continue
    source = sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(backup_path / name)
    try:
        source.execute("PRAGMA query_only=ON")
        source.backup(destination)
        if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite backup integrity failure")
        info = {"present": True, "quick_check": "ok"}
        if name.startswith("oms-agent"):
            tables = {row[0] for row in destination.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            binding = destination.execute("SELECT value FROM metadata WHERE key='binding'").fetchone() if "metadata" in tables else None
            info["binding_present"] = bool(binding)
            if binding:
                value = json.loads(binding[0])
                if value != expected_binding:
                    raise RuntimeError("Journal account binding mismatch")
                info["binding_sha256"] = hashlib.sha256(binding[0].encode()).hexdigest()
        manifest["databases"][name] = info
    finally:
        destination.close()
        source.close()
# Quiesced tasks + account lock make the two DB snapshots, pending delivery spool,
# and global/per-instance HOLD evidence one recoverable bundle. Never restore
# these automatically: an external broker fill cannot be undone by file rollback.
for source_path in sorted(state_path.parent.iterdir()) if state_path.parent.exists() else []:
    if source_path.name == "oms-spool" or source_path.name == "HOLD" or source_path.name.startswith(("HOLD-", "HOLD.")):
        if source_path.is_symlink() or (source_path.is_dir() and any(p.is_symlink() for p in source_path.rglob("*"))):
            raise RuntimeError("Refusing linked runtime backup source")
        target = backup_path / "oms-runtime" / source_path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(source_path, target)
        else:
            shutil.copy2(source_path, target)
for path in sorted(backup_path.rglob("*")):
    if path.is_file():
        manifest["files"][path.relative_to(backup_path).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
(backup_path / "runtime-backup.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
'@
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $backupOutput = $backupScript | & $PythonExe - $stateDb $backup $fingerprint $EnvFile 2>&1
        $backupExitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousErrorActionPreference }
    if ($backupExitCode -ne 0) {
        throw "Hydra runtime backup failed before activation; state and pointer were not restored or changed"
    }

$runnerTemporary = "$installedRunner.new"
$pointerTemporary = "$activePointer.new"
try {
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
    else {
        Remove-Item -LiteralPath $installedRunner -Force -ErrorAction SilentlyContinue
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
    throw "Activation failed; code pointer/scripts were rolled back. Runtime DB/spool/HOLD evidence was not restored. Tasks remain disabled."
}

@{
    status = "INSTALLED"
    active_release = $SourceCommit
    release_root = $releaseRoot
    env_preserved = $true
    state_preserved = $true
    backup_root = $backup
    offline_acceptance = "PASS"
    oms_offline_acceptance = "PASS"
    local_doctor = "PASS"
    tasks_modified = $false
    runtime_scripts_installed = $true
} | ConvertTo-Json
}
finally {
    if ($accountLocked) { $accountLock.Unlock(0, 1) }
    $accountLock.Dispose()
}
