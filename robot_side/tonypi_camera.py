#!/usr/bin/env python3
# ruff: noqa: BLE001, SIM115, UP006, UP007, UP031, UP035, UP045
"""Recoverable TonyPi USB-camera MJPEG service.

This standalone service replaces the camera/MJPEG portion of the vendor
``tonypi.service``.  It supports the vendor ``/?action=stream`` URL and makes
camera failure visible to systemd instead of leaving an alive process that
serves HTTP headers without frames.

Target runtime: Python 3.8+ with OpenCV installed.
"""

import argparse
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qs, urlsplit

import cv2


class ConfigError(ValueError):
    """Raised when camera service configuration is invalid."""


class CameraError(RuntimeError):
    """Raised after a camera open/read failure requires a clean reopen."""


@dataclass(frozen=True)
class CameraConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    device: Union[int, str] = -1
    width: int = 640
    height: int = 480
    fps: float = 30.0
    jpeg_quality: int = 70
    fourcc: str = "YUYV"
    flip: Optional[int] = None
    read_failure_limit: int = 3
    max_reopen_attempts: int = 5
    initial_backoff_s: float = 0.2
    max_backoff_s: float = 3.0
    stable_reset_s: float = 5.0
    startup_timeout_s: float = 15.0
    stale_timeout_s: float = 10.0


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except ValueError as exc:
        raise ConfigError("%s must be an integer" % name) from exc


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, str(default)))
    except ValueError as exc:
        raise ConfigError("%s must be a number" % name) from exc


def _parse_device(value: str) -> Union[int, str]:
    text = value.strip()
    if not text:
        raise ConfigError("TONYPI_CAMERA_DEVICE must not be empty")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _parse_flip(value: str) -> Optional[int]:
    text = value.strip().lower()
    if text in ("", "none", "off", "false"):
        return None
    try:
        flip = int(text)
    except ValueError as exc:
        raise ConfigError("TONYPI_CAMERA_FLIP must be none, -1, 0 or 1") from exc
    if flip not in (-1, 0, 1):
        raise ConfigError("TONYPI_CAMERA_FLIP must be none, -1, 0 or 1")
    return flip


def _vendor_flip(path: str = "/boot/camera_setting.yaml") -> Optional[int]:
    """Read the two simple vendor YAML scalars without requiring PyYAML."""
    try:
        text = open(path, "r", encoding="utf-8").read()
    except OSError:
        return None
    values = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.split("#", 1)[0].strip()
    if values.get("flip", "").lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        flip = int(values.get("flip_param", "-1"))
    except ValueError:
        return None
    return flip if flip in (-1, 0, 1) else None


def load_config(env: Optional[Mapping[str, str]] = None) -> CameraConfig:
    values = os.environ if env is None else env
    flip = (
        _parse_flip(values["TONYPI_CAMERA_FLIP"])
        if "TONYPI_CAMERA_FLIP" in values
        else _vendor_flip(values.get("TONYPI_CAMERA_VENDOR_CONFIG", "/boot/camera_setting.yaml"))
    )
    config = CameraConfig(
        host=values.get("TONYPI_CAMERA_HOST", "0.0.0.0").strip(),
        port=_env_int(values, "TONYPI_CAMERA_PORT", 8080),
        device=_parse_device(values.get("TONYPI_CAMERA_DEVICE", "-1")),
        width=_env_int(values, "TONYPI_CAMERA_WIDTH", 640),
        height=_env_int(values, "TONYPI_CAMERA_HEIGHT", 480),
        fps=_env_float(values, "TONYPI_CAMERA_FPS", 30.0),
        jpeg_quality=_env_int(values, "TONYPI_CAMERA_JPEG_QUALITY", 70),
        fourcc=values.get("TONYPI_CAMERA_FOURCC", "YUYV").strip(),
        flip=flip,
        read_failure_limit=_env_int(values, "TONYPI_CAMERA_READ_FAILURE_LIMIT", 3),
        max_reopen_attempts=_env_int(values, "TONYPI_CAMERA_MAX_REOPEN_ATTEMPTS", 5),
        initial_backoff_s=_env_float(values, "TONYPI_CAMERA_INITIAL_BACKOFF", 0.2),
        max_backoff_s=_env_float(values, "TONYPI_CAMERA_MAX_BACKOFF", 3.0),
        stable_reset_s=_env_float(values, "TONYPI_CAMERA_STABLE_RESET", 5.0),
        startup_timeout_s=_env_float(values, "TONYPI_CAMERA_STARTUP_TIMEOUT", 15.0),
        stale_timeout_s=_env_float(values, "TONYPI_CAMERA_STALE_TIMEOUT", 10.0),
    )
    if not config.host:
        raise ConfigError("TONYPI_CAMERA_HOST must not be empty")
    if not 1 <= config.port <= 65535:
        raise ConfigError("TONYPI_CAMERA_PORT must be between 1 and 65535")
    if config.width <= 0 or config.height <= 0 or config.fps <= 0:
        raise ConfigError("camera width, height and FPS must be positive")
    if not 1 <= config.jpeg_quality <= 100:
        raise ConfigError("TONYPI_CAMERA_JPEG_QUALITY must be between 1 and 100")
    if config.fourcc and len(config.fourcc) != 4:
        raise ConfigError("TONYPI_CAMERA_FOURCC must be empty or four characters")
    if config.read_failure_limit <= 0 or config.max_reopen_attempts < 0:
        raise ConfigError("failure limit must be positive and reopen attempts non-negative")
    positive_times = (
        config.initial_backoff_s,
        config.max_backoff_s,
        config.stable_reset_s,
        config.startup_timeout_s,
        config.stale_timeout_s,
    )
    if any(value <= 0 for value in positive_times):
        raise ConfigError("camera timeout and backoff values must be positive")
    if config.initial_backoff_s > config.max_backoff_s:
        raise ConfigError("initial camera backoff must not exceed maximum backoff")
    return config


