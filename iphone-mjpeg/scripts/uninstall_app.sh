#!/bin/sh
set -eu

destination="/var/mobile/Applications/iPhoneCamera.app"
if [ ! -e "$destination" ]; then
    echo "iPhone Camera app is not installed"
    exit 0
fi
if ! grep -q '<string>com.local.iphonecamera</string>' "$destination/Info.plist" 2>/dev/null; then
    echo "Refusing to remove an app not owned by this project: $destination" >&2
    exit 1
fi
resolved=$(realpath "$destination" 2>/dev/null || true)
case "$resolved" in
    /private/var/mobile/Applications/iPhoneCamera.app) ;;
    *) echo "Refusing unexpected app path: $resolved" >&2; exit 1 ;;
esac
uicache -u "$destination" 2>/dev/null || true
rm -rf -- "$destination"
echo "Unregistered and removed this project's iPhone Camera app"
