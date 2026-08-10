<#
.SYNOPSIS
    Force-align the local clone of Fusion_Flow_V3_QAS to EXACTLY match a remote
    GitHub branch (default: Master). Local-only commits and edits are discarded.

.DESCRIPTION
    This performs a full, destructive download-and-overwrite:
      1. Verifies git is installed and the target path is a git repository.
      2. Fetches the latest state of origin (with --prune).
      3. (Safety) Tags the current HEAD as a backup ref so nothing is truly lost.
      4. Checks out the requested branch, tracking origin/<Branch>.
      5. Hard-resets the working branch to origin/<Branch>.
      6. Removes untracked files/folders so the tree byte-matches the remote.

    By default, git-ignored files (e.g. .env, run logs) are KEPT. Pass
    -IncludeIgnored to also wipe those.

.PARAMETER Branch
    Remote branch to align the local clone to. Default: 'Master'.

.PARAMETER RepoPath
    Path to the local repository root. Defaults to the parent of this script's
    folder (so it works when the script lives in <repo>\scripts\).

.PARAMETER IncludeIgnored
    Also delete git-ignored files (.env, logs, *.xlsx, etc). Off by default.

.PARAMETER NoBackup
    Skip creating the local backup ref before the hard reset.

.PARAMETER Force
    Skip the interactive confirmation prompt. Use in automation only.

.EXAMPLE
    .\Sync-LocalToRemote.ps1
    # Align to origin/Master, keep ignored files, prompt before discarding.

.EXAMPLE
    .\Sync-LocalToRemote.ps1 -Branch Master -RepoPath 'D:\Synovia_Fusion_Core\Application_Layer\Flow_V3\Synovia-Flow\Fusion_Flow_V3_QAS' -Force
#>
[CmdletBinding()]
param(
    [string]$Branch = 'Master',
    [string]$RepoPath = (Split-Path -Parent $PSScriptRoot),
    [switch]$IncludeIgnored,
    [switch]$NoBackup,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function Write-Step  ($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok    ($m) { Write-Host "    $m" -ForegroundColor Green }
function Write-Warn2 ($m) { Write-Host "    $m" -ForegroundColor Yellow }

# --- 0. Pre-flight ----------------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git was not found on PATH. Install Git for Windows first."
}

if (-not (Test-Path -LiteralPath $RepoPath)) {
    throw "RepoPath does not exist: $RepoPath"
}

Set-Location -LiteralPath $RepoPath

# Resolve the true repo root and make sure we are inside a work tree.
$insideWorkTree = (git rev-parse --is-inside-work-tree 2>$null)
if ($LASTEXITCODE -ne 0 -or $insideWorkTree -ne 'true') {
    throw "Not a git repository: $RepoPath"
}
$RepoRoot = (git rev-parse --show-toplevel)
Set-Location -LiteralPath $RepoRoot
Write-Step "Repository: $RepoRoot"

# --- 1. Show current state --------------------------------------------------
$currentBranch = (git rev-parse --abbrev-ref HEAD)
$currentHead   = (git rev-parse --short HEAD)
Write-Step "Current local state"
Write-Ok   "Branch: $currentBranch  (HEAD $currentHead)"

$dirty = (git status --porcelain)
if ($dirty) {
    Write-Warn2 "You have uncommitted local changes that WILL be discarded:"
    git status --short
} else {
    Write-Ok "Working tree is clean."
}

# --- 2. Fetch remote --------------------------------------------------------
Write-Step "Fetching origin (prune)..."
git fetch origin --prune --tags
if ($LASTEXITCODE -ne 0) { throw "git fetch failed. Check network / credentials." }

# Confirm the requested remote branch exists.
git show-ref --verify --quiet "refs/remotes/origin/$Branch"
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 "origin/$Branch does not exist yet. Remote branches found:"
    git branch -r
    throw "Remote branch 'origin/$Branch' not found. Create it on GitHub first, or pass -Branch <name>."
}

$remoteHead = (git rev-parse --short "origin/$Branch")
Write-Ok "Target: origin/$Branch (HEAD $remoteHead)"

# --- 3. Confirm (destructive) ----------------------------------------------
if (-not $Force) {
    Write-Host ""
    Write-Warn2 "This will HARD RESET '$Branch' to origin/$Branch and DELETE untracked files."
    if ($IncludeIgnored) { Write-Warn2 "Ignored files (.env, logs, etc) will ALSO be deleted." }
    $answer = Read-Host "Type 'YES' to proceed"
    if ($answer -ne 'YES') { Write-Host "Aborted. Nothing changed." -ForegroundColor Yellow; return }
}

# --- 4. Safety backup ref ---------------------------------------------------
if (-not $NoBackup) {
    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "backup/pre-sync-$stamp"
    git branch $backup HEAD 2>$null | Out-Null
    Write-Ok "Backup of current HEAD saved as local branch: $backup"
}

# --- 5. Align to remote -----------------------------------------------------
Write-Step "Checking out '$Branch' tracking origin/$Branch ..."
git checkout -B $Branch "origin/$Branch"
if ($LASTEXITCODE -ne 0) { throw "Checkout of $Branch failed." }

Write-Step "Hard-resetting to origin/$Branch ..."
git reset --hard "origin/$Branch"
if ($LASTEXITCODE -ne 0) { throw "Hard reset failed." }

Write-Step "Removing untracked files ..."
if ($IncludeIgnored) {
    git clean -fdx
} else {
    git clean -fd
}

# --- 6. Report --------------------------------------------------------------
Write-Step "Done. Local now matches origin/$Branch."
$finalHead = (git rev-parse --short HEAD)
Write-Ok "Branch: $Branch  (HEAD $finalHead)"
git status --short --branch