class FrameState:
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.jpeg = None  # type: Optional[bytes]
        self.sequence = 0
        self.last_frame_at = None  # type: Optional[float]
        self.status = "starting"
        self.fault = None  # type: Optional[str]

    def publish(self, jpeg: bytes, timestamp: float) -> None:
        with self.condition:
            self.jpeg = jpeg
            self.sequence += 1
            self.last_frame_at = timestamp
            self.status = "streaming"
            self.condition.notify_all()

    def set_status(self, status: str) -> None:
        with self.condition:
            self.status = status
            self.condition.notify_all()

    def set_unavailable(self, status: str) -> None:
        """Stop serving an old frame while preserving its timestamp for the watchdog."""
        with self.condition:
            self.jpeg = None
            self.status = status
            self.condition.notify_all()

    def fail(self, message: str) -> None:
        with self.condition:
            if self.fault is None:
                self.fault = message
            self.status = "failed"
            self.condition.notify_all()

    def snapshot(self) -> Tuple[Optional[bytes], int, Optional[float], str, Optional[str]]:
        with self.condition:
            return self.jpeg, self.sequence, self.last_frame_at, self.status, self.fault

    def wait_after(self, sequence: int, timeout: float) -> Tuple[Optional[bytes], int]:
        with self.condition:
            self.condition.wait_for(
                lambda: self.sequence != sequence or self.fault is not None,
                timeout=timeout,
            )
            return self.jpeg, self.sequence


