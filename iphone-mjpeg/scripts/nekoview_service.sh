#!/var/jb/usr/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

mkdir -p "$RUN_DIR" "$LOG_DIR"

if pid=$(read_project_pid 2>/dev/null) && is_project_process "$pid"; then
    echo "Already running outside this NekoView session (pid $pid)" >&2
    exit 1
fi

if port_8088_report; then
    echo "Port 8088 is already occupied; no process was stopped." >&2
    exit 1
fi

python_bin=$(find_python) || {
    echo "No usable python3 found in /usr/bin, /var/jb/usr/bin, /usr/local/bin, or PATH" >&2
    exit 1
}
command -v uiopen >/dev/null 2>&1 || {
    echo "uiopen is required for foreground app capture" >&2
    exit 1
}

bridge_port=${IPHONE_MJPEG_BRIDGE_PORT:-18088}
server_pid=

cleanup() {
    trap - EXIT HUP INT TERM
    uiopen --url iphonecamera://stop >/dev/null 2>&1 || true
    if [ -n "$server_pid" ] && is_project_process "$server_pid"; then
        kill -TERM "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    if [ -f "$PID_FILE" ] && [ "$(sed -n '1p' "$PID_FILE" 2>/dev/null)" = "$server_pid" ]; then
        rm -f "$PID_FILE"
    fi
}

trap cleanup EXIT
trap 'exit 0' HUP INT TERM

rm -f "$PID_FILE"
export IPHONE_MJPEG_LOG_FILE="$LOG_FILE"
"$python_bin" "$PROJECT_DIR/python/server.py" --no-native --tcp-port "$bridge_port" &
server_pid=$!
printf '%s\n' "$server_pid" >"$PID_FILE"

sleep 2
if ! is_project_process "$server_pid"; then
    echo "HTTP server failed to start; inspect $LOG_FILE" >&2
    wait "$server_pid" 2>/dev/null || true
    exit 1
fi

control_url=$(camera_control_url)
if ! uiopen --url "$control_url"; then
    echo "HTTP server started, but the foreground camera app could not be opened." >&2
    echo "Install native/build/iPhoneCamera.ipa with TrollStore and retry." >&2
fi

echo "NekoView owns iPhone MJPEG server pid $server_pid on 0.0.0.0:8088"
wait "$server_pid"
