#!/var/jb/usr/bin/sh

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RUN_DIR="$PROJECT_DIR/run"
LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$RUN_DIR/server.pid"
LOG_FILE="$LOG_DIR/camera.log"

find_python() {
    for candidate in /usr/bin/python3 /var/jb/usr/bin/python3 /usr/local/bin/python3; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    command -v python3 2>/dev/null || return 1
}

camera_control_url() {
    bridge_port=${IPHONE_MJPEG_BRIDGE_PORT:-18088}
    control_url="iphonecamera://start?bridgePort=$bridge_port"
    if [ "${IPHONE_MJPEG_WIDTH+x}" = x ]; then
        control_url="$control_url&width=$IPHONE_MJPEG_WIDTH"
    fi
    if [ "${IPHONE_MJPEG_HEIGHT+x}" = x ]; then
        control_url="$control_url&height=$IPHONE_MJPEG_HEIGHT"
    fi
    if [ "${IPHONE_MJPEG_FPS+x}" = x ]; then
        control_url="$control_url&fps=$IPHONE_MJPEG_FPS"
    fi
    if [ "${IPHONE_MJPEG_JPEG_QUALITY+x}" = x ]; then
        control_url="$control_url&quality=$IPHONE_MJPEG_JPEG_QUALITY"
    fi
    if [ "${IPHONE_MJPEG_ROTATION+x}" = x ]; then
        control_url="$control_url&rotation=$IPHONE_MJPEG_ROTATION"
    fi
    printf '%s\n' "$control_url"
}

port_8088_report() {
    found=1
    if command -v lsof >/dev/null 2>&1; then
        output=$(lsof -nP -iTCP:8088 2>/dev/null || true)
        if [ -n "$output" ]; then
            printf '%s\n' "$output"
            found=0
        fi
    fi
    if command -v netstat >/dev/null 2>&1; then
        output=$(netstat -an 2>/dev/null | grep '[.:]8088[[:space:]]' || true)
        if [ -n "$output" ]; then
            printf '%s\n' "$output"
            found=0
        fi
    fi
    if [ "$found" -ne 0 ] && python_bin=$(find_python 2>/dev/null); then
        if ! "$python_bin" -c 'import socket
probe = socket.socket()
probe.settimeout(0.25)
try:
    if probe.connect_ex(("127.0.0.1", 8088)) == 0:
        raise SystemExit(1)
finally:
    probe.close()
s = socket.socket()
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 8088))
    s.listen(1)
finally:
    s.close()' 2>/dev/null; then
            echo "TCP port 8088 is occupied (owner unavailable: lsof/netstat not installed)."
            found=0
        fi
    fi
    return "$found"
}

read_project_pid() {
    [ -f "$PID_FILE" ] || return 1
    pid=$(sed -n '1p' "$PID_FILE" 2>/dev/null)
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    printf '%s\n' "$pid"
}

is_project_process() {
    pid=$1
    kill -0 "$pid" 2>/dev/null || return 1
    command_line=$(ps -p "$pid" -o command= 2>/dev/null || ps ax 2>/dev/null | awk -v wanted="$pid" '$1 == wanted {$1=""; print; exit}')
    case "$command_line" in
        *"$PROJECT_DIR/python/server.py"*) return 0 ;;
        *) return 1 ;;
    esac
}
