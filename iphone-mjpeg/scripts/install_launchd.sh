#!/var/jb/usr/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

template="$PROJECT_DIR/launchd/com.local.iphonecamera.plist"
if [ "$(id -u)" -ne 0 ]; then
    echo "Installing an iOS system-domain LaunchDaemon requires root." >&2
    echo "Run: sudo '$PROJECT_DIR/scripts/install_launchd.sh'" >&2
    exit 1
fi
if [ -d /var/jb/Library/LaunchDaemons ]; then
    launchd_dir=/var/jb/Library/LaunchDaemons
elif [ -d /Library/LaunchDaemons ]; then
    launchd_dir=/Library/LaunchDaemons
else
    echo "No rootless or rootful LaunchDaemons directory found" >&2
    exit 1
fi
if [ -x /var/jb/usr/bin/sh ]; then
    service_shell=/var/jb/usr/bin/sh
elif [ -x /bin/sh ]; then
    service_shell=/bin/sh
else
    echo "No usable service shell found" >&2
    exit 1
fi
destination="$launchd_dir/com.local.iphonecamera.plist"
mkdir -p "$RUN_DIR" "$LOG_DIR"
if [ -e "$destination" ]; then
    echo "Refusing to overwrite existing $destination" >&2
    exit 1
fi

escaped_project=$(printf '%s' "$PROJECT_DIR" | sed 's/[\/&]/\\&/g')
escaped_shell=$(printf '%s' "$service_shell" | sed 's/[\/&]/\\&/g')
sed -e "s/__PROJECT_DIR__/$escaped_project/g" -e "s/__SHELL__/$escaped_shell/g" \
    "$template" >"$destination"
chmod 644 "$destination"
if ! launchctl bootstrap system "$destination" 2>/dev/null; then
    launchctl load "$destination"
fi
echo "Installed and bootstrapped $destination"
