from __future__ import annotations

import math
from dataclasses import dataclass

from .model import ControlState, Detection, RobotCommand


@dataclass(frozen=True, slots=True)
class StartupRouteConfig:
    """Optional open-loop lead-in followed by a stable-target right scan."""

    enabled: bool = False
    forward_enabled: bool = False
    forward_seconds: float = 1.5
    forward_velocity: float = 0.60
    settle_seconds: float = 0.50
    turn_steer: float = 0.35
    acquire_frames: int = 6

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("startup enabled must be a boolean")
        if not isinstance(self.forward_enabled, bool):
            raise TypeError("startup forward_enabled must be a boolean")
        values = (
            self.forward_seconds,
            self.forward_velocity,
            self.settle_seconds,
            self.turn_steer,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("startup route values must be finite")
        if self.forward_seconds < 0 or self.settle_seconds <= 0:
            raise ValueError("startup forward time must be non-negative and settle time positive")
        if not 0 < self.forward_velocity <= 1:
            raise ValueError("startup forward velocity must be in (0, 1]")
        if not 0.20 < self.turn_steer <= 1:
            raise ValueError("startup turn steer must select a TonyPi turn gait")
        if not isinstance(self.acquire_frames, int) or isinstance(self.acquire_frames, bool):
            raise TypeError("startup acquire_frames must be an integer")
        if self.acquire_frames < 1:
            raise ValueError("startup acquire_frames must be positive")


@dataclass(frozen=True, slots=True)
class StartupDecision:
    state: ControlState
    command: RobotCommand
    reason: str
    target_acquired: bool = False


class StartupRoute:
    """Optionally move forward, then turn right until the target is stable."""

    def __init__(self, config: StartupRouteConfig) -> None:
        self.config = config
        self.reset()

    @property
    def active(self) -> bool:
        return self._phase not in ("idle", "complete")

    def arm(
        self,
        now: float,
        *,
        forward: bool | None = None,
        forward_seconds: float | None = None,
    ) -> None:
        use_forward = self.config.forward_enabled if forward is None else forward
        if forward_seconds is not None and (
            not math.isfinite(forward_seconds) or not 0 < forward_seconds <= 120
        ):
            raise ValueError("startup forward duration must be in (0, 120] seconds")
        duration = self.config.forward_seconds if forward_seconds is None else forward_seconds
        self._forward_seconds = duration
        self._phase = "forward" if use_forward and duration > 0 else "settle"
        self._phase_started = now
        self._target_streak = 0

    def reset(self) -> None:
        self._phase = "idle"
        self._phase_started = 0.0
        self._target_streak = 0
        self._forward_seconds = self.config.forward_seconds

    def update(self, detection: Detection, now: float) -> StartupDecision:
        if self._phase == "forward":
            if now - self._phase_started < self._forward_seconds:
                return StartupDecision(
                    ControlState.STARTUP_FORWARD,
                    RobotCommand(velocity=self.config.forward_velocity),
                    "startup_forward",
                )
            self._phase = "settle"
            self._phase_started = now

        if self._phase == "settle":
            if now - self._phase_started < self.config.settle_seconds:
                return StartupDecision(
                    ControlState.STARTUP_FORWARD,
                    RobotCommand.stop(),
                    "startup_settle",
                )
            self._phase = "turn"

        if self._phase == "turn":
            if detection.candidate_detected:
                self._target_streak += 1
            else:
                self._target_streak = 0
            if self._target_streak >= self.config.acquire_frames and detection.has_current_target:
                self._phase = "complete"
                return StartupDecision(
                    ControlState.STARTUP_TURN,
                    RobotCommand.stop(),
                    "startup_target_acquired",
                    target_acquired=True,
                )
            return StartupDecision(
                ControlState.STARTUP_TURN,
                RobotCommand(steer=self.config.turn_steer),
                "startup_target_confirming" if self._target_streak else "startup_turn_right",
            )

        return StartupDecision(
            ControlState.IDLE,
            RobotCommand.stop(),
            "startup_complete" if self._phase == "complete" else "startup_not_armed",
            target_acquired=self._phase == "complete",
        )
