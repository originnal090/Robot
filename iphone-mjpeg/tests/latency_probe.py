#!/usr/bin/env python3
"""Estimate capture-to-HTTP latency from MJPEG part timestamps.

The iPhone and client clocks must be synchronized. This intentionally parses
the multipart stream directly so any OpenCV/FFmpeg buffering can be evaluated
separately with a visual clock test.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


def estimate_clock_offset(host: str, port: int, samples: int = 7) -> tuple[int, float]:
    """Return iPhone-minus-client nanoseconds and best HTTP RTT in milliseconds."""
    measurements: list[tuple[int, int]] = []
    url = f"http://{host}:{port}/health"
    for _ in range(samples):
        before = time.time_ns()
        with urllib.request.urlopen(url, timeout=3) as response:
            health = json.load(response)
        after = time.time_ns()
        midpoint = (before + after) // 2
        measurements.append((after - before, int(health["server_time_ns"]) - midpoint))
    best_rtt, offset = min(measurements)
    return offset, best_rtt / 1_000_000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host", help="iPhone IP address or Tailnet hostname")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--frames", type=int, default=100)
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/video"
    clock_offset_ns, calibration_rtt_ms = estimate_clock_offset(args.host, args.port)
    latencies: list[float] = []
    capture_to_send: list[float] = []
    wire_latencies: list[float] = []
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
            server_send_ns = int(headers["x-server-send-timestamp-ns"])
            payload = response.read(content_length)
            if len(payload) != content_length:
                raise RuntimeError("short JPEG payload")
            received_ns = time.time_ns()
            latency_ms = (received_ns + clock_offset_ns - capture_ns) / 1_000_000
            latencies.append(latency_ms)
            capture_to_send.append((server_send_ns - capture_ns) / 1_000_000)
            wire_latencies.append((received_ns + clock_offset_ns - server_send_ns) / 1_000_000)

    elapsed = time.monotonic() - started
    print(
        f"frames={len(latencies)} receive_fps={len(latencies) / elapsed:.1f} "
        f"clock_offset_ms={clock_offset_ns / 1_000_000:.1f} "
        f"calibration_rtt_ms={calibration_rtt_ms:.1f} "
        f"latency_ms p50={statistics.median(latencies):.1f} "
        f"p95={percentile(latencies, 0.95):.1f} max={max(latencies):.1f} "
        f"capture_to_send={statistics.median(capture_to_send):.1f} "
        f"wire={statistics.median(wire_latencies):.1f} "
        f"first10={statistics.median(latencies[:10]):.1f} "
        f"last10={statistics.median(latencies[-10:]):.1f}"
    )
    if min(latencies) < -(calibration_rtt_ms / 2 + 10):
        print("warning: clock-offset calibration may be inaccurate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
