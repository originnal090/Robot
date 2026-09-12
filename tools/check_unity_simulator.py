#!/usr/bin/env python3
"""Check the Unity virtual robot TCP, DIST telemetry, watchdog, and MJPEG endpoints."""

from __future__ import annotations

import argparse
import json
import math
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    CHECK_FAILED = 1
    USAGE_ERROR = 2


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    warning: bool = False


class LineCollector:
    """Collect newline-delimited simulator telemetry without external dependencies."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.lines: list[tuple[float, str]] = []
        self.error: str | None = None
        self.closed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="unity-check-reader", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def snapshot(self) -> list[tuple[float, str]]:
        with self._lock:
            return list(self.lines)

    def _run(self) -> None:
        buffer = b""
        self.sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                chunk = self.sock.recv(4096)
            except TimeoutError:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.error = str(exc)
                return
            if not chunk:
                self.closed = True
                return
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    with self._lock:
                        self.lines.append((time.monotonic(), text))


def build_control_frame(v: float, steer: float) -> bytes:
    if not all(math.isfinite(value) and -1.0 <= value <= 1.0 for value in (v, steer)):
        raise ValueError("v and steer must be finite values in [-1, 1]")
    payload = {
        "v": v,
        "steer": steer,
        "grab": False,
        "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode()


def parse_distance_line(line: str) -> float | None:
    if not line.startswith("DIST:"):
        return None
    try:
        value = float(line[5:].strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def watchdog_state_observed(lines: list[tuple[float, str]], since: float) -> bool:
    """Recognize optional simulator diagnostics without requiring a specific Unity build."""
    for received_at, line in lines:
        if received_at < since:
            continue
        upper = line.upper()
        if upper.startswith(("STATE:", "MODE:", "WATCHDOG:")) and any(
            word in upper for word in ("STAND", "STOP", "IDLE")
        ):
            return True
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            mode = str(payload.get("mode", payload.get("state", ""))).lower()
            if payload.get("watchdog") is True and mode in {"", "stand", "stopped", "idle"}:
                return True
            if mode in {"stand", "stopped", "idle"} and payload.get("source") == "watchdog":
                return True
    return False


def wait_for_distance(
    collector: LineCollector,
    *,
    timeout: float,
    not_before: float = 0.0,
) -> tuple[float, float] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for received_at, line in reversed(collector.snapshot()):
            if received_at < not_before:
                break
            value = parse_distance_line(line)
            if value is not None:
                return value, received_at
        if collector.closed or collector.error:
            return None
        time.sleep(0.02)
    return None


def check_tcp(
    host: str,
    port: int,
    *,
    timeout: float,
    distance_timeout: float,
    watchdog_seconds: float,
    watchdog_margin: float,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return [CheckResult("TCP connect", False, f"{host}:{port}: {exc}")]

    collector = LineCollector(sock)
    collector.start()
    zero = build_control_frame(0.0, 0.0)
    try:
        sock.sendall(build_control_frame(0.0, -0.35))
        results.append(CheckResult("TCP JSONL", True, "sent a valid left-turn control frame"))

        sock.sendall(zero)
        results.append(CheckResult("TCP zero vector", True, "sent v=0 and steer=0"))

        sock.sendall(b"CMD:stand\n")
        results.append(CheckResult("TCP CMD", True, "sent CMD:stand"))

        first_distance = wait_for_distance(collector, timeout=distance_timeout)
        if first_distance is None:
            detail = collector.error or "no valid DIST:<mm> line arrived"
            results.append(CheckResult("DIST telemetry", False, detail))
        else:
            distance, received_at = first_distance
            age = time.monotonic() - received_at
            results.append(
                CheckResult(
                    "DIST telemetry",
                    age <= distance_timeout,
                    f"latest={distance:g} mm, age={age:.3f}s",
                )
            )

        sock.sendall(build_control_frame(0.0, 0.35))
        watchdog_started = time.monotonic()
        wait_seconds = watchdog_seconds + watchdog_margin
        fresh_after_watchdog = wait_for_distance(
            collector,
            timeout=wait_seconds + distance_timeout,
            not_before=watchdog_started + watchdog_seconds,
        )
        observed = watchdog_state_observed(collector.snapshot(), watchdog_started)
        if collector.closed or collector.error:
            results.append(
                CheckResult(
                    "TCP watchdog",
                    False,
                    collector.error or "connection closed while waiting for watchdog",
                )
            )
        elif observed:
            results.append(
                CheckResult(
                    "TCP watchdog",
                    True,
                    f"optional simulator state reported stop after {watchdog_seconds:.2f}s",
                )
            )
        elif fresh_after_watchdog is not None:
            results.append(
                CheckResult(
                    "TCP watchdog",
                    True,
                    "watchdog interval elapsed and connection/DIST stayed alive; "
                    "visually confirm the avatar stopped",
                    warning=True,
                )
            )
        else:
            results.append(
                CheckResult(
                    "TCP watchdog",
                    False,
                    "no stop status and no fresh DIST after the watchdog interval",
                )
            )
    except OSError as exc:
        results.append(CheckResult("TCP protocol", False, str(exc)))
    finally:
        try:
            sock.sendall(zero)
            sock.sendall(b"CMD:stand\n")
        except OSError:
            pass
        collector.stop()
        try:
            sock.close()
        except OSError:
            pass
    return results


def extract_complete_jpeg(stream: Any, *, max_bytes: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < max_bytes:
        chunk = stream.read(min(16384, max_bytes - len(buffer)))
        if not chunk:
            break
        buffer.extend(chunk)
        start = buffer.find(b"\xff\xd8")
        if start < 0:
            if len(buffer) > 2:
                del buffer[:-2]
            continue
        end = buffer.find(b"\xff\xd9", start + 2)
        if end >= 0:
            return bytes(buffer[start : end + 2])
        if start > 0:
            del buffer[:start]
    raise ValueError(f"no complete JPEG found within {max_bytes} bytes")


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
        return None
    index = 2
    while index + 4 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0x01, *range(0xD0, 0xD9)}:
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2 or index + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if length < 7:
                return None
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return (width, height) if width > 0 and height > 0 else None
        index += length
    return None


def check_mjpeg(url: str, *, timeout: float, max_bytes: int) -> CheckResult:
    request = urllib.request.Request(url, headers={"User-Agent": "hcirobot-unity-check/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            jpeg = extract_complete_jpeg(response, max_bytes=max_bytes)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return CheckResult("MJPEG stream", False, f"{url}: {exc}")
    dimensions = jpeg_dimensions(jpeg)
    if dimensions is None:
        return CheckResult(
            "MJPEG stream",
            False,
            f"read {len(jpeg)} JPEG-framed bytes but could not parse image dimensions",
        )
    width, height = dimensions
    return CheckResult(
        "MJPEG stream",
        True,
        f"read one complete JPEG ({width}x{height}, {len(jpeg)} bytes)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Unity TCP host")
    parser.add_argument("--port", type=int, default=5075, help="Unity TCP port")
    parser.add_argument(
        "--mjpeg-url",
        default="http://127.0.0.1:8080/?action=stream",
        help="Unity MJPEG URL",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="connection/read timeout")
    parser.add_argument(
        "--distance-timeout", type=float, default=1.2, help="maximum wait for a DIST line"
    )
    parser.add_argument(
        "--watchdog-seconds", type=float, default=0.60, help="configured simulator watchdog"
    )
    parser.add_argument(
        "--watchdog-margin", type=float, default=0.25, help="extra watchdog observation time"
    )
    parser.add_argument(
        "--max-mjpeg-bytes", type=int, default=8 * 1024 * 1024, help="JPEG search limit"
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.port <= 65535:
        raise ValueError("--port must be in 1..65535")
    for name in ("timeout", "distance_timeout", "watchdog_seconds", "watchdog_margin"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be > 0")
    if args.max_mjpeg_bytes < 1024:
        raise ValueError("--max-mjpeg-bytes must be at least 1024")


def run(args: argparse.Namespace) -> tuple[int, list[CheckResult]]:
    try:
        validate_args(args)
    except ValueError as exc:
        return ExitCode.USAGE_ERROR, [CheckResult("arguments", False, str(exc))]
    results = check_tcp(
        args.host,
        args.port,
        timeout=args.timeout,
        distance_timeout=args.distance_timeout,
        watchdog_seconds=args.watchdog_seconds,
        watchdog_margin=args.watchdog_margin,
    )
    results.append(
        check_mjpeg(args.mjpeg_url, timeout=args.timeout, max_bytes=args.max_mjpeg_bytes)
    )
    code = ExitCode.OK if all(result.ok for result in results) else ExitCode.CHECK_FAILED
    return code, results


def main() -> None:
    args = build_parser().parse_args()
    code, results = run(args)
    for result in results:
        label = "WARN" if result.warning and result.ok else ("PASS" if result.ok else "FAIL")
        print(f"[{label}] {result.name}: {result.detail}")
    print(
        "Exit codes: 0=all endpoint checks passed, 1=one or more checks failed, "
        "2=invalid arguments"
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
