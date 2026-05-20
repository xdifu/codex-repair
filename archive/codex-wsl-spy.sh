#!/bin/bash
echo === codex processes in WSL ===
ps -eo pid,ppid,user,comm,args | grep -i codex | grep -v grep | head -40
echo
echo === /proc/PID/exe for codex pids ===
for p in $(pgrep -f codex 2>/dev/null); do
    exe=$(readlink -f /proc/$p/exe 2>/dev/null)
    cmd=$(cat /proc/$p/cmdline 2>/dev/null | tr '\0' ' ')
    echo "PID=$p EXE=$exe"
    echo "  CMD=$cmd"
done
echo
echo === Searching for codex Linux binaries OUTSIDE /home/god ===
for dir in /root /opt /usr/local /var/lib /tmp /run /mnt/wsl; do
    if [ -d "$dir" ]; then
        find "$dir" -maxdepth 6 -name codex -type f 2>/dev/null
    fi
done
echo
echo === /mnt/c MSIX codex Linux binary ===
ls -la "/mnt/c/Program Files/WindowsApps/OpenAI.Codex_26.519.2081.0_x64__2p2nqsd0c76g0/app/resources/codex" 2>/dev/null
echo
echo === codex Linux binaries under /mnt/c/Users/Xiao Difu/AppData ===
find "/mnt/c/Users/Xiao Difu/AppData" -maxdepth 8 -name codex -type f 2>/dev/null | head -20
echo
echo === wsl mount info ===
mount | grep -E "^(C:|drvfs|9p)" | head -5
