#!/usr/bin/env python3
"""Manual TonyPi/OpenCV stream smoke test."""

from __future__ import annotations

import argparse
import time

import cv2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host", help="iPhone IP address or Tailnet hostname")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()
    url = f"http://{args.host}:{args.port}/video"
    cap = cv2.VideoCapture(url)
    previous = time.monotonic()
    while True:
        ok, frame = cap.read()
        if not ok:
            print("failed")
            cap.release()
            return 1
        now = time.monotonic()
        print(f"shape={frame.shape} inter_frame_ms={(now - previous) * 1000:.1f}")
        previous = now
        if not args.no_window:
            cv2.imshow("iphone", frame)
            if cv2.waitKey(1) == 27:
                break
    cap.release()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
