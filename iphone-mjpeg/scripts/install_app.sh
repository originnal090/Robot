#!/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

source_bundle="$PROJECT_DIR/native/build/iPhoneCamera.app"
destination="/var/mobile/Applications/iPhoneCamera.app"

if [ ! -x "$source_bundle/iPhoneCamera" ]; then
    echo "Built app is missing. Run make -C '$PROJECT_DIR/native' app app-sign" >&2
    exit 1
fi
if [ -e "$destination" ]; then
    if ! grep -q '<string>com.local.iphonecamera</string>' "$destination/Info.plist" 2>/dev/null; then
        echo "Refusing to replace an app not owned by this project: $destination" >&2
        exit 1
    fi
    resolved=$(realpath "$destination" 2>/dev/null || true)
    case "$resolved" in
        /private/var/mobile/Applications/iPhoneCamera.app) rm -rf -- "$destination" ;;
        *) echo "Refusing unexpected app path: $resolved" >&2; exit 1 ;;
    esac
fi
cp -R "$source_bundle" "$destination"
chmod 755 "$destination/iPhoneCamera"
uicache -p "$destination"
echo "Installed com.local.iphonecamera at $destination"
