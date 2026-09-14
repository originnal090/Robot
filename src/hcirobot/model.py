from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class ControlState(str, Enum):
    IDLE = "IDLE"
    SEARCHING = "SEARCHING"
    ALIGNING = "ALIGNING"
    APPROACHING = "APPROACHING"
    ARRIVED = "ARRIVED"
    LOST_SAFE = "LOST_SAFE"


@dataclass(frozen=True, slots=True)
class Detection:
    candidate_detected: bool
    detected: bool
    center_x: float | None
    center_y: float | None
    radius: float | None
    frame_width: int
    frame_height: int
    area: float = 0.0
    circularity: float = 0.0
    aspect_ratio: float = 0.0
    score: float = 0.0
    control_confirmed: bool | None = None

    @property
    def has_current_target(self) -> bool:
        values = (self.center_x, self.center_y, self.radius)
        confirmed = self.detected if self.control_confirmed is None else self.control_confirmed
        return (
            self.candidate_detected
            and confirmed
            and self.frame_width > 0
            and self.frame_height > 0
            and all(value is not None and math.isfinite(value) for value in values)
            and 0 <= float(self.center_x) < self.frame_width
            and 0 <= float(self.center_y) < self.frame_height
            and float(self.radius) > 0
        )

    @property
    def horizontal_error(self) -> float:
        if not self.has_current_target:
            raise ValueError("detection has no current target")
        return max(-1.0, min(1.0, 2.0 * float(self.center_x) / self.frame_width - 1.0))

    @property
    def radius_ratio(self) -> float:
        if not self.has_current_target:
            raise ValueError("detection has no current target")
        return float(self.radius) / min(self.frame_width, self.frame_height)


@dataclass(frozen=True, slots=True)
class RobotCommand:
    velocity: float = 0.0
    steer: float = 0.0
    grab: bool = False
    lateral: float = 0.0  # Body-relative: positive right, negative left.

    def __post_init__(self) -> None:
        if not math.isfinite(self.velocity) or not math.isfinite(self.steer):
            raise ValueError("robot command values must be finite")
        if not 0.0 <= self.velocity <= 1.0:
            raise ValueError("velocity must be between 0 and 1")
        if not -1.0 <= self.steer <= 1.0:
            raise ValueError("steer must be between -1 and 1")
        if (
            isinstance(self.lateral, bool)
            or not isinstance(self.lateral, (int, float))
            or not math.isfinite(self.lateral)
            or not -1.0 <= self.lateral <= 1.0
        ):
            raise ValueError("lateral must be a finite number between -1 and 1")

    @classmethod
    def stop(cls) -> RobotCommand:
        return cls()
