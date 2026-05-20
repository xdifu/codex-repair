#!/bin/bash
echo "=== Processes ==="
ps -ef | grep -E '(codex|app-server)' | grep -v grep || echo "(no codex processes)"
echo ""
echo "=== Log content ==="
if [ -f /tmp/codex-bf.log ]; then
    wc -c /tmp/codex-bf.log
    echo "--- last 50 lines ---"
    tail -50 /tmp/codex-bf.log
else
    echo "(no log file)"
fi
echo ""
echo "=== Sockets ==="
ls -la /tmp/codex-bf-*.sock 2>/dev/null || echo "(no sockets)"
