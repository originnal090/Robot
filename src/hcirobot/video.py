from __future__ import annotations

import contextlib
import http.client
import socket
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
        # Poll in short slices so close() from another thread is noticed promptly:
        # on Windows a blocking recv is not woken by shutdown() from another thread.
        sock = self._detach_socket(self._response)
        if sock is not None:
            sock.settimeout(min(timeout_seconds, self._POLL_SECONDS))

    def __iter__(self) -> Iterator[Frame]:
        buffer = bytearray()
        while True:
            chunk = self._read_chunk()
            if not chunk:
                return  # EOF, timeout, or close() from another thread ends the stream.
            buffer.extend(chunk)
            while True:
                start = buffer.find(self._SOI)
                if start < 0:
                    del buffer[:-1]  # keep one byte: SOI may straddle the chunk edge
                    break
                end = buffer.find(self._EOI, start + len(self._SOI))
                if end < 0:
                    del buffer[:start]  # keep everything from the SOI onward
                    break
                jpeg = bytes(buffer[start : end + len(self._EOI)])
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
        sock = self._detach_socket(response)
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
        deadline = time.monotonic() + self._timeout
        while True:
            with self._lock:
                response = self._response
            if response is None:
                return b""  # close() from another thread ends the stream.
            try:
                chunk = response.read1(self._CHUNK_BYTES)
            except TimeoutError:
                if time.monotonic() >= deadline:
                    return b""  # stream stalled longer than the read timeout
                continue
            except (OSError, ValueError, EOFError, AttributeError, http.client.HTTPException):
                return b""  # reset, truncated body, or closed underneath us
            return chunk if chunk else b""  # b"" means clean EOF

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


def annotate(frame: Frame, detection, decision) -> Frame:
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
        f"v={decision.command.velocity:.2f} steer={decision.command.steer:.2f}",
        f"detected={detection.detected} candidate={detection.candidate_detected}",
    ]
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
