#!/var/jb/usr/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Removing an iOS system-domain LaunchDaemon requires root." >&2
    echo "Run: sudo '$0'" >&2
    exit 1
fi
if [ -e /var/jb/Library/LaunchDaemons/com.local.iphonecamera.plist ]; then
    label_path=/var/jb/Library/LaunchDaemons/com.local.iphonecamera.plist
elif [ -e /Library/LaunchDaemons/com.local.iphonecamera.plist ]; then
    label_path=/Library/LaunchDaemons/com.local.iphonecamera.plist
else
    echo "LaunchDaemon is not installed"
    exit 0
fi
launchctl bootout system/com.local.iphonecamera 2>/dev/null || \
    launchctl unload "$label_path" 2>/dev/null || true
rm -f "$label_path"
echo "Booted out and removed this project's LaunchDaemon"
