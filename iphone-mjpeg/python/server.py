#!/usr/bin/env python3
"""Low-latency MJPEG server backed by the native AVFoundation helper."""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO

from config import Config, default_project_dir

LOG = logging.getLogger("iphone-mjpeg")
FRAME_MAGIC = b"MJP1"
FRAME_HEADER = struct.Struct("!4sIIIQ")
MAX_JPEG_BYTES = 8 * 1024 * 1024
BOUNDARY = "frame"


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    width: int
    height: int
    capture_timestamp_ns: int
    sequence: int
    received_monotonic: float


class FrameStore:
    """A single-slot frame buffer: publishing always replaces the old frame."""

    def __init__(self, stale_after_seconds: float = 2.0) -> None:
        self._condition = threading.Condition()
        self._frame: Frame | None = None
        self._sequence = 0
        self._arrival_times: collections.deque[float] = collections.deque(maxlen=120)
        self._stale_after_seconds = stale_after_seconds
        self._native_connected = False
        self._last_error: str | None = "waiting for native camera helper"

    def set_native_connected(self, connected: bool, error: str | None = None) -> None:
        with self._condition:
            if connected and not self._native_connected:
                self._arrival_times.clear()
                self._frame = None
            self._native_connected = connected
            if error is not None:
                self._last_error = error
            elif connected:
                self._last_error = None
            self._condition.notify_all()

    def publish(self, jpeg: bytes, width: int, height: int, capture_timestamp_ns: int) -> Frame:
        now = time.monotonic()
        with self._condition:
            self._sequence += 1
            frame = Frame(jpeg, width, height, capture_timestamp_ns, self._sequence, now)
            self._frame = frame
            self._arrival_times.append(now)
            self._last_error = None
            self._condition.notify_all()
            return frame

    def latest(self) -> Frame | None:
        with self._condition:
            return self._frame

    def wait_for_new(self, after_sequence: int, timeout: float) -> Frame | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._frame is None or self._frame.sequence <= after_sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._frame

    def health(self) -> dict[str, object]:
        now = time.monotonic()
        with self._condition:
            frame = self._frame
            age = None if frame is None else max(0.0, now - frame.received_monotonic)
            camera_ok = bool(self._native_connected and age is not None and age < self._stale_after_seconds)
            fps = 0.0
            if len(self._arrival_times) >= 2:
                duration = self._arrival_times[-1] - self._arrival_times[0]
                if duration > 0:
                    fps = (len(self._arrival_times) - 1) / duration
            if not camera_ok:
                fps = 0.0
            result: dict[str, object] = {
                "ok": camera_ok,
                "camera": camera_ok,
                "server_time_ns": time.time_ns(),
                "fps": round(fps, 1),
                "width": frame.width if frame else 0,
                "height": frame.height if frame else 0,
                "frame_age_ms": round(age * 1000, 1) if age is not None else None,
                "sequence": frame.sequence if frame else 0,
                "capture_timestamp_ns": frame.capture_timestamp_ns if frame else None,
            }
            if self._last_error:
                result["error"] = self._last_error
            return result


