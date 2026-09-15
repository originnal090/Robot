# Tested device environment

Read-only inspection performed on 2026-09-15 before deployment.

| Item | Observed value |
| --- | --- |
| Hardware | iPhone 7 (`iPhone9,1`, arm64 / T8010) |
| OS | iPhone OS 15.8.2, build 19H384; Darwin 21.6.0 |
| SSH user | `mobile` (uid/gid 501) |
| Jailbreak layout | Rootless Procursus; `/var/jb` points into `/private/preboot/.../procursus` |
| Python | `/var/jb/usr/bin/python3` -> `python3.9`, Python 3.9.9 |
| Compiler | `/var/jb/usr/bin/clang`, Procursus Clang 16.0.0, arm64 iOS target |
| Build tools | `/var/jb/usr/bin/make`, `/var/jb/usr/bin/ld`, `/var/jb/usr/bin/ldid` |
| SDK | `/var/jb/usr/share/SDKs/iPhoneOS.sdk` (installed by `build-essential`) |
| Theos / xcrun | Not installed; not required for the direct build |
| Media packages | No dpkg-installed OpenCV, FFmpeg, or libjpeg package |
| Free space | About 33 GiB on both relevant APFS views |
| Installed app | TrollStore app v6, bundle id `com.local.iphonecamera` |
| Camera permission | TCC authorized (`kTCCServiceCamera`, auth value 2) |
| App sandbox | TrollStore container-required app sandbox; no `no-sandbox` entitlement |
| Internal bridge | TCP `127.0.0.1:18088` (not exposed to LAN) |
| HTTP service | `0.0.0.0:8088` |

The SDK contains framework headers and Clang automatically supplies it as the
sysroot. The live OS provides AVFoundation, CoreMedia, CoreVideo, CoreGraphics,
and ImageIO frameworks.

Neither `lsof` nor `netstat` is installed on this phone, so their requested
checks reported `unavailable`. No package was installed merely for port
inspection. A Python socket bind to `0.0.0.0:8088` succeeded and was immediately
closed, establishing that port 8088 was free at inspection time. Port 8080 was
not inspected, bound, stopped, or modified.

The existing `build-essential`, Python, and `ldid` packages are sufficient for
the first deployment attempt. No package installation is currently needed.

The bare native helper and a TrollStore app signed with `no-sandbox` both
created a running preview session but did not provide usable data-output
frames. The command-line session was also interrupted when backgrounded.
Installing the app as a normal TrollStore container sandbox immediately
restored `AVCaptureVideoDataOutput` callbacks while preserving TCC permission;
the sandboxed app therefore sends its in-memory JPEG frames to CPython over the
loopback-only bridge.
