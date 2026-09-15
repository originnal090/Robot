#!/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

if pid=$(read_project_pid 2>/dev/null) && is_project_process "$pid"; then
    echo "Process: running (pid $pid)"
else
    echo "Process: stopped"
fi

if command -v curl >/dev/null 2>&1 && health=$(curl -fsS --max-time 2 http://127.0.0.1:8088/health 2>/dev/null); then
    echo "Listening on 0.0.0.0:8088"
    echo "Health: $health"
else
    echo "Health: unavailable"
    port_8088_report || true
fi
