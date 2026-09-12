from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .app import RuntimeEvent

AUTONOMY_STATUS_TYPE = "autonomy_status"
AUTONOMY_STATUS_SCHEMA_VERSION = 1
DEFAULT_TARGET_FRESHNESS_MS = 500


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    raise TypeError(f"unsupported telemetry value: {type(value).__name__}")


def encode_autonomy_status(status: Mapping[str, Any]) -> bytes:
    """Encode one schema-v1 autonomy status datagram as strict UTF-8 JSON."""
    payload = _json_safe(status)
    if not isinstance(payload, dict):
        raise TypeError("autonomy status must be a mapping")
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


class AutonomyStatusProjector:
    """Stateful projection from runtime events to the stable schema-v1 wire model."""

    def __init__(
        self,
        session_id: str | uuid.UUID | None = None,
        *,
        target_freshness_ms: int = DEFAULT_TARGET_FRESHNESS_MS,
        source: str = "hcirobot",
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if target_freshness_ms <= 0:
            raise ValueError("target freshness must be positive")
        if not source.strip():
            raise ValueError("telemetry source must not be empty")
        parsed_session = uuid.uuid4() if session_id is None else uuid.UUID(str(session_id))
        self.session_id = str(parsed_session)
        self.source = source.strip()
        self.target_freshness_ms = int(target_freshness_ms)
        self._monotonic = monotonic
        self._wall_clock_ms = wall_clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._last_target_at: float | None = None
        self._target: dict[str, Any] = self._empty_target()
        self._control_state = "IDLE"
        self._video_status = "waiting"
        self._fault: str | None = None
        self._termination: str | None = None
        self._estop = False

    @staticmethod
    def _empty_target() -> dict[str, Any]:
        return {
            "candidate": False,
            "confirmed": False,
            "fresh": False,
            "age_ms": -1,
            "center_x": -1.0,
            "center_y": -1.0,
            "radius": -1.0,
            "frame_width": 0,
            "frame_height": 0,
        }

    def project(
        self,
        event: RuntimeEvent,
        *,
        seq: int,
        sent_at_ms: int | None = None,
    ) -> dict[str, Any]:
        if seq < 1:
            raise ValueError("telemetry sequence must be positive")
        now = self._monotonic()
        self._apply_event(event, now)
        target = dict(self._target)
        target_age_ms = self._target_age_ms(now)
        target["age_ms"] = -1 if target_age_ms is None else target_age_ms
        target["fresh"] = bool(
            target["confirmed"]
            and target_age_ms is not None
            and target_age_ms <= self.target_freshness_ms
        )
        control_state = (
            event.decision.state.value if event.decision is not None else self._control_state
        )
        distance_mm = _finite_number(event.distance_mm)
        return {
            "type": AUTONOMY_STATUS_TYPE,
            "schema_version": AUTONOMY_STATUS_SCHEMA_VERSION,
            "source": self.source,
            "session_id": self.session_id,
            "seq": int(seq),
            "sent_at_ms": int(self._wall_clock_ms() if sent_at_ms is None else sent_at_ms),
            "mode": "autonomy" if event.armed else "preview",
            "armed": bool(event.armed),
            "control_state": control_state,
            "state": control_state,
            "reason": event.decision.reason if event.decision is not None else event.message,
            "video_status": self._video_status,
            "frame_count": max(0, int(event.frame_count)),
            "target": target,
            "output": {
                "v": _finite_number(event.output_v) or 0.0,
                "steer": _finite_number(event.output_steer) or 0.0,
                "grab": bool(event.output_grab),
                "source": str(event.output_source),
            },
            "velocity": _finite_number(event.output_v) or 0.0,
            "steer": _finite_number(event.output_steer) or 0.0,
            "obstacle": {
                "state": event.obstacle_state,
                "distance_mm": -1.0 if distance_mm is None else distance_mm,
                "avoid_count": None if event.avoid_count is None else max(0, int(event.avoid_count)),
            },
            "distance_mm": -1.0 if distance_mm is None else distance_mm,
            "estop": self._estop,
            "fault": self._fault,
            "termination": self._termination,
            "event": {"kind": event.kind, "message": event.message},
        }

    def _apply_event(self, event: RuntimeEvent, now: float) -> None:
        if event.kind == "started":
            self._video_status = "waiting"
            self._fault = None
            self._termination = None
            self._estop = False
        if event.kind == "frame":
            self._video_status = "streaming"
            if event.detection is not None:
                detection = event.detection
                confirmed = detection.has_current_target
                center_x = _finite_number(detection.center_x)
                center_y = _finite_number(detection.center_y)
                radius = _finite_number(detection.radius)
                self._target = {
                    "candidate": bool(detection.candidate_detected),
                    "confirmed": confirmed,
                    "fresh": confirmed,
                    "age_ms": 0 if confirmed else -1,
                    "center_x": -1.0 if center_x is None else center_x,
                    "center_y": -1.0 if center_y is None else center_y,
                    "radius": -1.0 if radius is None else radius,
                    "frame_width": max(0, int(detection.frame_width)),
                    "frame_height": max(0, int(detection.frame_height)),
                }
                if confirmed:
                    self._last_target_at = now
        if event.decision is not None:
            self._control_state = event.decision.state.value
        if event.kind == "estop":
            self._estop = True
            self._control_state = "LOST_SAFE"
        elif event.kind == "error":
            self._fault = event.message or "runtime_error"
            self._control_state = "LOST_SAFE"
        elif event.kind == "finished":
            self._termination = event.message or "completed"
            self._video_status = "ended"
            if self._termination in {
                "estop",
                "video_timeout",
                "video_ended",
                "obstacle_blocked",
                "robot_connection_lost",
                "runtime_error",
            }:
                self._control_state = "LOST_SAFE"

    def _target_age_ms(self, now: float) -> int | None:
        if self._last_target_at is None:
            return None
        return max(0, round((now - self._last_target_at) * 1000))
