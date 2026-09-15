#!/bin/sh
# Read-only deployment preflight for a jailbroken iOS device.
set -u

section() {
    printf '\n[%s]\n' "$1"
}

section identity
uname -a 2>&1 || true
sw_vers 2>&1 || true
id 2>&1 || true

section jailbreak-layout
for path in /var/jb /usr /usr/local /opt/procursus; do
    if [ -e "$path" ]; then
        ls -ld "$path" 2>&1 || true
    fi
done

section python
which python3 2>&1 || true
python3 --version 2>&1 || true
for path in /usr/bin/python3 /usr/local/bin/python3 /var/jb/usr/bin/python3; do
    if [ -e "$path" ]; then
        ls -l "$path" 2>&1 || true
        "$path" --version 2>&1 || true
    fi
done
if command -v dpkg >/dev/null 2>&1; then
    dpkg -l 2>/dev/null | grep -i python || true
    dpkg -l 2>/dev/null | grep -Ei 'opencv|ffmpeg|jpeg' || true
fi

section toolchain
for command_name in clang make ldid theos nic xcrun; do
    command -v "$command_name" 2>&1 || true
done
clang --version 2>&1 || true
make --version 2>&1 | sed -n '1,2p' || true
ldid -h 2>&1 | sed -n '1,5p' || true

section sdk-and-frameworks
for path in \
    /System/Library/Frameworks/AVFoundation.framework \
    /System/Library/Frameworks/CoreMedia.framework \
    /System/Library/Frameworks/CoreVideo.framework \
    /System/Library/Frameworks/CoreGraphics.framework \
    /System/Library/Frameworks/ImageIO.framework \
    /var/jb/System/Library/Frameworks/AVFoundation.framework \
    /usr/include/AVFoundation \
    /var/jb/usr/include/AVFoundation; do
    [ ! -e "$path" ] || ls -ld "$path" 2>&1
done
find /var/theos /opt/theos /var/jb/var/theos /var/jb/opt/theos \
    -maxdepth 3 -type d -name '*.sdk' 2>/dev/null | sed -n '1,30p'

section ports
echo 'lsof -i :8088'
if command -v lsof >/dev/null 2>&1; then
    lsof -nP -i :8088 2>&1 || true
else
    echo 'lsof unavailable'
fi
echo 'netstat -an | grep 8088'
if command -v netstat >/dev/null 2>&1; then
    netstat -an 2>&1 | grep 8088 || true
else
    echo 'netstat unavailable'
fi
echo '8080 is intentionally not inspected or modified by this project.'

section camera-tcc
if command -v ldid >/dev/null 2>&1; then
    ldid -e /System/Library/Frameworks/AVFoundation.framework/AVFoundation 2>&1 | sed -n '1,80p' || true
fi
echo 'No TCC database was read or modified.'
