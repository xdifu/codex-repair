$ErrorActionPreference = "Continue"

# Kill anything left
Get-Process | Where-Object { $_.ProcessName -match "Codex|codex" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# Reset state_5.sqlite so Codex must recreate
$codex = "C:\Users\Xiao Difu\.codex"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Get-ChildItem -LiteralPath $codex -Filter "state_5.sqlite*" -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch "broken" } |
    ForEach-Object {
        Move-Item -LiteralPath $_.FullName -Destination "$($_.FullName).broken-spy2.$stamp" -Force
    }

# Register WMI process start trace - this CAPTURES short-lived processes
$logFile = "C:\Users\Xiao Difu\codex-spy-trace.log"
"=== spy session $stamp started at $(Get-Date -Format o) ===" | Out-File -FilePath $logFile -Encoding utf8

$global:procMap = @{}
$action = {
    $e = $Event.SourceEventArgs.NewEvent
    $line = "{0:HH:mm:ss.fff} START pid={1,-6} ppid={2,-6} {3}" -f (Get-Date), $e.ProcessID, $e.ParentProcessID, $e.ProcessName
    Add-Content -Path "C:\Users\Xiao Difu\codex-spy-trace.log" -Value $line
    # Try also fetching CommandLine via Get-CimInstance (race-y but worth trying)
    Start-Job -ScriptBlock {
        param($pid, $logf)
        try {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$pid" -ErrorAction SilentlyContinue
            if ($p) {
                $cmd = $p.CommandLine
                if ($cmd -and $cmd.Length -gt 400) { $cmd = $cmd.Substring(0,400) + "..." }
                "  cmd[$pid]: $cmd" | Out-File -FilePath $logf -Append -Encoding utf8
                "  exe[$pid]: $($p.ExecutablePath)" | Out-File -FilePath $logf -Append -Encoding utf8
            }
        } catch {}
    } -ArgumentList $e.ProcessID, "C:\Users\Xiao Difu\codex-spy-trace.log" | Out-Null
}

$startSub = Register-WmiEvent -Class Win32_ProcessStartTrace -SourceIdentifier CodexStart -Action $action
$stopAction = {
    $e = $Event.SourceEventArgs.NewEvent
    $line = "{0:HH:mm:ss.fff} STOP  pid={1,-6} exit={2,-6} {3}" -f (Get-Date), $e.ProcessID, $e.ExitStatus, $e.ProcessName
    Add-Content -Path "C:\Users\Xiao Difu\codex-spy-trace.log" -Value $line
}
$stopSub = Register-WmiEvent -Class Win32_ProcessStopTrace -SourceIdentifier CodexStop -Action $stopAction

Write-Host "Spy registered. Launching Codex in 2 seconds..."
Start-Sleep -Seconds 2

Start-Process "explorer.exe" -ArgumentList "shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"

Write-Host "Codex launched. Capturing 25 seconds of process events..."
Start-Sleep -Seconds 25

Unregister-Event -SourceIdentifier CodexStart -ErrorAction SilentlyContinue
Unregister-Event -SourceIdentifier CodexStop -ErrorAction SilentlyContinue
Get-Job | Wait-Job -Timeout 5 | Out-Null
Get-Job | Remove-Job -Force

Write-Host ""
Write-Host "=== Trace log (filtered to interesting names) ==="
Get-Content -Path "C:\Users\Xiao Difu\codex-spy-trace.log" | Where-Object { $_ -match "codex|wsl|bash|node|cmd\.exe|powershell|sqlite" -or $_ -match "^===" } | ForEach-Object { Write-Host $_ }

Write-Host ""
Write-Host "=== Full trace also at: C:\Users\Xiao Difu\codex-spy-trace.log ==="
Write-Host ""
Write-Host "=== state_5.sqlite after spy ==="
Get-ChildItem -LiteralPath $codex -Filter "state_5.sqlite*" -File -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch "broken" } | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize

Write-Host ""
Write-Host "=== Kill Codex now ==="
Get-Process | Where-Object { $_.ProcessName -match "Codex|codex" } | Stop-Process -Force -ErrorAction SilentlyContinue
