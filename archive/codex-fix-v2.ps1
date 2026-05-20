$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

Write-Host "================================================================"
Write-Host " Codex packaged-app LocalCache binary sync fix"
Write-Host " (Codex is a packaged Win32 app; AppData\Local\OpenAI is"
Write-Host "  virtualized to LocalCache. We must sync LocalCache binaries"
Write-Host "  with the MSIX-bundled app\resources\ binaries.)"
Write-Host "================================================================"
Write-Host ""

# -------- 0. Make sure nothing Codex-related is running --------
Write-Host "[0/7] Stopping any Codex / WSL processes..."
$names = @("Codex","codex","codex-command-runner","codex-windows-sandbox-setup")
foreach ($n in $names) {
    Get-Process -Name $n -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
wsl --shutdown 2>$null | Out-Null
Start-Sleep -Seconds 3

$still = Get-Process -Name $names -ErrorAction SilentlyContinue
if ($still) {
    Write-Host "WARNING: still running:" -ForegroundColor Yellow
    $still | Select-Object ProcessName, Id | Format-Table -AutoSize
    throw "Codex processes still alive. Abort to be safe."
}
Write-Host "    OK (no Codex processes running)"
Write-Host ""

# -------- 1. Paths --------
$msix = "C:\Program Files\WindowsApps\OpenAI.Codex_26.519.2081.0_x64__2p2nqsd0c76g0\app\resources"
$lc   = "C:\Users\Xiao Difu\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\OpenAI\Codex\bin"

if (-not (Test-Path -LiteralPath $msix)) { throw "MSIX resources missing: $msix" }
if (-not (Test-Path -LiteralPath $lc))   { throw "LocalCache bin missing: $lc" }

# -------- 2. Backup LocalCache bin --------
$lcBackup = "$lc.backup.$stamp"
Write-Host "[1/7] Backing up LocalCache bin to:"
Write-Host "    $lcBackup"
Copy-Item -LiteralPath $lc -Destination $lcBackup -Recurse -Force
$bkCount = (Get-ChildItem -LiteralPath $lcBackup -Recurse -File | Measure-Object).Count
Write-Host "    OK ($bkCount files backed up)"
Write-Host ""

# -------- 3. Determine which files need to be replaced --------
$candidates = @(
    "codex.exe",
    "codex-command-runner.exe",
    "codex-windows-sandbox-setup.exe",
    "node.exe",
    "node_repl.exe",
    "rg.exe"
)

Write-Host "[2/7] Comparing MSIX resources vs LocalCache:"
$toReplace = @()
foreach ($n in $candidates) {
    $src = Join-Path $msix $n
    $dst = Join-Path $lc   $n
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Host "    SKIP (no source): $n"
        continue
    }
    if (-not (Test-Path -LiteralPath $dst)) {
        Write-Host "    NEW  (not in LocalCache): $n  ->  will copy"
        $toReplace += $n
        continue
    }
    $hSrc = (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash
    $hDst = (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash
    if ($hSrc -eq $hDst) {
        Write-Host "    SAME (already up to date): $n"
    } else {
        Write-Host "    DIFF (will replace): $n  [$($hSrc.Substring(0,12)) -> $($hDst.Substring(0,12))]"
        $toReplace += $n
    }
}
Write-Host ""

if ($toReplace.Count -eq 0) {
    Write-Host "Nothing to do; LocalCache already in sync." -ForegroundColor Yellow
    return
}

# -------- 4. Replace ---------
Write-Host "[3/7] Replacing LocalCache binaries with MSIX-bundled new ones..."
foreach ($n in $toReplace) {
    $src = Join-Path $msix $n
    $dst = Join-Path $lc   $n
    Copy-Item -LiteralPath $src -Destination $dst -Force
    Write-Host "    replaced: $n"
}
Write-Host ""

# -------- 5. Verify ----------
Write-Host "[4/7] Verifying SHA256 of replaced files matches MSIX source..."
$allOk = $true
foreach ($n in $toReplace) {
    $hSrc = (Get-FileHash -LiteralPath (Join-Path $msix $n) -Algorithm SHA256).Hash
    $hDst = (Get-FileHash -LiteralPath (Join-Path $lc   $n) -Algorithm SHA256).Hash
    if ($hSrc -eq $hDst) {
        Write-Host "    OK  $n  $($hSrc.Substring(0,16))"
    } else {
        Write-Host "    !!! $n  src=$($hSrc.Substring(0,16))  dst=$($hDst.Substring(0,16))" -ForegroundColor Red
        $allOk = $false
    }
}
if (-not $allOk) { throw "Hash verification failed. Backup at: $lcBackup" }
Write-Host ""

# -------- 6. Confirm new codex.exe --version (unpackaged side, may fail with EBUSY etc.) --------
Write-Host "[5/7] (sanity) MSIX resources\codex.exe is the new bundled version:"
Write-Host "    Size: $((Get-Item -LiteralPath (Join-Path $msix 'codex.exe')).Length)"
Write-Host "    MTime: $((Get-Item -LiteralPath (Join-Path $msix 'codex.exe')).LastWriteTime)"
Write-Host ""

# -------- 7. Reset state_5.sqlite so the now-aligned backend can create it fresh --------
$codex = "C:\Users\Xiao Difu\.codex"
$stateDb = Join-Path $codex "state_5.sqlite"
$walFs   = @("state_5.sqlite-wal","state_5.sqlite-shm","state_5.sqlite-journal")

Write-Host "[6/7] Moving any current state_5.sqlite aside so the backend can"
Write-Host "      create a clean one whose migration checksums match the GUI."
Write-Host "      (Conversation history is in .codex\sessions\, NOT in this file.)"

if (Test-Path -LiteralPath $stateDb) {
    $tgt = "$stateDb.broken-localcache-mismatch.$stamp"
    Move-Item -LiteralPath $stateDb -Destination $tgt -Force
    Write-Host "    moved -> $(Split-Path $tgt -Leaf)"
} else {
    Write-Host "    (state_5.sqlite absent, nothing to move)"
}
foreach ($n in $walFs) {
    $f = Join-Path $codex $n
    if (Test-Path -LiteralPath $f) {
        $tgt = "$f.broken-localcache-mismatch.$stamp"
        Move-Item -LiteralPath $f -Destination $tgt -Force
        Write-Host "    moved -> $(Split-Path $tgt -Leaf)"
    }
}
Write-Host ""

Write-Host "[7/7] Final check: LocalCache contents after sync:"
Get-ChildItem -LiteralPath $lc -Force -File | ForEach-Object {
    $h = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.Substring(0,16)
    "    {0,-40} {1,12}  {2}  {3}" -f $_.Name, $_.Length, $_.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss"), $h
}
Write-Host ""
Write-Host "================================================================"
Write-Host " DONE. Summary:"
Write-Host "   - Conversation history (.codex\sessions\) UNTOUCHED"
Write-Host "   - LocalCache bin backed up to: $lcBackup"
Write-Host "   - old state_5.sqlite kept as: state_5.sqlite.broken-localcache-mismatch.$stamp"
Write-Host "   - Next: launch Codex from Start menu and verify."
Write-Host "================================================================"
