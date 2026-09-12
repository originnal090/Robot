from __future__ import annotations

import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .app import EventSink, RuntimeEvent
from .telemetry import AutonomyStatusProjector, encode_autonomy_status


@dataclass(frozen=True, slots=True)
class UnityStatusConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 6102
    frame_rate_hz: float = 10.0
    target_freshness_ms: int = 500
    source: str = "hcirobot"
    maximum_datagram_bytes: int = 4096

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("Unity status host must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("Unity status port must be between 1 and 65535")
        if self.frame_rate_hz <= 0:
            raise ValueError("Unity status frame rate must be positive")
        if self.target_freshness_ms <= 0:
            raise ValueError("Unity target freshness must be positive")
        if not self.source.strip():
            raise ValueError("Unity status source must not be empty")
        if not 256 <= self.maximum_datagram_bytes <= 65507:
            raise ValueError("Unity maximum datagram bytes must be between 256 and 65507")


class UnityStatusPublisher:
    """Best-effort event sink for schema-v1 autonomy status UDP datagrams."""

    def __init__(
        self,
        config: UnityStatusConfig,
        *,
        projector: AutonomyStatusProjector | None = None,
        sock: Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._monotonic = monotonic
        self._projector = projector or AutonomyStatusProjector(
            target_freshness_ms=config.target_freshness_ms,
            source=config.source,
            monotonic=monotonic,
        )
        self._socket = sock
        if config.enabled and self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._address = (config.host, config.port)
        self._minimum_frame_interval = 1.0 / config.frame_rate_hz
        self._last_frame_sent = float("-inf")
        self._last_state_key: tuple[Any, ...] | None = None
        self._seq = 0
        self._closed = False
        self.send_errors = 0

    @property
    def session_id(self) -> str:
        return self._projector.session_id

    @property
    def seq(self) -> int:
        return self._seq

    def __call__(self, event: RuntimeEvent) -> None:
        self.publish(event)

    def publish(self, event: RuntimeEvent) -> bool:
        if self._closed or not self.config.enabled:
            return False
        try:
            now = self._monotonic()
            candidate_seq = self._seq + 1
            status = self._projector.project(event, seq=candidate_seq)
            state_key = self._state_key(status)
            force = event.kind != "frame" or state_key != self._last_state_key
            if not force and now - self._last_frame_sent < self._minimum_frame_interval:
                return False
            payload = encode_autonomy_status(status)
            if len(payload) > self.config.maximum_datagram_bytes:
                raise ValueError(
                    f"Unity status datagram is {len(payload)} bytes; "
                    f"limit is {self.config.maximum_datagram_bytes}"
                )
            self._socket.sendto(payload, self._address)
        except Exception:  # noqa: BLE001 - status reporting cannot stop the control loop.
            self.send_errors += 1
            return False
        self._seq = candidate_seq
        self._last_state_key = state_key
        if event.kind == "frame":
            self._last_frame_sent = now
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _state_key(status: dict[str, Any]) -> tuple[Any, ...]:
        obstacle = status["obstacle"]
        return (
            status["armed"],
            status["control_state"],
            status["video_status"],
            status["estop"],
            status["fault"],
            status["termination"],
            status["output"]["source"],
            obstacle["state"],
            obstacle["avoid_count"],
        )


def fanout_event_sinks(*sinks: EventSink | None) -> EventSink | None:
    active = tuple(sink for sink in sinks if sink is not None)
    if not active:
        return None

    def fanout(event: RuntimeEvent) -> None:
        for sink in active:
            try:
                sink(event)
            except Exception:  # noqa: BLE001, S110 - observers cannot stop control.
                pass

    return fanout
