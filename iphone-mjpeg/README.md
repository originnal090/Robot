# iPhone 7 MJPEG camera service

This project exposes the rear iPhone camera as a low-latency MJPEG stream on
port **8088**. It does not inspect, stop, or reconfigure the service on port
8080.

The tested data path is:

```text
AVCaptureVideoDataOutput (BGRA, late frames discarded)
  -> ImageIO JPEG encoder
  -> single latest-frame slot
  -> loopback TCP frame bridge (127.0.0.1:18088)
  -> CPython ThreadingHTTPServer
  -> /video, /snapshot.jpg, /health
```

Camera capture runs inside the foreground `iPhone Camera` app so iOS grants a
normal TCC camera session. The native capture callback and socket sender are
separate. If the socket or a client is slow, the pending JPEG is replaced by
the newest JPEG instead of
building a frame queue. Every HTTP stream runs in its own thread.

## Device environment

The inspected iPhone 7 environment and exact paths are recorded in
[`ENVIRONMENT.md`](ENVIRONMENT.md).

Run the read-only preflight before building:

```sh
cd /var/mobile/iphone-mjpeg
chmod +x scripts/*.sh
./scripts/inspect_device.sh | tee logs/preflight.log
```

The scripts look for Python in this order:

1. `/usr/bin/python3`
2. `/var/jb/usr/bin/python3`
3. `/usr/local/bin/python3`
4. the first `python3` in `PATH`

No particular rootless or rootful layout is assumed.

The checked-in shell shebang is `/var/jb/usr/bin/sh`, matching this tested
rootless phone. On a rootful device, change it to that device's actual shell
path after preflight.

## Build, sign, and install the camera app

On a device that has Clang plus the iOS framework headers:

```sh
cd /var/mobile/iphone-mjpeg
./scripts/install_app.sh
```

This creates:

```text
/var/mobile/iphone-mjpeg/native/build/iPhoneCamera.ipa
```

Install that IPA with TrollStore, open **iPhone Camera**, and allow Camera
access. Keep the app visible while streaming; iOS 15 interrupts this camera
session when the app enters the background. Do not manually copy the bundle
into `/var/mobile/Applications`: that does not provide a reliable registered
app identity for TCC.

The direct build links Foundation, AVFoundation, CoreMedia, CoreVideo,
CoreGraphics, ImageIO, and UIKit. Theos is not required because the installed
Clang toolchain already has usable iOS headers. `make app-package` signs the
executable with:

```sh
ldid -S../app-entitlements.plist build/iPhoneCamera.app/iPhoneCamera
```

TrollStore supplies the installed app identity. `Info.plist` contains
`NSCameraUsageDescription`; the app calls
`requestAccessForMediaType:completionHandler:` and reports denial in its UI.
No TCC database is edited. The separately buildable `native/iphone-camera`
binary remains useful for diagnostics, but on this iOS build its camera session
is interrupted in a background command-line context and is not the normal
service path.

The installed v6 app was verified with the TrollStore-added entitlement
`com.apple.private.security.container-required=com.local.iphonecamera`. Do not
add `com.apple.private.security.no-sandbox`: on this iPhone it allowed the
preview layer to render but prevented video/photo sample callbacks.

References:

