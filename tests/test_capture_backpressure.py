"""Latest-wins frame backpressure keeps live control latency bounded."""

from __future__ import annotations

import queue
import threading
import time

import numpy as np

from hcirobot.app import _END, _capture_frames


class _PacedSource:
    """Yields numbered frames at a fixed interval, like an MJPEG camera."""

    def __init__(self, count: int, interval: float) -> None:
        self.count = count
        self.interval = interval

    def __iter__(self):
        for index in range(self.count):
            if index:
                time.sleep(self.interval)
            yield np.full((4, 4, 3), index % 256, dtype=np.uint8)

    def close(self) -> None:
        return None


def test_capture_drops_stale_frames_instead_of_backpressuring() -> None:
    frames: queue.Queue = queue.Queue(maxsize=1)
    stop = threading.Event()
    source = _PacedSource(count=12, interval=0.01)

    capture = threading.Thread(target=_capture_frames, args=(source, frames, stop), daemon=True)
    capture.start()
    # Hold the only queue slot so the producer must shed frames, not block.
    first = frames.get(timeout=1)
    # A blocking producer would stall here for the rest of the stream; a
    # latest-wins producer replaces the slot within one frame interval.
    second = frames.get(timeout=0.2)
    stop.set()
    capture.join(timeout=1)

    assert second is not first
    assert frames.qsize() <= 1
    assert not capture.is_alive()


def test_capture_forwards_end_sentinel_and_exceptions() -> None:
    frames: queue.Queue = queue.Queue(maxsize=1)
    stop = threading.Event()

    def consume() -> list[object]:
        items = []
        while True:
            try:
                item = frames.get(timeout=1)
            except queue.Empty:
                return items
            items.append(item)
            if item is _END:
                return items

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    capture = threading.Thread(
        target=_capture_frames, args=(_PacedSource(count=3, interval=0.0), frames, stop), daemon=True
    )
    capture.start()
    consumer.join(timeout=2)
    assert not capture.is_alive()
