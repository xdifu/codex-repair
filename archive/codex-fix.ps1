$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

Write-Host "================================================================"
Write-Host " Codex backend binary version-mismatch fix"
Write-Host " (preserves all conversation history in .codex\sessions\)"
Write-Host "================================================================"
Write-Host ""

# -------- Step 0: Make sure nothing Codex-related is running --------
Write-Host "[0/6] Stopping any Codex / WSL processes..."
$names = @("Codex","codex","codex-command-runner","codex-windows-sandbox-setup")
foreach ($n in $names) {
    Get-Process -Name $n -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
wsl --shutdown 2>$null | Out-Null
Start-Sleep -Seconds 2

$still = Get-Process -Name $names -ErrorAction SilentlyContinue
if ($still) {
    Write-Host "WARNING: still running:" -ForegroundColor Yellow
    $still | Select-Object ProcessName, Id | Format-Table -AutoSize
    throw "Codex processes still alive. Abort to be safe."
}
Write-Host "    OK (no Codex processes running)"
Write-Host ""

# -------- Step 1: Validate paths and the new binaries exist --------
$binRoot      = "C:\Users\Xiao Difu\AppData\Local\OpenAI\Codex\bin"
$newCodex     = Join-Path $binRoot "76ac88818493fc45\codex.exe"
$newRunner    = Join-Path $binRoot "76ac88818493fc45\codex-command-runner.exe"
$newSandbox   = Join-Path $binRoot "76ac88818493fc45\codex-windows-sandbox-setup.exe"
$newNodeRepl  = Join-Path $binRoot "46831e373630ff93\node_repl.exe"
$newNode      = Join-Path $binRoot "5b9024f90663758b\node.exe"

$pairs = @(
    @{ src = $newCodex;    dst = (Join-Path $binRoot "codex.exe") },
    @{ src = $newRunner;   dst = (Join-Path $binRoot "codex-command-runner.exe") },
    @{ src = $newSandbox;  dst = (Join-Path $binRoot "codex-windows-sandbox-setup.exe") },
    @{ src = $newNodeRepl; dst = (Join-Path $binRoot "node_repl.exe") },
    @{ src = $newNode;     dst = (Join-Path $binRoot "node.exe") }
)

Write-Host "[1/6] Validating that new (staged) binaries all exist..."
foreach ($p in $pairs) {
    if (-not (Test-Path -LiteralPath $p.src)) { throw "Missing staged binary: $($p.src)" }
    if (-not (Test-Path -LiteralPath $p.dst)) { throw "Missing target file:   $($p.dst)" }
    $srcLen = (Get-Item -LiteralPath $p.src).Length
    $dstLen = (Get-Item -LiteralPath $p.dst).Length
    Write-Host ("    {0,-45} {1,12} -> {2,12}" -f (Split-Path $p.dst -Leaf), $dstLen, $srcLen)
}
Write-Host ""

# -------- Step 2: Backup the entire bin directory --------
$binBackup = "$binRoot.backup.$stamp"
Write-Host "[2/6] Backing up entire bin/ tree to:"
Write-Host "    $binBackup"
Copy-Item -LiteralPath $binRoot -Destination $binBackup -Recurse -Force
$bkCount = (Get-ChildItem -LiteralPath $binBackup -Recurse -File | Measure-Object).Count
Write-Host "    OK ($bkCount files backed up)"
Write-Host ""

# -------- Step 3: Promote new binaries to bin root (overwrite old) --------
Write-Host "[3/6] Promoting new binaries to bin\ root..."
foreach ($p in $pairs) {
    Copy-Item -LiteralPath $p.src -Destination $p.dst -Force
    Write-Host "    promoted: $(Split-Path $p.dst -Leaf)"
}
Write-Host ""

# -------- Step 4: Verify hashes match staged ----------
Write-Host "[4/6] Verifying SHA256 of promoted files matches staged source..."
$allOk = $true
foreach ($p in $pairs) {
    $hSrc = (Get-FileHash -LiteralPath $p.src -Algorithm SHA256).Hash
    $hDst = (Get-FileHash -LiteralPath $p.dst -Algorithm SHA256).Hash
    $tag = if ($hSrc -eq $hDst) { "OK" } else { "MISMATCH"; $allOk = $false }
    Write-Host ("    {0,-45} {1}  src={2}  dst={3}" -f (Split-Path $p.dst -Leaf), $tag, $hSrc.Substring(0,12), $hDst.Substring(0,12))
}
if (-not $allOk) { throw "Hash verification failed. Bin backup at: $binBackup" }
Write-Host ""

# -------- Step 5: Confirm new --version --------
Write-Host "[5/6] codex.exe --version:"
$v = & (Join-Path $binRoot "codex.exe") --version 2>&1
Write-Host "    $v"
Write-Host ""

# -------- Step 6: Move corrupted state_5.sqlite aside --------
$codex   = "C:\Users\Xiao Difu\.codex"
$stateDb = Join-Path $codex "state_5.sqlite"
$walFs   = @("state_5.sqlite-wal","state_5.sqlite-shm","state_5.sqlite-journal")

Write-Host "[6/6] Moving current (corrupted) state_5.sqlite aside so the new"
Write-Host "      backend can re-create it fresh with correct migration checksums."
Write-Host "      (Conversation history is in .codex\sessions\, NOT in this file.)"

if (Test-Path -LiteralPath $stateDb) {
    $tgt = "$stateDb.broken-cli-mismatch.$stamp"
    Move-Item -LiteralPath $stateDb -Destination $tgt -Force
    Write-Host "    moved -> $(Split-Path $tgt -Leaf)"
} else {
    Write-Host "    (state_5.sqlite already absent, nothing to move)"
}
foreach ($n in $walFs) {
    $f = Join-Path $codex $n
    if (Test-Path -LiteralPath $f) {
        $tgt = "$f.broken-cli-mismatch.$stamp"
        Move-Item -LiteralPath $f -Destination $tgt -Force
        Write-Host "    moved -> $(Split-Path $tgt -Leaf)"
    }
}
Write-Host ""

Write-Host "================================================================"
Write-Host " DONE. Summary:"
Write-Host "   - Conversation history (.codex\sessions\) UNTOUCHED"
Write-Host "   - bin backed up to: $binBackup"
Write-Host "   - old state_5.sqlite kept as: state_5.sqlite.broken-cli-mismatch.$stamp"
Write-Host "   - Next step: launch Codex from the Start Menu."
Write-Host "================================================================"
