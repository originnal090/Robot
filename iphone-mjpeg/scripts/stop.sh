#!/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

pid=$(read_project_pid 2>/dev/null) || {
    echo "Not running (no valid project pid file)"
    exit 0
}

if ! is_project_process "$pid"; then
    echo "Refusing to stop pid $pid: it is not this project's server.py process." >&2
    exit 1
fi

if command -v uiopen >/dev/null 2>&1; then
    uiopen iphonecamera://stop >/dev/null 2>&1 || true
fi
kill -TERM "$pid"
count=0
while kill -0 "$pid" 2>/dev/null && [ "$count" -lt 10 ]; do
    sleep 1
    count=$((count + 1))
done
if kill -0 "$pid" 2>/dev/null; then
    echo "Project process $pid did not stop after 10 seconds; it was not force-killed." >&2
    exit 1
fi
rm -f "$PID_FILE"
echo "Stopped iPhone MJPEG service"
