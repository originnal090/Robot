from __future__ import annotations

import errno
import json
import math
import os
import re
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from .model import RobotCommand

_ACTION_NAME = re.compile(r"[A-Za-z0-9_]{1,32}")
_CONNECT_POLL_SECONDS = 0.1
_RAW_KEYS = ("v", "steer", "grab", "t")


class RobotBackend(Protocol):
    def send(self, command: RobotCommand) -> None: ...

    def send_action(self, name: str) -> None: ...

    def send_raw(self, payload: dict) -> None: ...

    def close(self) -> None: ...


def encode_legacy_command(command: RobotCommand) -> bytes:
    """Encode the JSON Lines format consumed by Example/TCP_connect.py."""
    payload = {
        "v": round(command.velocity, 4),
        "steer": round(command.steer, 4),
        "grab": command.grab,
        "t": datetime.now(UTC).isoformat(),
    }
    return (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def validate_action_name(name: str) -> None:
    """Validate the action name carried by the ``CMD:<name>`` wire format."""
    if not isinstance(name, str) or not _ACTION_NAME.fullmatch(name):
        raise ValueError("action name must be 1-32 alphanumeric or underscore characters")


def validate_raw_payload(payload: dict) -> None:
    """Validate a raw v/steer payload sent straight to the robot service."""
    if not isinstance(payload, dict):
        raise TypeError("raw payload must be a dict")
    missing = [key for key in _RAW_KEYS if key not in payload]
    if missing:
        raise ValueError(f"raw payload missing keys: {', '.join(missing)}")
    for key in ("v", "steer"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"raw payload {key} must be a finite number")
        if not -1.0 <= float(value) <= 1.0:
            raise ValueError(f"raw payload {key} must be between -1 and 1")


def encode_raw_payload(payload: dict) -> bytes:
    """Validate and encode a raw JSON Lines control frame."""
    validate_raw_payload(payload)
    return (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


@dataclass(slots=True)
class RecordingRobot:
    commands: list[RobotCommand] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    raw_payloads: list[dict] = field(default_factory=list)

    def send(self, command: RobotCommand) -> None:
        self.commands.append(command)

    def send_action(self, name: str) -> None:
        validate_action_name(name)
        self.actions.append(name)

    def send_raw(self, payload: dict) -> None:
        validate_raw_payload(payload)
        self.raw_payloads.append(dict(payload))

    def close(self) -> None:
        if not self.commands or self.commands[-1] != RobotCommand.stop():
            self.send(RobotCommand.stop())


class TcpRobotClient:
    def __init__(
        self,
        host: str,
        port: int = 5075,
        connect_timeout: float = 3.0,
        minimum_send_interval: float = 0.1,
    ) -> None:
        if not host.strip():
            raise ValueError("robot host must not be empty")
        if not 1 <= port <= 65535:
            raise ValueError("robot port must be between 1 and 65535")
        if connect_timeout <= 0:
            raise ValueError("connect timeout must be positive")
        if minimum_send_interval < 0:
            raise ValueError("minimum send interval must be non-negative")
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.minimum_send_interval = minimum_send_interval
        self._socket: socket.socket | None = None
        self._last_send = 0.0
        self._lock = threading.Lock()
        self._cancel = threading.Event()

    def connect(self) -> None:
        with self._lock:
            if self._socket is not None:
                return
            self._cancel.clear()
            sock = self._connect_cancellable()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self.connect_timeout)
            self._socket = sock

    def _connect_cancellable(self) -> socket.socket:
        """Connect with a deadline polled in ~0.1s slices so cancel() can interrupt it."""
        deadline = time.monotonic() + self.connect_timeout
        address = (self.host, self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            error = sock.connect_ex(address)
            if error == 0:
                return sock
            if error not in (errno.EINPROGRESS, errno.EWOULDBLOCK):
                raise OSError(error, os.strerror(error))
            while True:
                if self._cancel.is_set():
                    raise ConnectionError("robot connect cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out connecting to robot {self.host}:{self.port}")
                _, writable, _ = select.select([], [sock], [], min(_CONNECT_POLL_SECONDS, remaining))
                if not writable:
                    continue
                error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error != 0:
                    raise OSError(error, os.strerror(error))
                return sock
        except BaseException:
            sock.close()
            raise

    def send(self, command: RobotCommand) -> None:
        self._send_bytes(encode_legacy_command(command))

    def send_action(self, name: str) -> None:
        validate_action_name(name)
        self._send_bytes(f"CMD:{name}\n".encode())

    def send_raw(self, payload: dict) -> None:
        self._send_bytes(encode_raw_payload(payload))

    def cancel(self) -> None:
        """Unblock any in-flight connect/sendall; later sends raise ConnectionError.

        Must not acquire the client lock: a sender blocked inside ``sendall`` holds
        it, and cancel is exactly what has to break that deadlock. Closing the
        socket is safe to race with ``_close_unlocked`` because ``socket.close``
        is idempotent and every call below tolerates ``OSError``.
        """
        self._cancel.set()
        sock = self._socket
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            if self._socket is None:
                return
            try:
                self._socket.sendall(encode_legacy_command(RobotCommand.stop()))
            except OSError:
                pass
            self._close_unlocked()

    def _send_bytes(self, data: bytes) -> None:
        with self._lock:
            if self._cancel.is_set():
                raise ConnectionError("robot client is cancelled")
            if self._socket is None:
                raise ConnectionError("robot client is not connected")
            self._throttle_unlocked()
            sock = self._socket
            if sock is None:  # cancel() fired while the throttle wait was sleeping
                raise ConnectionError("robot client is cancelled")
            try:
                sock.sendall(data)
            except OSError as exc:
                self._close_unlocked()
                raise ConnectionError(f"failed to send robot command: {exc}") from exc
            self._last_send = time.monotonic()

    def _throttle_unlocked(self) -> None:
        remaining = self.minimum_send_interval - (time.monotonic() - self._last_send)
        if remaining > 0:
            self._cancel.wait(remaining)

    def _close_unlocked(self) -> None:
        sock, self._socket = self._socket, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
