#!/usr/bin/env python3
"""Collect timestamped JPEG training frames from the iPhone MJPEG stream."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import urllib.request
import uuid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host", help="iPhone LAN IP or Tailnet hostname")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--output", type=Path, default=Path("data/iphone-camera"))
    parser.add_argument("--fps", type=float, default=2.0, help="saved frames per second")
    parser.add_argument("--seconds", type=float, default=0.0, help="0 records until Ctrl+C")
    parser.add_argument("--frames", type=int, default=0, help="0 has no frame-count limit")
    args = parser.parse_args()
    if args.fps <= 0 or args.seconds < 0 or args.frames < 0:
        parser.error("fps must be positive; seconds and frames cannot be negative")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    session = args.output / f"session-{stamp}-{uuid.uuid4().hex[:6]}"
    session.mkdir(parents=True)
    metadata = {
        "schema_version": 1,
        "created_at": time.time(),
        "source": f"http://{args.host}:{args.port}/video",
        "target_fps": args.fps,
        "format": "jpeg",
    }
    (session / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    interval_ns = int(1_000_000_000 / args.fps)
    last_saved_ns = 0
    saved = 0
    started = time.monotonic()
    url = f"http://{args.host}:{args.port}/video"
    print(f"saving to {session.resolve()}")
    try:
        with urllib.request.urlopen(url, timeout=10) as response, (
            session / "frames.jsonl"
        ).open("a", encoding="utf-8") as manifest:
            while True:
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
                length = int(headers["content-length"])
                payload = response.read(length)
                if len(payload) != length:
                    raise RuntimeError("short JPEG payload")
                capture_ns = int(headers.get("x-capture-timestamp-ns", time.time_ns()))
                if capture_ns - last_saved_ns < interval_ns:
                    continue
                if not (payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9")):
                    raise RuntimeError("invalid JPEG markers")
                filename = f"frame-{capture_ns:020d}-{saved + 1:08d}.jpg"
                (session / filename).write_bytes(payload)
                record = {
                    "file": filename,
                    "timestamp_ns": capture_ns,
                    "sequence": int(headers.get("x-sequence", saved + 1)),
                    "bytes": length,
                }
                manifest.write(json.dumps(record, separators=(",", ":")) + "\n")
                manifest.flush()
                saved += 1
                last_saved_ns = capture_ns
                if saved == 1 or saved % 25 == 0:
                    print(f"saved={saved} elapsed={time.monotonic() - started:.1f}s")
                if args.frames and saved >= args.frames:
                    break
                if args.seconds and time.monotonic() - started >= args.seconds:
                    break
    except KeyboardInterrupt:
        print("capture stopped by user")

    print(f"complete: {saved} frames in {session.resolve()}")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
