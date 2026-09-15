#!/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

mkdir -p "$RUN_DIR" "$LOG_DIR"

if pid=$(read_project_pid 2>/dev/null) && is_project_process "$pid"; then
    echo "Already running (pid $pid)"
    exit 0
fi

if port_8088_report; then
    echo "Port 8088 is already occupied; no process was stopped." >&2
    exit 1
fi

python_bin=$(find_python) || {
    echo "No usable python3 found in /usr/bin, /var/jb/usr/bin, /usr/local/bin, or PATH" >&2
    exit 1
}

if [ ! -x "$PROJECT_DIR/native/iphone-camera" ]; then
    echo "Native helper is missing or not executable: $PROJECT_DIR/native/iphone-camera" >&2
    echo "Run: make -C '$PROJECT_DIR/native' && make -C '$PROJECT_DIR/native' sign" >&2
    exit 1
fi

rm -f "$PID_FILE"
nohup "$python_bin" "$PROJECT_DIR/python/server.py" >>"$LOG_FILE" 2>&1 &
pid=$!
printf '%s\n' "$pid" >"$PID_FILE"
sleep 2
if ! is_project_process "$pid"; then
    echo "Service failed to start; inspect $LOG_FILE" >&2
    rm -f "$PID_FILE"
    tail -n 30 "$LOG_FILE" 2>/dev/null || true
    exit 1
fi

echo "Started iPhone MJPEG service (pid $pid)"
"$PROJECT_DIR/scripts/status.sh"
