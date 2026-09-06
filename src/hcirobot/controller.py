from __future__ import annotations

import math
from dataclasses import dataclass

from .model import ControlState, Detection, RobotCommand


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    align_enter_error: float = 0.08
    align_exit_error: float = 0.14
    arrival_radius_ratio: float = 0.18
    slow_radius_ratio: float = 0.10
    search_steer: float = 0.35
    search_reverse_seconds: float = 2.0
    max_search_seconds: float = 12.0
    align_steer_gain: float = 1.2
    approach_steer_gain: float = 0.9
    align_steer_max: float = 0.60
    approach_steer_max: float = 0.35
    far_speed: float = 0.25
    near_speed: float = 0.21
    minimum_active_steer: float = 0.25
    align_frames: int = 3
    misalign_frames: int = 2
    arrival_frames: int = 3
    lost_frames: int = 3

    def __post_init__(self) -> None:
        float_values = (
            self.align_enter_error,
            self.align_exit_error,
            self.arrival_radius_ratio,
            self.slow_radius_ratio,
            self.search_steer,
            self.search_reverse_seconds,
            self.max_search_seconds,
            self.align_steer_gain,
            self.approach_steer_gain,
            self.align_steer_max,
            self.approach_steer_max,
            self.far_speed,
            self.near_speed,
            self.minimum_active_steer,
        )
        if not all(math.isfinite(value) for value in float_values):
            raise ValueError("all controller values must be finite")
        if not 0 <= self.align_enter_error < self.align_exit_error <= 1:
            raise ValueError("alignment thresholds must satisfy 0 <= enter < exit <= 1")
        if not 0 < self.slow_radius_ratio < self.arrival_radius_ratio < 1:
            raise ValueError("radius thresholds must satisfy 0 < slow < arrival < 1")
        if not 0 <= self.near_speed <= self.far_speed <= 1:
            raise ValueError("speeds must satisfy 0 <= near <= far <= 1")
        if self.search_reverse_seconds <= 0 or self.max_search_seconds <= 0:
            raise ValueError("search timing values must be positive")
        if not 0 <= self.minimum_active_steer <= self.align_steer_max <= 1:
            raise ValueError("steer values must satisfy 0 <= minimum <= align maximum <= 1")
        if not 0 <= self.search_steer <= 1:
            raise ValueError("search steer must be between 0 and 1")
        if self.align_steer_gain < 0 or self.approach_steer_gain < 0:
            raise ValueError("steer gains must be non-negative")
        if not 0 <= self.approach_steer_max <= 1:
            raise ValueError("approach steer maximum must be between 0 and 1")
        if min(self.align_frames, self.misalign_frames, self.arrival_frames, self.lost_frames) < 1:
            raise ValueError("frame thresholds must be positive")


@dataclass(frozen=True, slots=True)
class ControlDecision:
    state: ControlState
    command: RobotCommand
    reason: str
    horizontal_error: float | None = None
    radius_ratio: float | None = None


