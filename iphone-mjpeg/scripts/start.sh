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

capture_mode=${IPHONE_MJPEG_CAPTURE_MODE:-app}
server_arguments=
case "$capture_mode" in
    app)
        if [ ! -x /var/mobile/Applications/iPhoneCamera.app/iPhoneCamera ]; then
            echo "Foreground camera app is not installed; run scripts/install_app.sh" >&2
            exit 1
        fi
        command -v uiopen >/dev/null 2>&1 || {
            echo "uiopen is required for foreground app capture" >&2
            exit 1
        }
        server_arguments=--no-native
        ;;
    cli)
        if [ ! -x "$PROJECT_DIR/native/iphone-camera" ]; then
            echo "Native helper is missing or not executable: $PROJECT_DIR/native/iphone-camera" >&2
            echo "Run: make -C '$PROJECT_DIR/native' && make -C '$PROJECT_DIR/native' sign" >&2
            exit 1
        fi
        ;;
    *)
        echo "IPHONE_MJPEG_CAPTURE_MODE must be app or cli" >&2
        exit 1
        ;;
esac

rm -f "$PID_FILE"
nohup "$python_bin" "$PROJECT_DIR/python/server.py" $server_arguments >>"$LOG_FILE" 2>&1 &
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
if [ "$capture_mode" = app ]; then
    control_url="iphonecamera://start?width=${IPHONE_MJPEG_WIDTH:-1280}&height=${IPHONE_MJPEG_HEIGHT:-720}&fps=${IPHONE_MJPEG_FPS:-30}&quality=${IPHONE_MJPEG_JPEG_QUALITY:-75}&rotation=${IPHONE_MJPEG_ROTATION:-0}"
    if ! uiopen "$control_url" >>"$LOG_FILE" 2>&1; then
        echo "HTTP server started, but the foreground camera app could not be opened" >&2
    fi
fi
"$PROJECT_DIR/scripts/status.sh"