- [Apple: requesting camera authorization](https://developer.apple.com/documentation/AVFoundation/requesting-authorization-to-capture-and-save-media)
- [Apple: setting up a capture session](https://developer.apple.com/documentation/avfoundation/setting-up-a-capture-session)
- [Procursus ldid](https://github.com/ProcursusTeam/ldid)

## Configuration

All service settings are centralized in `python/config.py` and may be
overridden with environment variables:

```sh
export IPHONE_MJPEG_HOST=0.0.0.0
export IPHONE_MJPEG_PORT=8088
export IPHONE_MJPEG_BRIDGE_PORT=18088
export IPHONE_MJPEG_WIDTH=640
export IPHONE_MJPEG_HEIGHT=480
export IPHONE_MJPEG_FPS=30
export IPHONE_MJPEG_STREAM_FPS=15
export IPHONE_MJPEG_JPEG_QUALITY=60
export IPHONE_MJPEG_ROTATION=0
```

Port 18088 is an internal app-to-Python frame bridge bound only to
`127.0.0.1`; it is not exposed to the LAN. Port 8088 remains the public HTTP
service port.

`IPHONE_MJPEG_ROTATION` accepts `0`, `90`, `180`, or `270`. Rotation uses the
AVFoundation video connection, and mirroring is disabled. The default `0`
means landscape-right. If the mounted phone is sideways or inverted, change
this one value and restart. The tested defaults capture at `640x480@30` and
serve each client at up to 15 FPS with JPEG quality 60. This keeps two clients
below the measured Tailnet/Wi-Fi throughput without queuing stale frames.
`1280x720@30`, quality 75 remains available through the environment variables
when the LAN has enough bandwidth.

App build 7 adds an on-device **Config** panel. It persists resolution
(640x480 or 1280x720), requested camera FPS (15/24/30), JPEG quality (40–90),
and output rotation (0/90/180/270) in the app container. Tap **Apply & save**;
the capture session restarts while the Python/NekoView service stays up. The
preview uses the same orientation as the encoded output and fills the landscape
screen. NekoView opens the app with only the loopback bridge port by default,
so it does not overwrite saved settings. Explicit `IPHONE_MJPEG_*` environment
variables still override the corresponding saved values.

## Start, inspect, and stop

The start script first runs `lsof` and `netstat` when available, followed by a
real bind probe. If any process owns 8088, it prints the process/socket
information and exits without killing it. On the tested phone neither
inspection tool is installed, so the fallback reports the conflict but cannot
name its owner; install neither tool merely for this service.

```sh
cd /var/mobile/iphone-mjpeg
chmod +x scripts/*.sh
./scripts/start.sh
./scripts/status.sh
tail -f logs/camera.log
./scripts/stop.sh
```

The native app log lives inside its sandbox. Locate it without assuming the
container UUID:

```sh
find /private/var/mobile/Containers/Data/Application \
  -path '*/Library/Logs/iPhoneCamera/app.log' -print
```

`start.sh` launches the Python HTTP process and opens the installed foreground
camera app through its `iphonecamera://` control URL. `stop.sh` only sends
SIGTERM to the PID in this project's PID file after
verifying that its command line is this project's `python/server.py`. It does
not force-kill an unrecognized or stuck process. Stop/uninstall the app itself
from TrollStore; `scripts/uninstall_app.sh` intentionally does not delete app
container paths.

## NekoView service control

The tested phone already runs NekoView from `/var/jb/var/mobile/neko-api`.
Install this project's service definition without modifying the existing
OpenList or PocketBase entries:

```sh
cd /var/mobile/iphone-mjpeg
./scripts/install_nekoview.sh
```

The installer validates `services.json`, creates a timestamped adjacent backup,
atomically appends only the `iphone-mjpeg` entry, and restarts the NekoView
dashboard so it reloads the JSON. NekoView then controls the tmux session named
`iphone-mjpeg`; use its Start, Stop, Restart, Status, and Logs controls.

NekoView runs `scripts/nekoview_service.sh`, a foreground ownership wrapper.
The wrapper starts the Python HTTP process, opens the camera app, remains alive
for the lifetime of the server, and stops only that verified child when the
NekoView session ends. This is intentionally different from `start.sh`, which
daemonizes and therefore cannot be tracked correctly by NekoView. Python logs
are available both in NekoView and at `logs/camera.log`.

Environment variables can be added to the NekoView command if mounting changes:
`IPHONE_MJPEG_WIDTH`, `IPHONE_MJPEG_HEIGHT`, `IPHONE_MJPEG_FPS`,
`IPHONE_MJPEG_JPEG_QUALITY`, `IPHONE_MJPEG_ROTATION`, and
`IPHONE_MJPEG_BRIDGE_PORT`. Defaults are 640x480, requested 30 FPS, JPEG quality
60, rotation 0, and loopback bridge port 18088.

To remove only this service entry (leaving NekoView and all other services in
place):

```sh
./scripts/uninstall_nekoview.sh
```

## HTTP API

Replace `IPHONE_IP` with the LAN address or Tailnet hostname (for this device,
the Tailnet hostname is `yhiphone7`):

| URL | Result |
| --- | --- |
| `http://IPHONE_IP:8088/` | Minimal browser page containing `<img src="/video">` |
| `http://IPHONE_IP:8088/video` | `multipart/x-mixed-replace; boundary=frame` stream |
| `http://IPHONE_IP:8088/snapshot.jpg` | Latest JPEG, or HTTP 503 before the first frame |
| `http://IPHONE_IP:8088/health` | Camera state, receive FPS, dimensions, frame age, sequence |

Local device smoke test:

```sh
curl -fsS http://127.0.0.1:8088/health
curl -fsS -o test.jpg http://127.0.0.1:8088/snapshot.jpg
file test.jpg
```

LAN/Tailnet smoke test:

```sh
curl -fsS http://IPHONE_IP:8088/health
```

The health response is similar to:

```json
{"ok":true,"camera":true,"fps":24.9,"width":640,"height":480,"frame_age_ms":18.2,"sequence":540}
```

Validated on 2026-09-15 with app v6:

- iPhone local and PC LAN health returned `ok:true` at roughly 24–25 capture FPS.
- `snapshot.jpg` decoded as a 640×480, three-channel JPEG.
- OpenCV read 640×480 BGR frames, disconnected, and reconnected successfully.
- One stream over the current Wi-Fi measured p50 103 ms and p95 198 ms over
  120 frames. With a concurrent OpenCV client, p50 was 171 ms; transient Wi-Fi
  spikes occurred but latency returned to the newest frame instead of growing.
- The current Wi-Fi address was `10.208.88.250`; it is DHCP-assigned and may
  change. The Tailnet hostname is `yhiphone7`.

## OpenCV / TonyPi

OpenCV can read the stream directly:

```python
import cv2

cap = cv2.VideoCapture("http://IPHONE_IP:8088/video")
while True:
    ok, frame = cap.read()
    if not ok:
        print("failed")
        break
    print(frame.shape)
    cv2.imshow("iphone", frame)
    if cv2.waitKey(1) == 27:
        break
cap.release()
```

The equivalent checked-in client is:

```sh
python tests/opencv_client.py IPHONE_IP
```

No TonyPi source or configuration is changed by this project.

## Training-data capture

App build 7 can save training frames without routing raw buffers through
Python. Open **Config**, choose 1, 2, or 5 capture FPS, and tap **Start capture**.
Tap **Stop capture** before copying the dataset. Each run creates a unique
`Documents/Datasets/session-...` directory containing JPEG files,
`metadata.json`, and a timestamped `frames.jsonl` manifest. Image writes run on
a separate serial queue with at most one pending frame, so slow storage causes
sampling skips rather than camera-pipeline backlog. Existing sessions are never
overwritten.

`UIFileSharingEnabled` and in-place document access are enabled, so datasets
can be copied from Files/Finder under **iPhone Camera → Datasets**. On the
jailbroken phone, locate (do not assume) the current container with:

```sh
find /private/var/mobile/Containers/Data/Application \
  -path '*/Documents/Datasets' -type d -print
```

Alternatively, collect directly on a PC or TonyPi without OpenCV or Pillow:

```sh
python tests/collect_dataset.py IPHONE_IP --output data/iphone-camera --fps 2 --seconds 600
```

This reads `/video`, samples current frames without building a stale queue, and
writes the same JPEG-plus-JSONL session layout. It captures images and timing
metadata only; annotations/labels should be added as a separate training-data
step.

## Latency and queue checks

Each MJPEG part includes `X-Capture-Timestamp-Ns`. The probe estimates the
iPhone/client clock offset with several `/health` round trips, then measures
native-callback-to-client latency with:

```sh
python tests/latency_probe.py IPHONE_IP --frames 300
```

Run it once immediately after startup and again after a continuous 10–15 minute
stream. Compare p50/p95/max and confirm the result does not grow over time. This
probe includes JPEG encode, socket bridge, HTTP write, and network transit. To
include OpenCV/FFmpeg decoding and buffering, point the camera at a millisecond
clock and compare that clock with the displayed OpenCV frame. The target is
under 200 ms with no monotonic growth.

Connect one OpenCV client and one browser simultaneously, refresh/close the
browser repeatedly, and reconnect OpenCV. Client disconnects are isolated to
their handler thread and do not terminate capture or the server.

## launchd

On iOS, `launchctl help` confirms there is no per-user or GUI domain. Install a
system-domain LaunchDaemon only after manual startup and camera access have
passed (sudo prompts for the mobile user's password):

```sh
sudo ./scripts/install_launchd.sh
```

The installer detects rootless `/var/jb/Library/LaunchDaemons` versus rootful
`/Library/LaunchDaemons`, substitutes the actual project and shell paths,
refuses to overwrite an existing plist, and bootstraps it in the iOS system
domain while running the job as `mobile`.

The LaunchDaemon starts the HTTP component and asks SpringBoard to open the
camera app. It cannot bypass the iOS foreground-camera or locked-device rules;
after a reboot/unlock, confirm that **iPhone Camera** is visible before relying
on the stream.

Uninstall only this project's LaunchAgent:

```sh
sudo ./scripts/uninstall_launchd.sh
```

The files and privilege checks were validated as `mobile`; installation was
not performed because the SSH session is uid 501 and non-interactive sudo is
disabled. The manual service is left running. This avoids silently installing
a system LaunchDaemon without an authenticated root session.

## Failure behavior

- Native camera initialization failures are logged and supervised restarts use
  a two-second delay.
- `/health` reports `camera:false` and `fps:0.0` if the native producer disconnects or the last
  frame is older than two seconds.
- App mode binds its internal bridge only to `127.0.0.1`; CLI diagnostic mode
  retains the guarded Unix-socket transport.
- The frame protocol rejects invalid magic, dimensions, incomplete JPEGs, and
  payloads over 8 MiB.
- Port 8088 conflicts are reported; no occupying process is killed.
