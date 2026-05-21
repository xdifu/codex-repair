# repair.ps1 — Friendly PowerShell wrapper around codex-repair.py
#
# Usage (double-click in Explorer, or run from PowerShell):
#   .\repair.ps1                     # interactive diagnose + repair flow
#   .\repair.ps1 -Mode doctor        # diagnose only
#   .\repair.ps1 -Mode fix           # dry-run repair plan
#   .\repair.ps1 -Mode fix -Apply    # actually apply fix
#   .\repair.ps1 -Mode fix -Isolated # zero-risk dry-run against DB copies
#
# Safety:
# * By default this script is INTERACTIVE: it shows the diagnose result and
#   asks before applying any fix.
# * When the running Codex process is detected, it warns and offers to stop
#   it. You can still proceed without stopping if you only want a doctor pass
#   or a dry-run.
# * Backups are always written by the Python script before any DB mutation.

[CmdletBinding()]
param(
    [ValidateSet("auto", "doctor", "fix", "fix-checksums", "manual-backfill", "extract-checksums")]
    [string]$Mode = "auto",
    [switch]$Apply,
    [switch]$Isolated,
    [string]$CodexHome = "$env:USERPROFILE\.codex",
    [string]$Binary,
    [switch]$NoPrompt
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyScript  = Join-Path $scriptDir "codex-repair.py"

function Write-Header($text) {
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor Cyan
}

function Test-Codex-Running {
    $procs = @(Get-Process -Name "Codex" -ErrorAction SilentlyContinue)
    return $procs.Count -gt 0
}

function Stop-Codex {
    Write-Host "  Stopping Codex processes..." -ForegroundColor Yellow
    Get-Process -Name "Codex" -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $_.CloseMainWindow() | Out-Null
        } catch {}
    }
    Start-Sleep -Seconds 2
    Get-Process -Name "Codex" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    if (Test-Codex-Running) {
        Write-Host "  Some Codex processes are still running. Please close them manually." -ForegroundColor Red
        return $false
    }
    Write-Host "  Codex stopped." -ForegroundColor Green
    return $true
}

# Validate Python availability
try {
    $pyVer = python --version 2>&1
    Write-Host "Python detected: $pyVer" -ForegroundColor DarkGray
} catch {
    Write-Host "ERROR: Python is required but not on PATH. Install Python 3.10+ first." -ForegroundColor Red
    exit 2
}

if (-not (Test-Path -LiteralPath $pyScript)) {
    Write-Host "ERROR: $pyScript not found." -ForegroundColor Red
    exit 2
}

# Build base argument list for the Python script
$pyArgs = @("--codex-home", $CodexHome)
if ($Binary)    { $pyArgs += @("--binary", $Binary) }
if ($VerbosePreference -ne 'SilentlyContinue') { $pyArgs += "-v" }
if ($Isolated)  { $pyArgs += "--use-isolated-copy" }
if ($Apply)     { $pyArgs += "--apply" }

# Decide subcommand
$subcmd = $Mode
if ($Mode -eq "auto") {
    $subcmd = "doctor"
}

# Run doctor pass first when in interactive auto mode
if ($Mode -eq "auto" -and -not $NoPrompt) {
    Write-Header "Step 1: diagnose"
    & python $pyScript @pyArgs doctor
    $doctorExit = $LASTEXITCODE

    if ($doctorExit -eq 0) {
        Write-Host ""
        Write-Host "Install looks healthy. No repair needed." -ForegroundColor Green
        exit 0
    }

    # Map exit code to human label
    $issue = switch ($doctorExit) {
        10 { "migration checksum drift" }
        11 { "backfill stuck" }
        12 { "both checksum drift AND backfill stuck" }
        20 { "backend binary not found" }
        21 { "database file not found" }
        default { "unknown issue (exit code $doctorExit)" }
    }

    Write-Host ""
    Write-Header "Step 2: review and confirm"
    Write-Host "Detected: $issue" -ForegroundColor Yellow

    if ($doctorExit -in @(20, 21)) {
        Write-Host "Cannot auto-repair: prerequisite missing. Please review the doctor output above." -ForegroundColor Red
        exit $doctorExit
    }

    if (Test-Codex-Running) {
        Write-Host ""
        Write-Host "  WARNING: Codex is currently running. To safely apply fixes," -ForegroundColor Yellow
        Write-Host "           Codex should be closed first." -ForegroundColor Yellow
        $resp = Read-Host "  Stop Codex now? [Y/n]"
        if ($resp -notin @("n", "N")) {
            if (-not (Stop-Codex)) {
                Write-Host "  Aborting: cannot apply fix while Codex is running." -ForegroundColor Red
                exit 1
            }
        } else {
            Write-Host "  Will run dry-run only. Re-launch this script with Codex closed to apply." -ForegroundColor Yellow
            $Apply = $false
        }
    }

    Write-Host ""
    Write-Host "Plan: $(if ($Apply) { 'APPLY fix to real DBs (with backup)' } else { 'DRY-RUN only (no DB changes)' })" -ForegroundColor Cyan
    if (-not $Apply) {
        Write-Host ""
        $resp = Read-Host "Run dry-run now? [Y/n]"
        if ($resp -in @("n", "N")) { exit 0 }
    } else {
        Write-Host ""
        $resp = Read-Host "Proceed with APPLY? [y/N]"
        if ($resp -notin @("y", "Y")) {
            Write-Host "  Aborted by user." -ForegroundColor Yellow
            exit 30
        }
    }

    Write-Header "Step 3: run fix"
    if ($Apply) { $pyArgs += "--apply" }
    & python $pyScript @pyArgs fix
    exit $LASTEXITCODE
}

# Non-interactive: just pass through
& python $pyScript @pyArgs $subcmd
exit $LASTEXITCODE
