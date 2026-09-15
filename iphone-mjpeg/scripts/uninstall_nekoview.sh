#!/var/jb/usr/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

NEKOVIEW_DIR=${NEKOVIEW_DIR:-/var/jb/var/mobile/neko-api}
SERVICES_FILE="$NEKOVIEW_DIR/services.json"
MANAGER="$NEKOVIEW_DIR/manage.sh"
python_bin=$(find_python) || { echo "No usable python3 found" >&2; exit 1; }

if command -v curl >/dev/null 2>&1; then
    curl -fsS -X POST http://127.0.0.1:8765/api/services/iphone-mjpeg/stop >/dev/null 2>&1 || true
    sleep 1
fi

result=$(
    "$python_bin" - "$SERVICES_FILE" <<'PY'
import json
import os
import shutil
import sys
import tempfile
import time

services_path = sys.argv[1]
with open(services_path, encoding="utf-8") as stream:
    document = json.load(stream)
services = document.get("services")
if not isinstance(services, list):
    raise SystemExit("services.json does not contain a services list")
kept = [item for item in services if not (isinstance(item, dict) and item.get("id") == "iphone-mjpeg")]
if len(kept) == len(services):
    print("unchanged")
    raise SystemExit(0)
backup = services_path + ".bak." + time.strftime("%Y%m%d-%H%M%S")
shutil.copy2(services_path, backup)
document["services"] = kept
directory = os.path.dirname(services_path)
fd, temporary = tempfile.mkstemp(prefix=".services.", suffix=".json", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, os.stat(services_path).st_mode)
    os.replace(temporary, services_path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print("removed:" + backup)
PY
)

case "$result" in
    unchanged) echo "NekoView service was not configured" ;;
    removed:*) echo "Removed only iphone-mjpeg from NekoView (backup: ${result#removed:})" ;;
    *) echo "Unexpected uninstaller result: $result" >&2; exit 1 ;;
esac

/var/jb/usr/bin/sh "$MANAGER" restart
count=0
until "$python_bin" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8765/api/services", timeout=1).read()' >/dev/null 2>&1; do
    count=$((count + 1))
    if [ "$count" -ge 15 ]; then
        echo "NekoView did not become ready on 127.0.0.1:8765 after restart" >&2
        exit 1
    fi
    sleep 1
done
echo "NekoView reloaded"
