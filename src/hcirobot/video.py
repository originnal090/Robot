from __future__ import annotations

import contextlib
import http.client
import select
import socket
import ssl
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

Frame = NDArray[np.uint8]


class OpenCvVideoSource:
    def __init__(self, source: str | int, timeout_seconds: float = 0.75) -> None:
        self.source = source
        self._capture = cv2.VideoCapture()
        timeout_ms = max(1, round(timeout_seconds * 1000))
        if isinstance(source, str) and source.startswith(("http://", "https://")):
            self._capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
            self._capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)
        if not self._capture.open(source):
            self._capture.release()
            raise ConnectionError(f"cannot open video source: {source}")

    def __iter__(self) -> Iterator[Frame]:
        while True:
            ok, frame = self._capture.read()
            if not ok or frame is None:
                return
            yield frame

    def close(self) -> None:
        self._capture.release()


class MjpegHttpSource:
    """MJPEG-over-HTTP source that bypasses OpenCV's FFmpeg backend.

    FFmpeg's demuxer aborts the whole process (C-level assert in
    libavformat/demux.c) on some streams served by robot cameras, so the
    multipart/x-mixed-replace body is scanned manually for JPEG SOI/EOI
    markers and every frame is decoded with cv2.imdecode instead.
    """

    _SOI = b"\xff\xd8"
    _EOI = b"\xff\xd9"
    _CHUNK_BYTES = 4096
    _POLL_SECONDS = 0.05
    _MAX_JPEG_BYTES = 8 * 1024 * 1024
    _USER_AGENT = "hcirobot-mjpeg/1.0 (python-urllib)"

    def __init__(self, url: str, timeout_seconds: float = 0.75) -> None:
        self.url = url
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._response: http.client.HTTPResponse | None = None
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self._USER_AGENT,
                "Accept": "multipart/x-mixed-replace, image/jpeg, */*",
            },
        )
        try:
            self._response = urllib.request.urlopen(request, timeout=timeout_seconds)
        except OSError as exc:  # URLError and socket timeouts both subclass OSError.
            raise ConnectionError(f"cannot open MJPEG stream: {url}: {exc}") from exc
        transfer_encodings = {
            value.strip().lower()
            for value in self._response.headers.get("Transfer-Encoding", "").split(",")
        }
        if "chunked" in transfer_encodings:
            self._response.close()
            self._response = None
            raise ConnectionError("chunked MJPEG transfer encoding is not supported")
        # urllib may read past the HTTP headers into BufferedReader.peek()/buffer.
        # Mixing that prefetched body with recv() on the underlying socket loses or
        # reorders bytes. Drain the buffered layer first, then use non-blocking raw
        # socket reads so close() remains responsive and timeout polls are reusable.
        self._socket = self._detach_socket(self._response)
        if self._socket is not None:
            self._socket.setblocking(False)
        self._prefetched = self._take_prefetched_body(self._response)

    def __iter__(self) -> Iterator[Frame]:
        buffer = bytearray()
        while True:
            chunk = self._read_chunk()
            if not chunk:
                return  # EOF or close() from another thread ends the stream.
            buffer.extend(chunk)
            while True:
                start = buffer.find(self._SOI)
                if start < 0:
                    del buffer[:-1]  # keep one byte: SOI may straddle the chunk edge
                    break
                if start:
                    del buffer[:start]
                end = buffer.find(self._EOI, len(self._SOI))
                if end < 0:
                    if len(buffer) > self._MAX_JPEG_BYTES:
                        # A bogus/missing EOI must not grow forever. Prefer the
                        # newest SOI as a recovery point; otherwise discard the
                        # oversized candidate while preserving a split marker.
                        next_start = buffer.rfind(self._SOI, len(self._SOI))
                        if next_start > 0:
                            del buffer[:next_start]
                        else:
                            del buffer[:-1]
                    break
                if end + len(self._EOI) > self._MAX_JPEG_BYTES:
                    # The first EOI may belong to a valid JPEG embedded after an
                    # unterminated oversized candidate. Restart from its newest
                    # nested SOI instead of discarding that recoverable frame.
                    next_start = buffer.rfind(self._SOI, len(self._SOI), end)
                    if next_start > 0:
                        del buffer[:next_start]
                        continue
                    del buffer[: end + len(self._EOI)]
                    continue
                jpeg = bytes(buffer[: end + len(self._EOI)])
                del buffer[: end + len(self._EOI)]
                frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    continue  # corrupt segment: skip it and keep the stream alive
                yield frame

    def close(self) -> None:
        """Close the stream; safe to call from another thread while iterating."""
        with self._lock:
            response = self._response
            self._response = None
        if response is None:
            return
        sock = self._socket or self._detach_socket(response)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # tear the TCP connection down now
            except OSError:
                pass
        with contextlib.suppress(OSError, ValueError, http.client.HTTPException):
            response.close()  # best-effort, like the other sources
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _read_chunk(self) -> bytes:
        # A live robot camera (or an unfocused Unity editor) routinely stalls
        # for longer than one frame interval. Stall policy belongs to
        # run_loop's frame timeout, which triggers the safe stop and closes
        # this source; here a timeout just means "poll again".
        while True:
            with self._lock:
                response = self._response
                if self._prefetched:
                    chunk = bytes(self._prefetched[: self._CHUNK_BYTES])
                    del self._prefetched[: self._CHUNK_BYTES]
                    return chunk
            if response is None:
                return b""  # close() from another thread ends the stream.
            sock = self._socket
            if sock is not None:
                try:
                    readable, _, _ = select.select([sock], [], [], self._POLL_SECONDS)
                except (OSError, ValueError):
                    return b""
                if not readable:
                    continue
                try:
                    return sock.recv(self._CHUNK_BYTES)
                except (BlockingIOError, ssl.SSLWantReadError):
                    continue
                except OSError:
                    return b""  # reset, truncated body, or closed underneath us
            try:
                chunk = response.read1(self._CHUNK_BYTES)
            except (OSError, ValueError, EOFError, AttributeError, http.client.HTTPException):
                return b""  # reset, truncated body, or closed underneath us
            return chunk if chunk else b""  # b"" means clean EOF

    @classmethod
    def _take_prefetched_body(cls, response: http.client.HTTPResponse) -> bytearray:
        file_object = getattr(response, "fp", None)
        peek = getattr(file_object, "peek", None)
        if peek is None:
            return bytearray()
        try:
            # With the socket non-blocking, peek() only exposes bytes already
            # held by the HTTP BufferedReader; read1() drains those bytes without
            # waiting to fill the request from the raw socket.
            available = peek(cls._CHUNK_BYTES)
            if not available:
                return bytearray()
            return bytearray(file_object.read1(len(available)))
        except (OSError, ValueError, AttributeError, http.client.HTTPException):
            return bytearray()

    @staticmethod
    def _detach_socket(response: http.client.HTTPResponse) -> socket.socket | None:
        file_object = getattr(response, "fp", None)
        raw = getattr(file_object, "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is None:
            sock = getattr(file_object, "_sock", None)
        return sock if isinstance(sock, socket.socket) else None


@dataclass(frozen=True, slots=True)
class SyntheticConfig:
    width: int = 640
    height: int = 480
    fps: float = 10.0
    realtime: bool = False


class SyntheticBallSource:
    """Deterministic target sequence for hardware-free acceptance tests."""

    def __init__(self, config: SyntheticConfig | None = None) -> None:
        self.config = config or SyntheticConfig()
        lab = np.array([[[120, 170, 135]]], dtype=np.uint8)
        self._ball_bgr = tuple(int(value) for value in cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)[0, 0])

    def __iter__(self) -> Iterator[Frame]:
        scenarios: list[tuple[int, float | None, int]] = [
            (8, None, 0),
            (8, 0.22, 35),
            (6, 0.38, 35),
            (6, 0.48, 35),
            (8, 0.50, 45),
            (8, 0.50, 65),
            (8, 0.50, 88),
        ]
        period = 1.0 / max(self.config.fps, 0.1)
        for count, x_ratio, radius in scenarios:
            for _ in range(count):
                frame = np.full((self.config.height, self.config.width, 3), 32, dtype=np.uint8)
                if x_ratio is not None:
                    center = (round(self.config.width * x_ratio), self.config.height // 2)
                    cv2.circle(frame, center, radius, self._ball_bgr, -1, lineType=cv2.LINE_8)
                yield frame
                if self.config.realtime:
                    time.sleep(period)

    def close(self) -> None:
        return None


def annotate(
    frame: Frame,
    detection,
    decision,
    *,
    output_v: float | None = None,
    output_steer: float | None = None,
    output_source: str | None = None,
    distance_mm: float | None = None,
    obstacle_state: str | None = None,
) -> Frame:
    output = frame.copy()
    height, width = output.shape[:2]
    center_x = width // 2
    cv2.line(output, (center_x, 0), (center_x, height), (180, 180, 180), 1)
    if detection.candidate_detected and detection.center_x is not None:
        center = (round(detection.center_x), round(detection.center_y))
        cv2.circle(output, center, round(detection.radius), (0, 255, 0), 2)
        cv2.circle(output, center, 3, (0, 255, 255), -1)
    lines = [
        f"state={decision.state.value} reason={decision.reason}",
        f"intent v={decision.command.velocity:+.2f} steer={decision.command.steer:+.2f}",
    ]
    if output_v is not None and output_steer is not None:
        source = f" [{output_source}]" if output_source else ""
        lines.append(f"actual v={output_v:+.2f} steer={output_steer:+.2f}{source}")
    if distance_mm is not None:
        state = f" obstacle={obstacle_state}" if obstacle_state else ""
        lines.append(f"dist={distance_mm:.0f}mm{state}")
    lines.append(f"detected={detection.detected} candidate={detection.candidate_detected}")
    for index, text in enumerate(lines):
        cv2.putText(
            output,
            text,
            (10, 25 + index * 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2,
        )
    return output


class RetryingSource:
    """Reopen the underlying video source on EOF/error instead of ending the session.

    Built for live MJPEG over flaky WiFi: the control session stays up while
    the source quietly reconnects (bounded backoff).  Safety is preserved
    upstream: an armed session still stops safely through run_loop's frame
    timeout, and the GUI opts preview sessions out of preview-timeout via
    ``video_retry`` so only user stop ends them.
    """

    _INITIAL_BACKOFF_S = 0.2
    _MAX_BACKOFF_S = 3.0

    def __init__(self, factory, *, on_event=None):
        self._factory = factory
        self._on_event = on_event
        self._source = None
        self._closed = False
        self._failures = 0

    def __iter__(self) -> Iterator[Frame]:
        backoff = self._INITIAL_BACKOFF_S
        while not self._closed:
            if self._source is None:
                try:
                    self._source = self._factory()
                except Exception as exc:  # noqa: BLE001 - open failures retry too.
                    self._note(f"打开视频源失败（{type(exc).__name__}: {exc}）")
                    if not self._sleep(backoff):
                        return
                    backoff = min(backoff * 2, self._MAX_BACKOFF_S)
                    continue
            try:
                for frame in self._source:
                    if self._closed:
                        return
                    if self._failures:
                        count = self._failures
                        self._failures = 0
                        self._emit(f"视频已恢复（此前中断 {count} 次）")
                    backoff = self._INITIAL_BACKOFF_S
                    yield frame
                self._source = None  # clean EOF: the server rotated the stream
                self._note("视频流结束")
                if not self._sleep(backoff):
                    return
                backoff = min(backoff * 2, self._MAX_BACKOFF_S)
            except Exception as exc:  # noqa: BLE001 - stream break: reconnect.
                with contextlib.suppress(Exception):
                    self._source.close()
                self._source = None
                self._note(f"视频中断（{type(exc).__name__}: {exc}）")
                if not self._sleep(backoff):
                    return
                backoff = min(backoff * 2, self._MAX_BACKOFF_S)

    def close(self) -> None:
        self._closed = True
        source, self._source = self._source, None
        if source is not None:
            with contextlib.suppress(Exception):
                source.close()

    def _sleep(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self._closed and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self._closed

    def _note(self, message: str) -> None:
        self._failures += 1
        if self._failures == 1 or self._failures % 5 == 0:
            self._emit(f"{message}，自动重连中（第 {self._failures} 次）")

    def _emit(self, message: str) -> None:
        callback = self._on_event
        if callback is not None:
            with contextlib.suppress(Exception):
                callback(message)
