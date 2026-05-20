#!/bin/bash
echo "=== Processes mentioning codex (other than god's VS Code one) ==="
ps -ef | grep -i codex | grep -v 'home/god' | grep -v grep || echo "(none)"
echo ""
echo "=== Sockets ==="
ls -la /tmp/codex-bf*.sock 2>/dev/null || echo "(none)"
ls -la /tmp/codex*.sock 2>/dev/null
echo ""
echo "=== Log ==="
if [ -f /tmp/codex-bf.log ]; then
    ls -la /tmp/codex-bf.log
    echo "--- content ---"
    cat /tmp/codex-bf.log
else
    echo "(no log)"
fi