class CameraWorker(threading.Thread):
    def __init__(self, config: CameraConfig, state: FrameState, stop_event: threading.Event):
        super().__init__(name="tonypi-camera-capture", daemon=True)
        self.config = config
        self.state = state
        self.stop_event = stop_event

    def _open(self):
        capture = cv2.VideoCapture(self.config.device)
        if capture is None or not capture.isOpened():
            if capture is not None:
                capture.release()
            raise CameraError("camera device %r did not open" % (self.config.device,))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        capture.set(cv2.CAP_PROP_FPS, self.config.fps)
        if self.config.fourcc:
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.config.fourcc))
        buffer_property = getattr(cv2, "CAP_PROP_BUFFERSIZE", None)
        if buffer_property is not None:
            capture.set(buffer_property, 1)
        return capture

    def run(self) -> None:
        reopen_failures = 0
        backoff = self.config.initial_backoff_s
        while not self.stop_event.is_set():
            capture = None
            stable_since = None
            try:
                self.state.set_status("opening")
                capture = self._open()
                self.state.set_status("waiting_for_frame")
                consecutive_failures = 0
                while not self.stop_event.is_set():
                    ok, frame = capture.read()
                    now = time.monotonic()
                    if not ok or frame is None:
                        consecutive_failures += 1
                        if consecutive_failures >= self.config.read_failure_limit:
                            raise CameraError(
                                "camera read failed %d consecutive times"
                                % consecutive_failures
                            )
                        self.stop_event.wait(0.03)
                        continue
                    consecutive_failures = 0
                    if stable_since is None:
                        stable_since = now
                    elif now - stable_since >= self.config.stable_reset_s:
                        reopen_failures = 0
                        backoff = self.config.initial_backoff_s
                    if self.config.flip is not None:
                        frame = cv2.flip(frame, self.config.flip)
                    encoded, jpeg = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), self.config.jpeg_quality],
                    )
                    if not encoded:
                        raise CameraError("JPEG encoding failed")
                    self.state.publish(jpeg.tobytes(), now)
            except Exception as exc:
                reopen_failures += 1
                self.state.set_unavailable("reconnecting")
                print(
                    "[CAMERA] %s; reopen attempt %d/%d"
                    % (exc, reopen_failures, self.config.max_reopen_attempts),
                    flush=True,
                )
            finally:
                # Release the failed handle before attempting to open a replacement.
                if capture is not None:
                    try:
                        capture.release()
                    except Exception as exc:
                        print("[CAMERA] release failed: %s" % exc, flush=True)
            if self.stop_event.is_set():
                return
            if reopen_failures > self.config.max_reopen_attempts:
                self.state.fail(
                    "camera reopen limit reached (%d)" % self.config.max_reopen_attempts
                )
                return
            if self.stop_event.wait(backoff):
                return
            backoff = min(backoff * 2.0, self.config.max_backoff_s)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(state: FrameState, stop_event: threading.Event):
    class MJPEGHandler(BaseHTTPRequestHandler):
        server_version = "HCIRobotCamera/1.0"

        def log_message(self, pattern: str, *args: Any) -> None:
            print("[HTTP] %s - %s" % (self.address_string(), pattern % args), flush=True)

        def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            action = parse_qs(parsed.query).get("action", [""])[0]
            if parsed.path == "/healthz":
                jpeg, sequence, last_frame_at, status, fault = state.snapshot()
                age = None if last_frame_at is None else time.monotonic() - last_frame_at
                healthy = jpeg is not None and fault is None
                body = json.dumps(
                    {
                        "healthy": healthy,
                        "status": status,
                        "sequence": sequence,
                        "frame_age_seconds": age,
                        "fault": fault,
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                self._send_bytes(200 if healthy else 503, "application/json", body)
                return
            if action == "snapshot":
                jpeg, _, _, _, _ = state.snapshot()
                if jpeg is None:
                    self._send_bytes(503, "text/plain; charset=utf-8", b"camera unavailable\n")
                else:
                    self._send_bytes(200, "image/jpeg", jpeg)
                return
            if action != "stream" and parsed.path not in ("/stream.mjpg", "/"):
                self.send_error(404)
                return
            jpeg, sequence = state.wait_after(-1, 2.0)
            if jpeg is None:
                self._send_bytes(503, "text/plain; charset=utf-8", b"camera unavailable\n")
                return
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while not stop_event.is_set():
                    jpeg, next_sequence = state.wait_after(sequence, 1.0)
                    if jpeg is None or next_sequence == sequence:
                        continue
                    sequence = next_sequence
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(("Content-Length: %d\r\n\r\n" % len(jpeg)).encode("ascii"))
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return MJPEGHandler


def check_bind(config: CameraConfig) -> None:
    family = socket.AF_INET6 if ":" in config.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((config.host, config.port))
    finally:
        sock.close()


def run_service(config: CameraConfig) -> int:
    stop_event = threading.Event()
    state = FrameState()
    started_at = time.monotonic()
    server = None

    def on_signal(signum: int, _frame: Any) -> None:
        print("[SYS] signal %d, shutting down" % signum, flush=True)
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)

    try:
        server = ThreadedHTTPServer((config.host, config.port), make_handler(state, stop_event))
    except OSError as exc:
        print(
            "[FATAL] cannot bind HTTP %s:%d: %s" % (config.host, config.port, exc),
            file=sys.stderr,
        )
        return 2

    camera = CameraWorker(config, state, stop_event)
    camera.start()
    http_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="tonypi-camera-http",
        daemon=True,
    )
    http_thread.start()
    print(
        "[SYS] camera=%r MJPEG=http://%s:%d/?action=stream"
        % (config.device, config.host, config.port),
        flush=True,
    )
    exit_code = 0
    try:
        while not stop_event.wait(0.2):
            _, _, last_frame_at, _, fault = state.snapshot()
            now = time.monotonic()
            if fault is not None:
                print("[FATAL] %s" % fault, file=sys.stderr, flush=True)
                exit_code = 2
                break
            if not camera.is_alive():
                print("[FATAL] camera worker exited", file=sys.stderr, flush=True)
                exit_code = 2
                break
            if not http_thread.is_alive():
                print("[FATAL] HTTP worker exited", file=sys.stderr, flush=True)
                exit_code = 2
                break
            if last_frame_at is None and now - started_at > config.startup_timeout_s:
                print("[FATAL] camera produced no startup frame", file=sys.stderr, flush=True)
                exit_code = 2
                break
            if last_frame_at is not None and now - last_frame_at > config.stale_timeout_s:
                print(
                    "[FATAL] camera frame stale for %.1fs" % (now - last_frame_at),
                    file=sys.stderr,
                    flush=True,
                )
                exit_code = 2
                break
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        camera.join(timeout=1.0)
        http_thread.join(timeout=1.0)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recoverable TonyPi MJPEG camera service")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and HTTP bind without opening the camera",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config()
        if args.check:
            check_bind(config)
            print(
                "[CHECK] OK: camera=%r HTTP=%s:%d" % (config.device, config.host, config.port)
            )
            return 0
    except (ConfigError, OSError) as exc:
        print("[FATAL] %s" % exc, file=sys.stderr)
        return 2
    return run_service(config)


if __name__ == "__main__":
    sys.exit(main())
