#!/var/jb/usr/bin/sh
set -eu

. "$(dirname -- "$0")/common.sh"

make -C "$PROJECT_DIR/native" app-package
echo
echo "Install this IPA with TrollStore, then open iPhone Camera once:"
echo "$PROJECT_DIR/native/build/iPhoneCamera.ipa"
echo
echo "Directly copying an app into /var/mobile/Applications is intentionally unsupported:"
echo "a TrollStore-managed app container is required for reliable TCC camera permission."
