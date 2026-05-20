#!/bin/bash
set -e
export HOME=/root
export CODEX_HOME='/mnt/c/Users/Xiao Difu/.codex'
export TERM=dumb
SOCK=/tmp/codex-bf-$$.sock
rm -f "$SOCK"
echo "[runner] starting app-server at PID $$, sock=$SOCK"
exec '/mnt/c/Users/Xiao Difu/.codex/bin/wsl/7945a00f33bdc140/codex' app-server --listen "unix://$SOCK" >/tmp/codex-bf.log 2>&1