class VisualApproachController:
    """Finite-state visual servo inspired by the vendor Follow/KickBall examples."""

    def __init__(self, config: ControllerConfig) -> None:
        self.config = config
        self.state = ControlState.IDLE
        self.reason = "not_armed"
        self._search_started_at: float | None = None
        self._search_initial_direction = 1
        self._last_target_error: float | None = None
        self._align_streak = 0
        self._misalign_streak = 0
        self._arrival_streak = 0
        self._lost_streak = 0

    def update_config(self, config: ControllerConfig) -> None:
        """Swap tuning parameters at runtime via an atomic reference swap."""
        self.config = config

    def arm(self, now: float) -> None:
        if self.state is ControlState.IDLE:
            self.state = ControlState.SEARCHING
            self.reason = "armed"
            self._begin_search(now)

    def reset(self) -> None:
        self.state = ControlState.IDLE
        self.reason = "reset"
        self._clear_streaks()
        self._search_started_at = None

    def estop(self, reason: str = "estop") -> None:
        self.state = ControlState.LOST_SAFE
        self.reason = reason
        self._clear_streaks()

    def fail_safe(self, reason: str) -> ControlDecision:
        self.estop(reason)
        return self._decision(RobotCommand.stop())

    def update(self, detection: Detection, now: float) -> ControlDecision:
        if self.state in (ControlState.IDLE, ControlState.LOST_SAFE, ControlState.ARRIVED):
            return self._decision(RobotCommand.stop())

        current = detection.has_current_target
        if current:
            error = detection.horizontal_error
            radius_ratio = detection.radius_ratio
            self._last_target_error = error
            self._lost_streak = 0
            if radius_ratio >= self.config.arrival_radius_ratio:
                self._arrival_streak += 1
                if self._arrival_streak >= self.config.arrival_frames:
                    self.state = ControlState.ARRIVED
                    self.reason = "arrival_confirmed"
                else:
                    self.reason = "arrival_pending"
                return self._decision(RobotCommand.stop(), error, radius_ratio)
            self._arrival_streak = 0
        else:
            error = None
            radius_ratio = None
            self._lost_streak += 1
            self._arrival_streak = 0

        if self.state is ControlState.SEARCHING:
            return self._update_search(current, error, radius_ratio, now)
        if self.state is ControlState.ALIGNING:
            return self._update_align(current, error, radius_ratio, now)
        return self._update_approach(current, error, radius_ratio, now)

    def _update_search(
        self,
        current: bool,
        error: float | None,
        radius_ratio: float | None,
        now: float,
    ) -> ControlDecision:
        assert self._search_started_at is not None
        if now - self._search_started_at >= self.config.max_search_seconds:
            return self.fail_safe("search_timeout")
        if current and error is not None and radius_ratio is not None:
            self.state = ControlState.ALIGNING
            self.reason = "target_confirmed"
            self._align_streak = 0
            return self._update_align(True, error, radius_ratio, now)
        phase = int((now - self._search_started_at) // self.config.search_reverse_seconds)
        direction = self._search_initial_direction * (-1 if phase % 2 else 1)
        self.reason = "searching"
        return self._decision(RobotCommand(steer=direction * self.config.search_steer))

    def _update_align(
        self,
        current: bool,
        error: float | None,
        radius_ratio: float | None,
        now: float,
    ) -> ControlDecision:
        if not current or error is None or radius_ratio is None:
            if self._lost_streak >= self.config.lost_frames:
                self.state = ControlState.SEARCHING
                self.reason = "target_lost"
                self._begin_search(now)
                return self._update_search(False, None, None, now)
            self.reason = "target_temporarily_missing"
            return self._decision(RobotCommand.stop())

        if abs(error) <= self.config.align_enter_error:
            self._align_streak += 1
            if self._align_streak >= self.config.align_frames:
                self.state = ControlState.APPROACHING
                self.reason = "target_aligned"
                self._misalign_streak = 0
                return self._update_approach(True, error, radius_ratio, now)
        else:
            self._align_streak = 0
        steer = self._clamp(error * self.config.align_steer_gain, self.config.align_steer_max)
        steer = self._minimum_active(steer)
        self.reason = "aligning"
        return self._decision(RobotCommand(steer=steer), error, radius_ratio)

    def _update_approach(
        self,
        current: bool,
        error: float | None,
        radius_ratio: float | None,
        now: float,
    ) -> ControlDecision:
        if not current or error is None or radius_ratio is None:
            if self._lost_streak >= self.config.lost_frames:
                self.state = ControlState.SEARCHING
                self.reason = "target_lost"
                self._begin_search(now)
                return self._update_search(False, None, None, now)
            self.reason = "target_temporarily_missing"
            return self._decision(RobotCommand.stop())

        if abs(error) > self.config.align_exit_error:
            self._misalign_streak += 1
            if self._misalign_streak >= self.config.misalign_frames:
                self.state = ControlState.ALIGNING
                self.reason = "alignment_lost"
                self._align_streak = 0
                return self._update_align(True, error, radius_ratio, now)
        else:
            self._misalign_streak = 0

        if abs(error) > self.config.align_enter_error:
            steer = self._clamp(
                error * self.config.approach_steer_gain,
                self.config.approach_steer_max,
            )
            steer = self._minimum_active(steer)
            self.reason = "approach_correction"
            return self._decision(RobotCommand(steer=steer), error, radius_ratio)

        velocity = self._approach_speed(radius_ratio)
        self.reason = "approaching"
        return self._decision(RobotCommand(velocity=velocity), error, radius_ratio)

    def _begin_search(self, now: float) -> None:
        self._search_started_at = now
        if self._last_target_error is not None:
            self._search_initial_direction = 1 if self._last_target_error >= 0 else -1
        self._clear_streaks()

    def _clear_streaks(self) -> None:
        self._align_streak = 0
        self._misalign_streak = 0
        self._arrival_streak = 0
        self._lost_streak = 0

    def _approach_speed(self, radius_ratio: float) -> float:
        if radius_ratio >= self.config.arrival_radius_ratio:
            return 0.0
        span = self.config.arrival_radius_ratio - self.config.slow_radius_ratio
        if span <= 0:
            return self.config.near_speed
        scale = (self.config.arrival_radius_ratio - radius_ratio) / span
        scale = max(0.0, min(1.0, scale))
        return self.config.near_speed + (self.config.far_speed - self.config.near_speed) * scale

    def _minimum_active(self, value: float) -> float:
        if value == 0:
            return 0.0
        return math.copysign(max(abs(value), self.config.minimum_active_steer), value)

    @staticmethod
    def _clamp(value: float, limit: float) -> float:
        if not math.isfinite(value):
            return 0.0
        return max(-limit, min(limit, value))

    def _decision(
        self,
        command: RobotCommand,
        error: float | None = None,
        radius_ratio: float | None = None,
    ) -> ControlDecision:
        return ControlDecision(self.state, command, self.reason, error, radius_ratio)
