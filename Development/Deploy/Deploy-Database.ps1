<#
.SYNOPSIS
    Fusion Flow V3 QAS - queue-based SQL deployment runner.

.DESCRIPTION
    Picks up every *.sql file in the Queue folder (in filename order), executes
    each against the target database, and on SUCCESS moves the script into the
    Archive folder (grouped by run timestamp). On the first failure it stops
    (unless -ContinueOnError) and leaves the failing script in the Queue.

    Logs:
      - Full, verbose run logs are written to  logs\_Ignore\  (GITIGNORED).
      - Use -PromoteLog to copy the run SUMMARY up one level to  logs\
        (which IS committed) so the result integrates into the repo / Claude.

    Execution engine: uses Invoke-Sqlcmd (SqlServer module) if available,
    otherwise falls back to sqlcmd.exe. Each script runs with abort-on-error.

.PARAMETER Server
    SQL Server / Azure SQL instance. Required (or set $env:FUSION_SQL_SERVER).

.PARAMETER Database
    Target database. Default: Fusion_Flow_V3_QAS.

.PARAMETER SqlUser / .PARAMETER SqlPassword
    Optional SQL auth. If omitted, integrated (Windows/AAD) auth is used.

.PARAMETER QueuePath / .PARAMETER ArchivePath / .PARAMETER LogRoot
    Override default folders. Defaults are resolved relative to the repo root.

.PARAMETER DryRun
    List what would be deployed; execute nothing and move nothing.

.PARAMETER ContinueOnError
    Keep going after a failing script instead of stopping.

.PARAMETER PromoteLog
    After the run, copy the run summary log from logs\_Ignore up to logs\.

.EXAMPLE
    .\Deploy-Database.ps1 -Server tcp:myserver.database.windows.net -DryRun

.EXAMPLE
    .\Deploy-Database.ps1 -Server localhost\SQLEXPRESS -PromoteLog
#>
[CmdletBinding()]
param(
    [string]$Server   = $env:FUSION_SQL_SERVER,
    [string]$Database  = 'Fusion_Flow_V3_QAS',
    [string]$SqlUser,
    [string]$SqlPassword,
    [string]$QueuePath,
    [string]$ArchivePath,
    [string]$LogRoot,
    [switch]$DryRun,
    [switch]$ContinueOnError,
    [switch]$PromoteLog,
    [switch]$TrustServerCertificate = $true
)

$ErrorActionPreference = 'Stop'

# --- Resolve folders --------------------------------------------------------
# Script lives in <repo>\Development\Deploy\ ; repo root is two levels up.
$DeployDir = $PSScriptRoot
$RepoRoot  = Split-Path -Parent (Split-Path -Parent $DeployDir)

if (-not $QueuePath)   { $QueuePath   = Join-Path $DeployDir 'Queue' }
if (-not $ArchivePath) { $ArchivePath = Join-Path $RepoRoot  'Archive' }
if (-not $LogRoot)     { $LogRoot     = Join-Path $RepoRoot  'logs' }

$IgnoreLogDir = Join-Path $LogRoot '_Ignore'   # gitignored, verbose
foreach ($d in @($QueuePath, $ArchivePath, $LogRoot, $IgnoreLogDir)) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
}

# Timestamp must be derived once per run (NO Get-Date inside loops for stable naming).
$runStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runLog   = Join-Path $IgnoreLogDir "deploy_$runStamp.log"
$manifest = Join-Path $ArchivePath  '_DeployManifest.csv'
$runArchive = Join-Path $ArchivePath $runStamp

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = ('{0}  [{1}]  {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message)
    Add-Content -LiteralPath $runLog -Value $line
    switch ($Level) {
        'ERROR' { Write-Host $line -ForegroundColor Red }
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        'OK'    { Write-Host $line -ForegroundColor Green }
        default { Write-Host $line }
    }
}

# --- Validate ---------------------------------------------------------------
if (-not $Server) {
    throw "No -Server given and `$env:FUSION_SQL_SERVER is not set."
}

# --- Pick execution engine --------------------------------------------------
$useModule = $false
if (Get-Command Invoke-Sqlcmd -ErrorAction SilentlyContinue) {
    $useModule = $true
} elseif (-not (Get-Command sqlcmd -ErrorAction SilentlyContinue)) {
    throw "Neither the SqlServer PowerShell module (Invoke-Sqlcmd) nor sqlcmd.exe is available."
}

