# iPhone 7 MJPEG camera service

This project exposes the rear iPhone camera as a low-latency MJPEG stream on
port **8088**. It does not inspect, stop, or reconfigure the service on port
8080.

The data path is:

```text
AVCaptureVideoDataOutput (BGRA, late frames discarded)
  -> ImageIO JPEG encoder
  -> single latest-frame slot
  -> Unix domain stream socket
  -> CPython ThreadingHTTPServer
  -> /video, /snapshot.jpg, /health
```

The native capture callback and socket sender are separate. If the socket or a
client is slow, the pending JPEG is replaced by the newest JPEG instead of
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

## Build and sign the native helper

On a device that has Clang plus the iOS framework headers:

```sh
cd /var/mobile/iphone-mjpeg/native
make
make sign
ldid -e ./iphone-camera
```

The direct build links Foundation, AVFoundation, CoreMedia, CoreVideo,
CoreGraphics, and ImageIO; Theos is not required when the installed Clang
toolchain already has usable iOS headers. `make sign` runs:

```sh
ldid -S../entitlements.plist iphone-camera
```

The supplied entitlement requests platform/no-container execution and the
private TCC camera allowance used by jailbreak daemons. Whether a jailbreak
accepts that entitlement is device-specific and must be tested. The helper
checks `authorizationStatusForMediaType:` before constructing the session. It
does not attempt to show a TCC prompt from a bare command-line process, because
Apple requires a camera usage string in an app `Info.plist` for prompting.

If the entitlement is not honored, do not edit `TCC.db` blindly. Use an app
bundle/context with `NSCameraUsageDescription`, grant Camera access once in the
UI, then run the signed helper under the identity accepted by that jailbreak.
The exact fallback must be selected from the device's observed iOS and
jailbreak environment.

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
export IPHONE_MJPEG_WIDTH=1280
export IPHONE_MJPEG_HEIGHT=720
export IPHONE_MJPEG_FPS=30
export IPHONE_MJPEG_JPEG_QUALITY=75
export IPHONE_MJPEG_ROTATION=0
```

`IPHONE_MJPEG_ROTATION` accepts `0`, `90`, `180`, or `270`. Rotation uses the
AVFoundation video connection, and mirroring is disabled. The default `0`
means landscape-right. If the mounted phone is sideways or inverted, change
this one value and restart. Common lower-bandwidth settings are `640x480@30`
and quality `70`; requested dimensions are matched to the closest rear-camera
format supporting the requested FPS.

## Start, inspect, and stop

The start script first runs both available port checks. If any process owns
8088, it prints the process/socket information and exits without killing it.

```sh
cd /var/mobile/iphone-mjpeg
chmod +x scripts/*.sh native/iphone-camera
./scripts/start.sh
./scripts/status.sh
tail -f logs/camera.log
./scripts/stop.sh
```

`stop.sh` only sends SIGTERM to the PID in this project's PID file after
verifying that its command line is this project's `python/server.py`. It does
not force-kill an unrecognized or stuck process.

## HTTP API

Replace `IPHONE_IP` with the LAN address or Tailnet hostname:

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
{"ok":true,"camera":true,"fps":29.7,"width":1280,"height":720,"frame_age_ms":18.2,"sequence":540}
```

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

## Latency and queue checks

Each MJPEG part includes `X-Capture-Timestamp-Ns`. With synchronized iPhone and
client clocks, measure native-callback-to-client latency with:

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

Install a per-user LaunchAgent only after manual startup and camera access have
passed:

```sh
./scripts/install_launchd.sh
```

The installer substitutes the current project directory into the template,
refuses to overwrite an existing plist, and loads
`~/Library/LaunchAgents/com.local.iphonecamera.plist`.

Uninstall only this project's LaunchAgent:

```sh
./scripts/uninstall_launchd.sh
```

Some jailbreaks require `launchctl bootstrap gui/501 ...` instead of legacy
`launchctl load`. Confirm the available launchctl interface during device
preflight before changing the script.

## Failure behavior

- Native camera initialization failures are logged and supervised restarts use
  a two-second delay.
- `/health` reports `camera:false` if the native socket disconnects or the last
  frame is older than two seconds.
- A stale socket path is removed only if it is actually a Unix socket; a normal
  file or symlink is never overwritten.
- The frame protocol rejects invalid magic, dimensions, incomplete JPEGs, and
  payloads over 8 MiB.
- Port 8088 conflicts are reported; no occupying process is killed.
