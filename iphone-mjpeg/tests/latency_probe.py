#!/usr/bin/env python3
"""Estimate capture-to-HTTP latency from MJPEG part timestamps.

The iPhone and client clocks must be synchronized. This intentionally parses
the multipart stream directly so any OpenCV/FFmpeg buffering can be evaluated
separately with a visual clock test.
"""

from __future__ import annotations

import argparse
import statistics
import time
import urllib.request


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host", help="iPhone IP address or Tailnet hostname")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--frames", type=int, default=100)
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/video"
    latencies: list[float] = []
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=10) as response:
        while len(latencies) < args.frames:
            line = response.readline()
            if not line:
                raise RuntimeError("MJPEG stream ended")
            if not line.startswith(b"--frame"):
                continue
            headers: dict[str, str] = {}
            while True:
                line = response.readline()
                if line in {b"\r\n", b"\n", b""}:
                    break
                name, value = line.decode("ascii").split(":", 1)
                headers[name.lower()] = value.strip()
            content_length = int(headers["content-length"])
            capture_ns = int(headers["x-capture-timestamp-ns"])
            payload = response.read(content_length)
            if len(payload) != content_length:
                raise RuntimeError("short JPEG payload")
            latency_ms = (time.time_ns() - capture_ns) / 1_000_000
            latencies.append(latency_ms)

    elapsed = time.monotonic() - started
    print(
        f"frames={len(latencies)} receive_fps={len(latencies) / elapsed:.1f} "
        f"latency_ms p50={statistics.median(latencies):.1f} "
        f"p95={percentile(latencies, 0.95):.1f} max={max(latencies):.1f}"
    )
    if min(latencies) < -50:
        print("warning: client and iPhone clocks appear unsynchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
