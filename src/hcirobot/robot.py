from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import re
import select
import socket
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from .model import RobotCommand

_ACTION_NAME = re.compile(r"[A-Za-z0-9_]{1,32}")
_CONNECT_POLL_SECONDS = 0.1
_RECV_POLL_SECONDS = 0.1
_RAW_KEYS = ("v", "steer", "grab", "t")


class RobotBackend(Protocol):
    def send(self, command: RobotCommand) -> None: ...

    def send_action(self, name: str) -> None: ...

    def send_raw(self, payload: dict) -> None: ...

    @property
    def supports_steps(self) -> bool: ...

    def start_step(self, command: RobotCommand) -> str: ...

    def step_status(self, ident: str) -> str | None: ...

    def heartbeat_step(self, ident: str) -> None: ...

    def latest_distance(self) -> tuple[float, float] | None:
        """Latest ultrasonic reading as (millimetres, time.monotonic() at receive).

        Returns None until a ``DIST:<int>`` telemetry line has been received.
        """
        ...

    def close(self) -> None: ...


def parse_endpoint(text: str, *, default_port: int = 5075) -> tuple[str, int]:
    """Parse ``host[:port]`` (IPv6 bracket form allowed) into a validated pair."""
    value = (text or "").strip()
    if not value:
        raise ValueError("endpoint must not be empty")
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port_text = rest.lstrip(":")
    else:
        host, sep, port_text = value.rpartition(":")
        if not sep:
            host, port_text = value, ""
    host = host.strip()
    if not host:
        raise ValueError("endpoint host must not be empty")
    try:
        port = int(port_text) if port_text else default_port
    except ValueError as exc:
        raise ValueError(f"endpoint port must be an integer: {port_text!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError("endpoint port must be between 1 and 65535")
    return host, port


def same_endpoint(host: str, port: int, other: tuple[str, int]) -> bool:
    """True when ``other`` resolves to host:port (loopback aliases count)."""
    aliases: dict[str, set[str]] = {
        "127.0.0.1": {"127.0.0.1", "localhost", "::1"},
        "localhost": {"127.0.0.1", "localhost", "::1"},
        "::1": {"127.0.0.1", "localhost", "::1"},
    }
    other_host, other_port = other
    if port != other_port:
        return False
    return other_host == host or other_host in aliases.get(host, set())


class MirrorTcpRobot:
    """Best-effort command mirror to a secondary TCP service (e.g. the Unity twin).

    A copy of every command is forwarded, but an unreachable/slow mirror must
    never disturb the primary robot session: connect failures and send errors
    are swallowed, reconnection uses a fixed backoff and finite session budget, and telemetry
    is not consumed (the primary stays the source of truth for DIST).
    """

    def __init__(
        self,
        host: str,
        port: int = 5075,
        *,
        connect_timeout: float = 2.0,
        minimum_send_interval: float = 0.1,
        reconnect_seconds: float = 5.0,
        max_retries: int = 5,
        on_status: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self._client = TcpRobotClient(host, port, connect_timeout, minimum_send_interval)
        self._max_retries = max_retries
        self._retry_failures = 0
        self._closed = False
        self._reconnect_seconds = reconnect_seconds
        self._on_status = on_status
        self._clock = clock
        self._connected = False
        self._next_attempt = float("-inf")
        self.failures = 0
        self._outbox: deque[tuple[bytes, float]] = deque()
        self._outbox_ready = threading.Condition()
        self._mirror_worker: threading.Thread | None = None

    @property
    def target(self) -> str:
        return f"{self._client.host}:{self._client.port}"

    def _ensure_connected(self) -> bool:
        if self._closed or self._retry_failures > self._max_retries:
            return False
        if self._connected:
            return True
        if self._clock() < self._next_attempt:
            return False
        self._next_attempt = self._clock() + self._reconnect_seconds
        try:
            self._client.connect()
        except (ConnectionError, OSError, TimeoutError):
            self._drop()
            return False
        self._connected = True
        self._report(f"镜像已连接：{self.target}")
        return True

    def _forward(self, data: bytes, deadline: float | None = None) -> bool:
        if not self._ensure_connected():
            return False
        if self._closed or (deadline is not None and time.monotonic() >= deadline):
            return False
        try:
            self._client._send_bytes(data)
            return True
        except (ConnectionError, OSError):
            self._drop()
            return False

    def send(self, command: RobotCommand) -> None:
        self._dispatch(encode_legacy_command(command))

    def send_action(self, name: str) -> None:
        validate_action_name(name)
        self._dispatch(f"CMD:{name}\n".encode())

    def send_raw(self, payload: dict) -> None:
        self._dispatch(encode_raw_payload(payload))

    def mirror_step(self, payload: dict) -> None:
        """Display-only events, never real STEP requests or synthetic ACKs.

        Once steps are mirrored, all following controls share a bounded FIFO
        worker so a delayed START cannot overtake a manual STOP. Network delays
        on the mirror must not stall the primary robot's heartbeat.
        """
        data = ("MIRROR_STEP:" + json.dumps(payload, allow_nan=False) + "\n").encode()
        with self._outbox_ready:
            if self._closed:
                return
            if self._mirror_worker is None:
                self._mirror_worker = threading.Thread(
                    target=self._drain_mirror, name="robot-step-mirror", daemon=True
                )
                self._mirror_worker.start()
        self._dispatch(data)

    def _dispatch(self, data: bytes) -> None:
        if self._mirror_worker is None:
            self._forward(data)
            return
        with self._outbox_ready:
            if self._closed:
                return
            if len(self._outbox) >= 32:
                self._outbox.clear()
                data = encode_legacy_command(RobotCommand.stop())
            self._outbox.append((data, time.monotonic() + 0.5))
            self._outbox_ready.notify()

    def _drain_mirror(self) -> None:
        while True:
            with self._outbox_ready:
                while not self._outbox and not self._closed:
                    self._outbox_ready.wait()
                if self._closed:
                    return
                data, deadline = self._outbox.popleft()
            if time.monotonic() <= deadline:
                self._forward(data, deadline)

    def _stop_mirror_worker(self) -> None:
        with self._outbox_ready:
            self._closed = True
            self._outbox.clear()
            self._outbox_ready.notify_all()
        if self._mirror_worker is not None:
            self._client.cancel()
            self._mirror_worker.join(timeout=1.0)

    def latest_distance(self) -> tuple[float, float] | None:
        return None  # mirror telemetry must not shadow the primary

    def cancel(self) -> None:
        self._stop_mirror_worker()
        with contextlib.suppress(Exception):
            self._client.cancel()
        self._connected = False

    def close(self) -> None:
        self._stop_mirror_worker()
        with contextlib.suppress(Exception):
            self._client.close()
        self._connected = False

    def _drop(self) -> None:
        # TcpRobotClient._send_bytes already retired its socket on the error,
        # so the next _ensure_connected() opens a fresh connection.
        self._connected = False
        self.failures += 1
        self._retry_failures += 1
        self._next_attempt = self._clock() + self._reconnect_seconds
        if self._retry_failures > self._max_retries:
            self._report(f"镜像重连已达上限（{self.target}），本次会话已停止镜像重连")
        else:
            self._report(f"镜像断开（{self.target}），{self._reconnect_seconds:.0f}s 后自动重连")

    def _report(self, message: str) -> None:
        callback = self._on_status
        if callback is None:
            return
        with contextlib.suppress(Exception):
            callback(message)


class FanoutRobot:
    """Broadcast backend: every command reaches the primary and all mirrors."""

    def __init__(self, primary: RobotBackend, mirrors: tuple = ()) -> None:
        self.primary = primary
        self.mirrors = tuple(mirrors)
        self._mirrored_step_id: str | None = None

    def connect(self) -> None:
        """Eagerly connect the primary while mirrors remain best-effort and lazy.

        The GUI attaches the fanout before connecting so Stop/E-stop can cancel
        an in-flight primary connection.  Without this delegation, enabling a
        mirror made GUI startup fail before the TCP socket was opened.
        """
        connect = getattr(self.primary, "connect", None)
        if connect is not None:
            connect()

    def send(self, command: RobotCommand) -> None:
        self.primary.send(command)
        for mirror in self.mirrors:
            with contextlib.suppress(Exception):
                mirror.send(command)

    def send_action(self, name: str) -> None:
        self.primary.send_action(name)
        for mirror in self.mirrors:
            with contextlib.suppress(Exception):
                mirror.send_action(name)

    def send_raw(self, payload: dict) -> None:
        self.primary.send_raw(payload)
        for mirror in self.mirrors:
            with contextlib.suppress(Exception):
                mirror.send_raw(payload)

    def latest_distance(self) -> tuple[float, float] | None:
        return self.primary.latest_distance()

    @property
    def supports_steps(self) -> bool:
        return bool(getattr(self.primary, "supports_steps", False))

    def start_step(self, command: RobotCommand) -> str:
        ident = self.primary.start_step(command)
        self._mirrored_step_id = ident
        self._mirror_step({"id": ident, "phase": "start",
                           "steer": command.steer, "lateral": command.lateral})
        return ident

    def step_status(self, ident: str) -> str | None:
        status = self.primary.step_status(ident)
        if ident == self._mirrored_step_id and status in ("done", "cancelled", "error"):
            self._mirror_step({"id": ident, "phase": status})
            self._mirrored_step_id = None
        return status

    def heartbeat_step(self, ident: str) -> None:
        self.primary.heartbeat_step(ident)
        if ident == self._mirrored_step_id:
            self._mirror_step({"id": ident, "phase": "heartbeat"})

    def _mirror_step(self, payload: dict) -> None:
        for mirror in self.mirrors:
            with contextlib.suppress(Exception):
                mirror.mirror_step(payload)

    def cancel(self) -> None:
        for backend in (self.primary, *self.mirrors):
            cancel = getattr(backend, "cancel", None)
            if cancel is None:
                continue
            with contextlib.suppress(Exception):
                cancel()

    def close(self) -> None:
        for backend in (self.primary, *self.mirrors):
            with contextlib.suppress(Exception):
                backend.close()


def encode_legacy_command(command: RobotCommand) -> bytes:
    """Encode the JSON Lines format consumed by Example/TCP_connect.py."""
    payload = {
        "v": round(command.velocity, 4),
        "steer": round(command.steer, 4),
        "grab": command.grab,
        "t": datetime.now(UTC).isoformat(),
    }
    # Keep legacy frames byte-schema compatible until lateral is explicitly used.
    if command.lateral:
        payload["lateral"] = round(command.lateral, 4)
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
    for key in ("v", "steer", "lateral"):
        value = payload.get(key, 0.0)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
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
    steps: list[tuple[str, RobotCommand]] = field(default_factory=list)
    supports_steps = True

    def start_step(self, command: RobotCommand) -> str:
        ident = uuid.uuid4().hex
        self.steps.append((ident, command))
        self.send(command)
        return ident

    def step_status(self, ident: str) -> str | None:
        return "done" if self.steps and self.steps[-1][0] == ident else None

    def heartbeat_step(self, ident: str) -> None:
        pass

    def send(self, command: RobotCommand) -> None:
        self.commands.append(command)

    def send_action(self, name: str) -> None:
        validate_action_name(name)
        self.actions.append(name)

    def send_raw(self, payload: dict) -> None:
        validate_raw_payload(payload)
        self.raw_payloads.append(dict(payload))

    def latest_distance(self) -> tuple[float, float] | None:
        return None

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
        on_message: Callable[[str, str], None] | None = None,
    ) -> None:
        """Wire client for the TonyPi TCP service.

        ``on_message`` receives robot-to-PC telemetry lines as ``(kind, payload)``
        with kind in ``{"distance", "color"}`` (``DIST:<int>`` mm and
        ``COLOR_SIGNAL:<TAG>``). It is invoked on the background reader thread,
        so it must not block; the latest distance is also kept and exposed via
        :meth:`latest_distance`.
        """
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
        self.on_message = on_message
        self._socket: socket.socket | None = None
        self._closing_socket: socket.socket | None = None
        self._last_send = 0.0
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._cancel = threading.Event()
        self._closed = False
        self._distance: tuple[float, float] | None = None
        self._data_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._supports_steps = False
        self._steps: dict[str, str] = {}

    def connect(self) -> None:
        with self._lock:
            with self._state_lock:
                if self._cancel.is_set():
                    raise ConnectionError("robot client is cancelled")
                if self._closed:
                    raise ConnectionError("robot client is closed")
                if self._socket is not None:
                    return
            try:
                sock = self._connect_cancellable()
            except OSError as exc:
                raise ConnectionError(
                    f"cannot connect to robot {self.host}:{self.port}: {exc}"
                ) from exc
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self.connect_timeout)
            with self._state_lock:
                if self._cancel.is_set():
                    sock.close()
                    raise ConnectionError("robot connect cancelled")
                self._socket = sock
                reader = threading.Thread(
                    target=self._recv_loop,
                    args=(sock,),
                    name="robot-telemetry",
                    daemon=True,
                )
                self._reader = reader
                reader.start()

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
                _, writable, _ = select.select(
                    [], [sock], [], min(_CONNECT_POLL_SECONDS, remaining)
                )
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

    @property
    def supports_steps(self) -> bool:
        with self._data_lock:
            return self._supports_steps

    def start_step(self, command: RobotCommand) -> str:
        if not self.supports_steps:
            raise ValueError("robot server does not advertise STEP_V1")
        if command.velocity or command.grab or bool(command.steer) == bool(command.lateral):
            raise ValueError("single step requires only steer or lateral")
        ident = uuid.uuid4().hex
        payload = {"id": ident, "steer": command.steer, "lateral": command.lateral}
        with self._data_lock:
            if any(status == "accepted" for status in self._steps.values()):
                raise ValueError("single step already pending")
            self._steps = {ident: "accepted"}
        self._send_bytes(("STEP:" + json.dumps(payload) + "\n").encode())
        return ident

    def step_status(self, ident: str) -> str | None:
        with self._data_lock:
            return self._steps.get(ident)

    def heartbeat_step(self, ident: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise ValueError("invalid step ID")
        self._send_bytes(("STEP_PING:" + ident + "\n").encode())

    def latest_distance(self) -> tuple[float, float] | None:
        """Latest telemetry distance as (millimetres, time.monotonic() at receive).

        Returns None until a ``DIST:<int>`` line has arrived; the value survives
        close()/cancel() so consumers can still read the last known reading and
        judge staleness from the timestamp.
        """
        with self._data_lock:
            return self._distance

    def _recv_loop(self, sock: socket.socket) -> None:
        """Parse telemetry for the exact socket that spawned this reader.

        Binding the socket prevents a late reader from consuming a replacement
        connection. On EOF/error it unregisters only its own socket, so passive
        disconnects are immediately visible to send() without clobbering a newer
        connection installed by another lifecycle operation.
        """
        buffer = b""
        try:
            while not self._cancel.is_set():
                try:
                    readable, _, _ = select.select([sock], [], [], _RECV_POLL_SECONDS)
                except (OSError, ValueError):
                    break
                if not readable:
                    continue
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break  # peer closed the connection
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._handle_telemetry_line(line)
        finally:
            self._retire_socket(sock)

    def _handle_telemetry_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if text == "CAPS:STEP_V1":
            with self._data_lock:
                self._supports_steps = True
        elif text.startswith("STEP_STATUS:"):
            try:
                payload = json.loads(text[12:])
                ident, status = payload["id"], payload["status"]
                if not isinstance(ident, str) or status not in (
                    "accepted", "done", "cancelled", "error"
                ):
                    return
            except (ValueError, TypeError, KeyError):
                return
            with self._data_lock:
                if self._steps.get(ident) == "accepted":
                    self._steps[ident] = status
            self._emit("step", text[12:])
        elif text.startswith("DIST:"):
            payload = text[5:].strip()
            try:
                value = int(payload)
            except ValueError:
                return  # malformed line: ignore
            with self._data_lock:
                self._distance = (float(value), time.monotonic())
            self._emit("distance", payload)
        elif text.startswith("COLOR_SIGNAL:"):
            self._emit("color", text[len("COLOR_SIGNAL:") :].strip())

    def _emit(self, kind: str, payload: str) -> None:
        callback = self.on_message
        if callback is None:
            return
        try:
            callback(kind, payload)
        except Exception:  # noqa: BLE001, S110 - 回调异常不能拖垮读取线程，忽略即可
            pass

    def cancel(self) -> None:
        """Unblock any in-flight connect/sendall; later sends raise ConnectionError.

        Must not acquire the client lock: a sender blocked inside ``sendall`` holds
        it, and cancel is exactly what has to break that deadlock. Closing the
        socket is safe to race with ``_close_unlocked`` because ``socket.close``
        is idempotent and every call below tolerates ``OSError``.
        """
        self._cancel.set()
        with self._state_lock:
            sock, self._socket = self._socket, None
            closing, self._closing_socket = self._closing_socket, None
        self._close_socket(sock)
        if closing is not sock:
            self._close_socket(closing)
        self._join_reader()

    def close(self) -> None:
        with self._lock:
            with self._state_lock:
                self._closed = True
                sock, self._socket = self._socket, None
                self._closing_socket = sock
            if sock is not None:
                try:
                    sock.sendall(encode_legacy_command(RobotCommand.stop()))
                except OSError:
                    pass
                finally:
                    with self._state_lock:
                        if self._closing_socket is sock:
                            self._closing_socket = None
                    self._close_socket(sock)
            self._join_reader()

    def _join_reader(self) -> None:
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)

    def _send_bytes(self, data: bytes) -> None:
        with self._lock:
            if self._cancel.is_set():
                raise ConnectionError("robot client is cancelled")
            with self._state_lock:
                sock = self._socket
            if sock is None:
                raise ConnectionError("robot client is not connected")
            self._throttle_unlocked()
            # cancel() sets the event before it closes the socket, so a send
            # woken from the throttle wait can still race an in-flight close;
            # the flag re-check is the authoritative "must not send" verdict.
            if self._cancel.is_set():
                raise ConnectionError("robot client is cancelled")
            with self._state_lock:
                if self._socket is not sock:
                    raise ConnectionError("robot client is not connected")
            try:
                sock.sendall(data)
            except OSError as exc:
                self._retire_socket(sock)
                raise ConnectionError(f"failed to send robot command: {exc}") from exc
            self._last_send = time.monotonic()

    def _throttle_unlocked(self) -> None:
        remaining = self.minimum_send_interval - (time.monotonic() - self._last_send)
        if remaining > 0:
            self._cancel.wait(remaining)

    def _retire_socket(self, sock: socket.socket) -> None:
        with self._state_lock:
            if self._socket is sock:
                self._socket = None
                with self._data_lock:
                    self._supports_steps = False
                    self._steps = {ident: "error" for ident in self._steps}
        self._close_socket(sock)

    @staticmethod
    def _close_socket(sock: socket.socket | None) -> None:
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
