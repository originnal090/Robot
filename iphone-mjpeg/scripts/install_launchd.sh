#!/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

template="$PROJECT_DIR/launchd/com.local.iphonecamera.plist"
destination="$HOME/Library/LaunchAgents/com.local.iphonecamera.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$RUN_DIR" "$LOG_DIR"
if [ -e "$destination" ]; then
    echo "Refusing to overwrite existing $destination" >&2
    exit 1
fi

escaped_project=$(printf '%s' "$PROJECT_DIR" | sed 's/[\/&]/\\&/g')
sed "s/__PROJECT_DIR__/$escaped_project/g" "$template" >"$destination"
chmod 600 "$destination"
launchctl load "$destination"
echo "Installed and loaded $destination"