def _read_exact(stream: BinaryIO, length: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class NativeFrameReceiver:
    def __init__(
        self,
        socket_path: Path,
        frames: FrameStore,
        tcp_address: tuple[str, int] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.frames = frames
        self.tcp_address = tcp_address
        self._stopping = threading.Event()
        self._server_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.tcp_address is None:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            if self.socket_path.exists() or self.socket_path.is_symlink():
                mode = self.socket_path.lstat().st_mode
                if not stat.S_ISSOCK(mode):
                    raise RuntimeError(f"refusing to replace non-socket path: {self.socket_path}")
                self.socket_path.unlink()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
        else:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(self.tcp_address)
        server.listen(1)
        server.setblocking(False)
        self._server_socket = server
        self._thread = threading.Thread(target=self._run, name="native-frame-receiver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._server_socket:
            self._server_socket.close()
        if self._thread:
            self._thread.join(timeout=3.0)
        try:
            if (
                self.tcp_address is None
                and self.socket_path.exists()
                and stat.S_ISSOCK(self.socket_path.lstat().st_mode)
            ):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def _run(self) -> None:
        assert self._server_socket is not None
        while not self._stopping.is_set():
            try:
                readable, _, _ = select.select([self._server_socket], [], [], 1.0)
                if not readable:
                    continue
                connection, _ = self._server_socket.accept()
            except OSError:
                if not self._stopping.is_set():
                    LOG.exception("Native frame accept failed")
                break
            LOG.info("Native camera producer connected")
            self.frames.set_native_connected(True)
            try:
                connection.settimeout(3.0)
                with connection, connection.makefile("rb", buffering=0) as stream:
                    self._consume(stream)
            except (OSError, ValueError) as exc:
                if not self._stopping.is_set():
                    LOG.warning("Native camera connection ended: %s", exc)
            finally:
                self.frames.set_native_connected(False, "native camera producer disconnected")

    def _consume(self, stream: BinaryIO) -> None:
        while not self._stopping.is_set():
            header = _read_exact(stream, FRAME_HEADER.size)
            if header is None:
                return
            magic, jpeg_length, width, height, timestamp_ns = FRAME_HEADER.unpack(header)
            if magic != FRAME_MAGIC:
                raise ValueError("invalid native frame magic")
            if not (1 <= jpeg_length <= MAX_JPEG_BYTES):
                raise ValueError(f"invalid JPEG frame length: {jpeg_length}")
            if width <= 0 or height <= 0:
                raise ValueError(f"invalid dimensions: {width}x{height}")
            jpeg = _read_exact(stream, jpeg_length)
            if jpeg is None:
                return
            if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                raise ValueError("native frame is not a complete JPEG")
            self.frames.publish(jpeg, width, height, timestamp_ns)


class NativeSupervisor:
    def __init__(self, executable: Path, socket_path: Path, config: Config, frames: FrameStore) -> None:
        self.executable = executable
        self.socket_path = socket_path
        self.config = config
        self.frames = frames
        self._stopping = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="native-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        process = self._process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                LOG.error("Native helper ignored SIGTERM; sending SIGKILL to our child process")
                process.kill()
                process.wait(timeout=2)
        if self._thread:
            self._thread.join(timeout=5)

    def _command(self) -> list[str]:
        return [
            str(self.executable),
            "--socket",
            str(self.socket_path),
            "--width",
            str(self.config.width),
            "--height",
            str(self.config.height),
            "--fps",
            str(self.config.fps),
            "--quality",
            str(self.config.jpeg_quality),
            "--rotation",
            str(self.config.rotation),
        ]

    def _run(self) -> None:
        while not self._stopping.is_set():
            if not self.executable.is_file():
                message = f"native helper not found: {self.executable}"
                LOG.error(message)
                self.frames.set_native_connected(False, message)
                return
            LOG.info("Starting native camera helper")
            try:
                self._process = subprocess.Popen(self._command())
                exit_code = self._process.wait()
            except OSError as exc:
                exit_code = -1
                LOG.error("Unable to start native helper: %s", exc)
            finally:
                self._process = None
            if self._stopping.is_set():
                return
            message = f"native helper exited with status {exit_code}"
            LOG.error("%s; restarting in %.1fs", message, self.config.native_restart_seconds)
            self.frames.set_native_connected(False, message)
            self._stopping.wait(self.config.native_restart_seconds)


class MJPEGServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], frames: FrameStore, stream_fps: int = 15) -> None:
        self.frames = frames
        self.stream_fps = stream_fps
        super().__init__(address, MJPEGHandler)


class MJPEGHandler(BaseHTTPRequestHandler):
    server: MJPEGServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = self.path.partition("?")[0]
        if path == "/":
            self._root()
        elif path == "/video":
            self._video()
        elif path == "/snapshot.jpg":
            self._snapshot()
        elif path == "/health":
            self._json(HTTPStatus.OK, self.server.frames.health())
        else:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def log_message(self, message_format: str, *args: object) -> None:
        LOG.info("http %s - %s", self.client_address[0], message_format % args)

    def _send_bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _root(self) -> None:
        body = b"<!doctype html><meta name=viewport content='width=device-width'><title>iPhone Camera</title><img src='/video' style='max-width:100%;height:auto'>"
        self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", body)

    def _snapshot(self) -> None:
        frame = self.server.frames.latest()
        if frame is None:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "camera frame unavailable"})
            return
        self._send_bytes(HTTPStatus.OK, "image/jpeg", frame.jpeg)

    def _video(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        # Keep the kernel queue below one typical JPEG. A large send buffer can
        # hide several stale frames when Wi-Fi is slower than the capture rate.
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024)
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        LOG.info(
            "MJPEG client connected: %s (send_buffer=%d)",
            self.client_address[0],
            self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF),
        )
        self.connection.settimeout(5.0)
        sequence = 0
        last_sent = 0.0
        frame_period = 1.0 / self.server.stream_fps
        try:
            while True:
                frame = self.server.frames.wait_for_new(sequence, timeout=2.0)
                if frame is None:
                    continue
                delay = last_sent + frame_period - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                    newest = self.server.frames.latest()
                    if newest is not None:
                        frame = newest
                sequence = frame.sequence
                server_send_ns = time.time_ns()
                header = (
                    f"--{BOUNDARY}\r\n"
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame.jpeg)}\r\n"
                    f"X-Sequence: {frame.sequence}\r\n"
                    f"X-Capture-Timestamp-Ns: {frame.capture_timestamp_ns}\r\n"
                    f"X-Server-Send-Timestamp-Ns: {server_send_ns}\r\n"
                    "\r\n"
                ).encode("ascii")
                self.wfile.write(header)
                self.wfile.write(frame.jpeg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                last_sent = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            LOG.info("MJPEG client disconnected: %s", self.client_address[0])


def _parse_arguments() -> argparse.Namespace:
    project_dir = default_project_dir()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, default=project_dir / "native" / "iphone-camera")
    parser.add_argument("--socket", type=Path, default=project_dir / "run" / "frames.sock")
    parser.add_argument(
        "--no-native",
        action="store_true",
        help="serve without spawning the command-line helper (foreground app mode)",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        help="receive app frames on 127.0.0.1:PORT instead of a Unix socket",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("IPHONE_MJPEG_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    args = _parse_arguments()
    config = Config()
    config.validate()
    frames = FrameStore(config.stale_after_seconds)
    tcp_address = ("127.0.0.1", args.tcp_port) if args.tcp_port else None
    receiver = NativeFrameReceiver(args.socket, frames, tcp_address)
    receiver.start()
    supervisor = None if args.no_native else NativeSupervisor(args.native, args.socket, config, frames)
    if supervisor:
        supervisor.start()
    server = MJPEGServer((config.host, config.port), frames, config.stream_fps)
    stopping = threading.Event()

    def request_stop(signal_number: int, current_frame: object) -> None:
        del signal_number, current_frame
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, name="http-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    LOG.info("Listening on %s:%d", config.host, config.port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        if supervisor:
            supervisor.stop()
        receiver.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
