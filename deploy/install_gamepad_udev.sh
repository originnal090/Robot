#!/bin/sh
set -eu

# Install a persistent permission rule for USB gamepads used by the WebUI.
# Run as root, optionally passing the service account name.
TARGET_USER=${1:-HwHiAiUser}
RULE=/etc/udev/rules.d/70-hcirobot-gamepad.rules

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' "Run this installer with sudo." >&2
    exit 1
fi
if ! id "$TARGET_USER" >/dev/null 2>&1; then
    printf '%s\n' "User does not exist: $TARGET_USER" >&2
    exit 1
fi

temporary=$(mktemp)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
printf '%s\n' \
    "SUBSYSTEM==\"input\", KERNEL==\"event*\", ENV{ID_INPUT_JOYSTICK}==\"1\", OWNER=\"$TARGET_USER\", MODE=\"0660\"" \
    > "$temporary"
install -o root -g root -m 0644 "$temporary" "$RULE"
udevadm control --reload-rules
udevadm trigger --subsystem-match=input --action=change

printf '%s\n' "Installed $RULE for $TARGET_USER"
printf '%s\n' "The running WebUI monitor should reconnect within five seconds."
