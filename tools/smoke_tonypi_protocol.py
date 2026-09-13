#!/usr/bin/env python3
"""No-hardware smoke test for robot_side/tonypi_server.py.

Starts the service as an explicit dry-run subprocess on free loopback ports,
sends the stable JSONL/CMD protocol, observes simulated DIST telemetry, waits
past the watchdog, then terminates the process and verifies a clean exit.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "robot_side" / "tonypi_server.py"


def _free_port(sock_type: int) -> int:
    with socket.socket(socket.AF_INET, sock_type) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pump(stream: TextIO, lines: list[str]) -> None:
    for line in stream:
        text = line.rstrip("\r\n")
        lines.append(text)
        print("[server]", text)


def _connect(port: int, process: subprocess.Popen[str], timeout: float) -> socket.socket:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited before accepting connections: {process.returncode}")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.25)
        try:
            sock.connect(("127.0.0.1", port))
            sock.settimeout(0.25)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
            time.sleep(0.05)
    raise RuntimeError(f"server did not listen on port {port}: {last_error}")


def _receive_lines(sock: socket.socket, seconds: float) -> list[str]:
    deadline = time.monotonic() + seconds
    buffer = b""
    lines: list[str] = []
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            continue
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            lines.append(raw.decode("utf-8", "replace").strip())
    return lines


def run_smoke(timeout: float = 8.0) -> None:
    tcp_port = _free_port(socket.SOCK_STREAM)
    udp_port = _free_port(socket.SOCK_DGRAM)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "TONYPI_MODE": "dry-run",
            "TONYPI_REQUIRE_SONAR": "1",
            "TONYPI_SONAR_SIM": "flat:432",
            "TONYPI_HOST": "127.0.0.1",
            "TONYPI_PORT": str(tcp_port),
            "TONYPI_UDP_COLOR_HOST": "127.0.0.1",
            "TONYPI_UDP_COLOR_PORT": str(udp_port),
            "TONYPI_DIST_INTERVAL": "0.05",
            "TONYPI_WATCHDOG": "0.20",
            "TONYPI_STEP_INTERVAL": "0.05",
            "TONYPI_PRINT_FWD": "0",
        }
    )
    env.pop("TONYPI_DRY_RUN", None)
    env.pop("TONYPI_ALLOW_SIM_WITH_HARDWARE", None)

    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, "-u", str(SERVER)],
        cwd=ROOT,
        env=env,
        creationflags=creationflags,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    output: list[str] = []
    reader = threading.Thread(target=_pump, args=(process.stdout, output), daemon=True)
    reader.start()

    sock: socket.socket | None = None
    try:
        sock = _connect(tcp_port, process, timeout)
        control = json.dumps({"v": 0.4, "steer": 0.0, "grab": False, "t": "smoke"})
        zero = json.dumps({"v": 0.0, "steer": 0.0, "grab": False, "t": "smoke-zero"})
        sock.sendall((control + "\n" + zero + "\nCMD:nod\n").encode("utf-8"))
        received = _receive_lines(sock, 0.45)
        if not any(line == "DIST:432" for line in received):
            raise RuntimeError(f"expected simulated DIST:432, received: {received!r}")
        if not any("[DRY] head servo" in line for line in output):
            raise RuntimeError("CMD:nod did not execute while the service was standing")

        # Let a fresh forward command remain silent longer than the watchdog.
        stand_count = sum("[MODE] stand" in line for line in output)
        sock.sendall((control + "\n").encode("utf-8"))
        time.sleep(0.35)
        if process.poll() is not None:
            raise RuntimeError(f"server exited during watchdog window: {process.returncode}")
        if sum("[MODE] stand" in line for line in output) <= stand_count:
            raise RuntimeError("watchdog did not produce a new stand transition")
    finally:
        if sock is not None:
            sock.close()
        if process.poll() is None:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGTERM)
        try:
            returncode = process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=2.0)
        reader.join(timeout=1.0)

    if returncode != 0:
        raise RuntimeError(f"server did not terminate cleanly: exit {returncode}")
    if not any("[SYS] bye" in line for line in output):
        raise RuntimeError("server did not report normal shutdown")
    if not any("[DRY] run action group: stand" in line for line in output):
        raise RuntimeError("service did not perform a dry-run stand action")
    print("[SMOKE] PASS: JSONL zero/control, CMD, DIST, watchdog and graceful shutdown")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()
    try:
        run_smoke(args.timeout)
    except Exception as exc:  # noqa: BLE001 - command-line diagnostic boundary
        print(f"[SMOKE] FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