function Invoke-SqlFile {
    param([string]$Path, [string]$PerFileLog)
    if ($useModule) {
        $p = @{
            ServerInstance = $Server; Database = $Database; InputFile = $Path
            AbortOnError = $true; ErrorAction = 'Stop'; QueryTimeout = 0
        }
        if ($TrustServerCertificate) { $p['TrustServerCertificate'] = $true }
        if ($SqlUser) { $p['Username'] = $SqlUser; $p['Password'] = $SqlPassword }
        Invoke-Sqlcmd @p -Verbose 4>&1 2>&1 | Tee-Object -FilePath $PerFileLog | Out-Null
    } else {
        $sqlArgs = @('-S', $Server, '-d', $Database, '-i', $Path, '-b', '-V', '16')
        if ($SqlUser) { $sqlArgs += @('-U', $SqlUser, '-P', $SqlPassword) } else { $sqlArgs += '-E' }
        if ($TrustServerCertificate) { $sqlArgs += '-C' }
        & sqlcmd @sqlArgs *>&1 | Tee-Object -FilePath $PerFileLog
        if ($LASTEXITCODE -ne 0) { throw "sqlcmd exited with code $LASTEXITCODE" }
    }
}

# --- Enumerate the queue ----------------------------------------------------
Write-Log "Deploy run $runStamp  ->  $Server / $Database  (engine: $(if($useModule){'Invoke-Sqlcmd'}else{'sqlcmd.exe'}))"
$scripts = Get-ChildItem -LiteralPath $QueuePath -Filter *.sql -File | Sort-Object Name
if (-not $scripts) { Write-Log "Queue is empty - nothing to deploy." 'OK'; return }
Write-Log ("Found {0} script(s) in queue: {1}" -f $scripts.Count, ($scripts.Name -join ', '))

if ($DryRun) {
    Write-Log "DRY RUN - no execution, no archive, no DB changes." 'WARN'
    $scripts | ForEach-Object { Write-Log "  would deploy: $($_.Name)" }
    return
}

# --- Deploy each ------------------------------------------------------------
$deployed = 0; $failed = 0
foreach ($s in $scripts) {
    $perFileLog = Join-Path $IgnoreLogDir ("{0}_{1}.log" -f $runStamp, $s.BaseName)
    Write-Log "--> deploying $($s.Name)"
    try {
        Invoke-SqlFile -Path $s.FullName -PerFileLog $perFileLog
        if (-not (Test-Path -LiteralPath $runArchive)) { New-Item -ItemType Directory -Force -Path $runArchive | Out-Null }
        Move-Item -LiteralPath $s.FullName -Destination (Join-Path $runArchive $s.Name) -Force
        Add-Content -LiteralPath $manifest -Value ('{0},{1},{2},{3},SUCCESS,{4}' -f $runStamp, $s.Name, $Server, $Database, $perFileLog)
        Write-Log "    SUCCESS - archived to Archive\$runStamp\$($s.Name)" 'OK'
        $deployed++
    } catch {
        $failed++
        Add-Content -LiteralPath $manifest -Value ('{0},{1},{2},{3},FAILED,{4}' -f $runStamp, $s.Name, $Server, $Database, $perFileLog)
        Write-Log "    FAILED - $($_.Exception.Message)" 'ERROR'
        Write-Log "    Script left in Queue for retry. See $perFileLog" 'ERROR'
        if (-not $ContinueOnError) { Write-Log "Stopping (use -ContinueOnError to keep going)." 'ERROR'; break }
    }
}

# --- Summary ----------------------------------------------------------------
Write-Log ("Run complete: {0} deployed, {1} failed." -f $deployed, $failed) $(if($failed){'ERROR'}else{'OK'})

if ($PromoteLog) {
    $promoted = Join-Path $LogRoot "deploy_$runStamp.summary.log"
    Copy-Item -LiteralPath $runLog -Destination $promoted -Force
    Write-Host "Promoted log to (committed) logs\: $promoted" -ForegroundColor Cyan
}

if ($failed -gt 0) { exit 1 }
