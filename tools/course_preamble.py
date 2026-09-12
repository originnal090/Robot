#!/usr/bin/env python3
"""Acceptance preamble: drive forward, turn right ~90 deg, then hand over.

The course acceptance scenario starts the robot at the firefighter's default
spot, walks it forward, turns right, and only then expects the ball (on the
cart) to enter view for the autonomous search-align-approach loop. This script
performs that scripted pre-positioning over the same TCP 5075 channel the
controller uses, then disconnects so autonomy can take the single-client slot.

Only standard library; no Unity install required.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5075


def send_vector(sock: socket.socket, v: float, steer: float) -> None:
    payload = {"v": round(v, 4), "steer": round(steer, 4), "grab": False,
               "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    sock.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())


def drive(sock: socket.socket, *, v: float, steer: float, seconds: float, label: str) -> None:
    """Hold one vector for `seconds`, resending faster than the 0.60 s watchdog."""
    print(f"[preamble] {label}: v={v:+.2f} steer={steer:+.2f} for {seconds:.1f}s")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        send_vector(sock, v, steer)
        # Drain telemetry so the socket buffers do not fill while driving.
        sock.settimeout(0.05)
        try:
            sock.recv(4096)
        except (TimeoutError, OSError):
            pass
        time.sleep(0.1)


def run(args: argparse.Namespace) -> int:
    try:
        sock = socket.create_connection((args.host, args.port), timeout=3.0)
    except OSError as exc:
        print(f"[preamble] cannot connect {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    sock.settimeout(3.0)
    try:
        if args.forward_meters > 0:
            forward_seconds = args.forward_meters / max(args.forward_speed, 1e-3)
            drive(sock, v=args.forward_speed, steer=0.0, seconds=forward_seconds,
                  label=f"forward {args.forward_meters:.1f} m")
        if args.right_degrees != 0:
            # steer magnitude 0.5 on the course simulator = 15 deg/s.
            turn_seconds = abs(args.right_degrees) / max(args.turn_rate, 1e-3)
            steer = math.copysign(0.5, args.right_degrees)
            drive(sock, v=0.0, steer=steer, seconds=turn_seconds,
                  label=f"turn {args.right_degrees:.0f} deg")
        sock.sendall(b"CMD:stand\n")
        print("[preamble] done - autonomy can take over now")
    finally:
        sock.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--forward-meters", type=float, default=10.0,
                        help="straight run before the turn (0 skips)")
    parser.add_argument("--right-degrees", type=float, default=90.0,
                        help="right-hand turn after the run (negative = left)")
    parser.add_argument("--forward-speed", type=float, default=0.4,
                        help="v command during the straight run")
    parser.add_argument("--turn-rate", type=float, default=15.0,
                        help="achievable turn rate in deg/s at steer 0.5")
    raise SystemExit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
