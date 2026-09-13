"""Interactive raw-frame capture for sample collection.

The recorder taps the video-capture thread directly (before the control
loop's latest-wins queue), so every frame the source produces is offered for
saving even when detection/control run slower than the camera.  offer() never
blocks: a bounded queue feeds a background writer thread, and overflow is
counted as dropped frames instead of stalling the capture path.
"""

from __future__ import annotations

import contextlib
import itertools
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

Frame = NDArray[np.uint8]

_MAX_CONSECUTIVE_FAILURES = 5


class FrameRecorder:
    """Save offered frames as PNGs; start/stop from any thread, idempotent."""

    def __init__(
        self,
        *,
        queue_size: int = 120,
        root: Path | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._queue_size = max(2, queue_size)
        self._root = root or Path("artifacts") / "captures"
        self._on_log = on_log
        self._lock = threading.Lock()
        self._active = False
        self._directory: Path | None = None
        self._writer: threading.Thread | None = None
        self._pending: queue.Queue | None = None
        self._stop_writer = threading.Event()
        self._counter = itertools.count(1)
        self._saved = 0
        self._dropped = 0
        self._failed = 0

    # ---------- session lifecycle ----------

    def start_session(self, directory: Path | None = None) -> Path:
        """Begin recording into ``directory`` (auto-timestamped under root)."""
        with self._lock:
            if self._active and self._directory is not None:
                return self._directory
            if directory is None:
                stamp = time.strftime("capture-%Y%m%d-%H%M%S")
                target = self._root / stamp
                suffix = 2
                while target.exists():  # restart within the same second
                    target = self._root / f"{stamp}-{suffix}"
                    suffix += 1
            else:
                target = directory
            target.mkdir(parents=True, exist_ok=True)
            self._directory = target
            self._active = True
            self._saved = 0
            self._dropped = 0
            self._failed = 0
            self._counter = itertools.count(1)
            self._pending = queue.Queue(maxsize=self._queue_size)
            self._stop_writer = threading.Event()
            self._writer = threading.Thread(
                target=self._writer_loop, name="frame-recorder", daemon=True
            )
            self._writer.start()
            return target

    def stop_session(self) -> tuple[int, int, str]:
        """Stop recording, flush the writer, and return (saved, dropped, path)."""
        with self._lock:
            active = self._active
            directory = self._directory
            writer = self._writer
        if not active or writer is None:
            with self._lock:
                return self._saved, self._dropped, str(directory or "")
        self._stop_writer.set()
        writer.join(timeout=10.0)
        with self._lock:
            self._active = False
        self._log(f"样本采集结束：{self._saved} 帧已保存，丢弃 {self._dropped} 帧 → {directory}")
        return self._saved, self._dropped, str(directory)

    def offer(self, frame: Frame) -> None:
        """Hand one frame to the writer; never blocks, drops on overflow."""
        with self._lock:
            pending = self._pending if self._active else None
        if pending is None:
            return
        try:
            pending.put_nowait(frame)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def snapshot(self) -> tuple[bool, int, int, str]:
        """Current state for UI display: (active, saved, dropped, path)."""
        with self._lock:
            return self._active, self._saved, self._dropped, str(self._directory or "")

    # ---------- internals ----------

    def _writer_loop(self) -> None:
        consecutive_failures = 0
        while True:
            pending = self._pending
            directory = self._directory
            if pending is None or directory is None:
                return
            try:
                frame = pending.get(timeout=0.05)
            except queue.Empty:
                if self._stop_writer.is_set():
                    return
                continue
            index = next(self._counter)
            try:
                written = cv2.imwrite(str(directory / f"frame-{index:05d}.png"), frame)
            except Exception:  # noqa: BLE001 - disk errors degrade, not crash.
                written = False
            with self._lock:
                if written:
                    self._saved += 1
                    consecutive_failures = 0
                else:
                    self._failed += 1
                    consecutive_failures += 1
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self._log(
                    f"样本采集写入连续失败 {consecutive_failures} 次，"
                    "本次采集提前停止（磁盘满或路径无效？）"
                )
                with contextlib.suppress(queue.Empty, queue.Full):
                    while pending.get_nowait() is not None:
                        pass
                return

    def _log(self, message: str) -> None:
        callback = self._on_log
        if callback is None:
            return
        with contextlib.suppress(Exception):
            callback(message)
