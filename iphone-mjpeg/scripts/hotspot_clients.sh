#!/var/jb/usr/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

python_bin=$(find_python) || {
    echo "No usable python3 found in /usr/bin, /var/jb/usr/bin, /usr/local/bin, or PATH" >&2
    exit 1
}

exec "$python_bin" "$PROJECT_DIR/python/hotspot_clients.py" "$@"
