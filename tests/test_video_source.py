"""Tests for the hand-rolled MJPEG HTTP source (FFmpeg-free decoding path)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import cv2
import numpy as np
import pytest

from hcirobot.video import MjpegHttpSource

_SIZE = (48, 64)


def make_jpeg(color_bgr: tuple[int, int, int]) -> bytes:
    image = np.full((*_SIZE, 3), color_bgr, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def assert_frame_matches(frame: np.ndarray, color_bgr: tuple[int, int, int]) -> None:
    assert frame.shape == (*_SIZE, 3)
    assert frame.dtype == np.uint8
    center = frame[_SIZE[0] // 2, _SIZE[1] // 2]
    for index in range(3):
        assert abs(int(center[index]) - color_bgr[index]) <= 12


class _StreamHandler(BaseHTTPRequestHandler):
    """Serves configured byte segments as a single multipart MJPEG response."""

    frames: ClassVar[list[bytes]] = []
    prefix: bytes = b""
    infix: bytes = b""
    suffix: bytes = b""
    hold_open: threading.Event | None = None
    abrupt: bool = False
    chunked: bool = False

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        if self.chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(self.prefix)
        for index, frame in enumerate(self.frames):
            if index:
                self.wfile.write(self.infix)
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        self.wfile.write(self.suffix)
        self.wfile.flush()
        if self.hold_open is not None:
            self.hold_open.wait(5.0)
        if self.abrupt:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def log_message(self, format: str, *args: object) -> None:
        return None


@contextmanager
def mjpeg_server(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/?action=stream"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_parses_frames_and_content_from_multipart_stream() -> None:
    colors = [(0, 0, 255), (0, 255, 0), (255, 255, 255)]

    class Handler(_StreamHandler):
        frames: ClassVar[list[bytes]] = [make_jpeg(color) for color in colors]

    with mjpeg_server(Handler) as url:
        source = MjpegHttpSource(url, timeout_seconds=2.0)
        try:
            decoded = list(source)
        finally:
            source.close()

    assert len(decoded) == len(colors)
    for frame, color in zip(decoded, colors, strict=True):
        assert_frame_matches(frame, color)


def test_preserves_body_prefetched_with_response_headers() -> None:
    frame = make_jpeg((64, 128, 255))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(4096)
            body = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}/stream"
    source = MjpegHttpSource(url, timeout_seconds=2.0)
    try:
        decoded = list(source)
    finally:
        source.close()
        listener.close()
        thread.join(timeout=2.0)

    assert len(decoded) == 1
    assert_frame_matches(decoded[0], (64, 128, 255))


def test_close_unblocks_pending_read_from_another_thread() -> None:
    class Handler(_StreamHandler):
        frames: ClassVar[list[bytes]] = [make_jpeg((0, 128, 255))]
        hold_open = threading.Event()  # never set: server stays silent after frame 1

    with mjpeg_server(Handler) as url:
        source = MjpegHttpSource(url, timeout_seconds=5.0)
        try:
            iterator = iter(source)
            first = next(iterator)
            assert_frame_matches(first, (0, 128, 255))
            closer = threading.Timer(0.2, source.close)
            closer.start()
            started = time.monotonic()
            remaining = list(iterator)  # blocked in read until close() fires
            elapsed = time.monotonic() - started
            closer.join(timeout=2.0)
        finally:
            Handler.hold_open.set()
            source.close()

    assert remaining == []
    assert elapsed < 3.0  # would be ~5s (socket read timeout) without close()


def test_stalled_stream_recovers_after_read_timeout() -> None:
    """A stall longer than the socket timeout must not end the stream.

    Regression: reading through the buffered HTTPResponse bricked it after one
    timeout ("cannot read from timed out object"), so a live camera with frame
    gaps died after its first frame. The raw-socket reader must keep polling.
    """
    first = make_jpeg((0, 0, 255))
    late = make_jpeg((0, 255, 0))
    released = threading.Event()

    class Handler(_StreamHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + first + b"\r\n")
            self.wfile.flush()
            released.wait(5.0)  # silence longer than the 0.2 s read timeout
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + late + b"\r\n")
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            return None

    with mjpeg_server(Handler) as url:
        source = MjpegHttpSource(url, timeout_seconds=0.2)
        try:
            iterator = iter(source)
            assert_frame_matches(next(iterator), (0, 0, 255))
            second = next(iterator)  # must survive the stall and deliver the late frame
        finally:
            released.set()
            source.close()

    assert_frame_matches(second, (0, 255, 0))


def test_server_disconnect_ends_iteration_normally() -> None:
    colors = [(255, 255, 255), (0, 0, 0)]

    class Handler(_StreamHandler):
        frames: ClassVar[list[bytes]] = [make_jpeg(color) for color in colors]
        suffix = b"\xff\xd8" + b"\x11\x22\x33" * 16  # truncated frame, then hard cut
        abrupt = True

    with mjpeg_server(Handler) as url:
        source = MjpegHttpSource(url, timeout_seconds=2.0)
        try:
            decoded = list(source)
        finally:
            source.close()

    assert len(decoded) == 2
    for frame, color in zip(decoded, colors, strict=True):
        assert_frame_matches(frame, color)


def test_malformed_segments_are_skipped_without_raising() -> None:
    colors = [(0, 255, 255), (255, 0, 255)]
    garbage = b"leading junk without any jpeg markers \x00\x01\x02"
    fake = b"\xff\xd8" + bytes(64) + b"\xff\xd9"  # valid markers, undecodable body

    class Handler(_StreamHandler):
        frames: ClassVar[list[bytes]] = [make_jpeg(color) for color in colors]
        prefix = garbage
        infix = garbage + fake + garbage
        suffix = fake

    with mjpeg_server(Handler) as url:
        source = MjpegHttpSource(url, timeout_seconds=2.0)
        try:
            decoded = list(source)
        finally:
            source.close()

    assert len(decoded) == 2
    for frame, color in zip(decoded, colors, strict=True):
        assert_frame_matches(frame, color)


def test_rejects_chunked_transfer_encoding_explicitly() -> None:
    class Handler(_StreamHandler):
        chunked = True

    with mjpeg_server(Handler) as url, pytest.raises(ConnectionError, match="chunked MJPEG"):
        MjpegHttpSource(url, timeout_seconds=1.0)


def test_oversized_truncated_candidate_recovers_at_later_jpeg() -> None:
    color = (255, 128, 0)

    class Handler(_StreamHandler):
        frames: ClassVar[list[bytes]] = [make_jpeg(color)]
        prefix = b"\xff\xd8" + bytes(MjpegHttpSource._MAX_JPEG_BYTES + 1024)

    with mjpeg_server(Handler) as url:
        source = MjpegHttpSource(url, timeout_seconds=2.0)
        try:
            decoded = list(source)
        finally:
            source.close()

    assert len(decoded) == 1
    assert_frame_matches(decoded[0], color)


def test_open_times_out_against_silent_server() -> None:
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            release.wait(3.0)  # accept the request but never answer

        def log_message(self, format: str, *args: object) -> None:
            return None

    with mjpeg_server(Handler) as url:
        started = time.monotonic()
        with pytest.raises(ConnectionError):
            MjpegHttpSource(url, timeout_seconds=0.25)
        elapsed = time.monotonic() - started
    release.set()

    assert 0.2 <= elapsed < 2.0


def test_open_failure_raises_connection_error() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    with pytest.raises(ConnectionError):
        MjpegHttpSource(f"http://127.0.0.1:{port}/?action=stream", timeout_seconds=1.0)


# ---------- RetryingSource ----------

import threading as _threading


class FlakyStream:
    """Yields two frames then breaks: by exception (first life) and EOF (later)."""

    lives = 0

    def __iter__(self):
        type(self).lives += 1
        yield np.zeros((8, 8, 3), dtype=np.uint8)
        yield np.zeros((8, 8, 3), dtype=np.uint8)
        if type(self).lives == 1:
            raise ConnectionError("wifi hiccup")
        # later lives end with a clean EOF (which a live stream retries)


def test_retrying_source_recovers_from_errors_and_eof() -> None:
    from hcirobot.video import RetryingSource

    notes: list[str] = []
    source = RetryingSource(lambda: FlakyStream(), on_event=notes.append)
    frames = []
    for frame in source:
        frames.append(frame)
        if len(frames) == 4:
            break  # EOF retries forever on live streams; stop consuming here
    source.close()
    assert len(frames) == 4  # two frames per connection, two connections
    assert FlakyStream.lives == 2
    assert any("中断" in note for note in notes)
    assert any("已恢复" in note for note in notes)


def test_retrying_source_open_failure_retries_and_close_stops() -> None:
    from hcirobot.video import RetryingSource

    attempts = {"count": 0}

    def failing_factory():
        attempts["count"] += 1
        raise ConnectionError("camera down")

    source = RetryingSource(failing_factory)
    iterator = iter(source)
    thread = _threading.Thread(target=lambda: [None for _ in iterator], daemon=True)
    thread.start()
    time.sleep(0.6)
    source.close()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert attempts["count"] >= 2


class StallingSource:
    """Three frames, a stall longer than the frame timeout, then more frames.

    close() is interruptible (sliced sleep) like the real MJPEG source.
    """

    def __init__(self, stall_seconds: float = 1.2) -> None:
        self._closed = _threading.Event()
        self._stall_seconds = stall_seconds

    def __iter__(self):
        for _ in range(3):
            yield np.zeros((48, 64, 3), dtype=np.uint8)
        slices = int(self._stall_seconds / 0.05)
        for _ in range(slices):
            if self._closed.is_set():
                return
            time.sleep(0.05)
        for _ in range(3):
            yield np.zeros((48, 64, 3), dtype=np.uint8)
        while not self._closed.is_set():
            time.sleep(0.05)

    def close(self) -> None:
        self._closed.set()


def test_run_loop_video_retry_keeps_preview_alive_through_stall() -> None:
    from hcirobot.app import SessionControl, run_loop
    from hcirobot.controller import ControllerConfig, VisualApproachController
    from hcirobot.detector import DetectorConfig, RedBallDetector
    from hcirobot.robot import RecordingRobot
    from hcirobot.video import RetryingSource

    session = SessionControl()
    lives = {"count": 0}

    def factory():
        lives["count"] += 1
        return StallingSource()

    def sink(event):
        if event.kind == "frame" and event.frame_count >= 6:
            session.request_stop()

    result = run_loop(
        RetryingSource(factory),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig()),
        RecordingRobot(),
        armed=False,
        frame_timeout_seconds=0.5,
        session=session,
        event_sink=sink,
        video_retry=True,
    )
    assert result.termination == "stop_requested"  # survived the 1.2 s stall
    assert result.frames >= 6

    # Without the flag the same stall ends an unarmed preview session.
    session = SessionControl()
    result = run_loop(
        StallingSource(),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig()),
        RecordingRobot(),
        armed=False,
        frame_timeout_seconds=0.5,
        session=session,
        event_sink=lambda _event: None,
        video_retry=False,
    )
    assert result.termination == "preview_timeout"
