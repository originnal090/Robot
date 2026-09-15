#!/var/jb/usr/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

NEKOVIEW_DIR=${NEKOVIEW_DIR:-/var/jb/var/mobile/neko-api}
SERVICES_FILE="$NEKOVIEW_DIR/services.json"
ENTRY_FILE="$PROJECT_DIR/nekoview/service.json"
MANAGER="$NEKOVIEW_DIR/manage.sh"

python_bin=$(find_python) || {
    echo "No usable python3 found" >&2
    exit 1
}
[ -f "$SERVICES_FILE" ] || { echo "NekoView config not found: $SERVICES_FILE" >&2; exit 1; }
[ -f "$ENTRY_FILE" ] || { echo "Service definition not found: $ENTRY_FILE" >&2; exit 1; }
[ -f "$MANAGER" ] || { echo "NekoView manager not found: $MANAGER" >&2; exit 1; }

result=$(
    "$python_bin" - "$SERVICES_FILE" "$ENTRY_FILE" <<'PY'
import json
import os
import shutil
import sys
import tempfile
import time

services_path, entry_path = sys.argv[1:]
with open(services_path, encoding="utf-8") as stream:
    document = json.load(stream)
with open(entry_path, encoding="utf-8") as stream:
    entry = json.load(stream)
services = document.get("services")
if not isinstance(services, list):
    raise SystemExit("services.json does not contain a services list")
matches = [item for item in services if isinstance(item, dict) and item.get("id") == entry["id"]]
if matches:
    if len(matches) == 1 and matches[0] == entry:
        print("unchanged")
        raise SystemExit(0)
    raise SystemExit("refusing to overwrite a different iphone-mjpeg service entry")

backup = services_path + ".bak." + time.strftime("%Y%m%d-%H%M%S")
shutil.copy2(services_path, backup)
services.append(entry)
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
print("installed:" + backup)
PY
)

case "$result" in
    unchanged)
        echo "NekoView service is already configured"
        ;;
    installed:*)
        echo "Added iphone-mjpeg to NekoView (backup: ${result#installed:})"
        ;;
    *)
        echo "Unexpected installer result: $result" >&2
        exit 1
        ;;
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
echo "NekoView reloaded; start iphone-mjpeg from its dashboard"
