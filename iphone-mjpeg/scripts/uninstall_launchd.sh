#!/bin/sh
set -eu

label_path="$HOME/Library/LaunchAgents/com.local.iphonecamera.plist"
if [ ! -e "$label_path" ]; then
    echo "LaunchAgent is not installed"
    exit 0
fi
launchctl unload "$label_path" 2>/dev/null || true
rm -f "$label_path"
echo "Unloaded and removed this project's LaunchAgent"
