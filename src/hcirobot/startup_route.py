from __future__ import annotations

import math
from dataclasses import dataclass

from .model import ControlState, Detection, RobotCommand


@dataclass(frozen=True, slots=True)
class StartupRouteConfig:
    """Bounded open-loop lead-in before visual servoing takes control."""

    enabled: bool = False
    forward_seconds: float = 1.5
    forward_velocity: float = 0.60
    settle_seconds: float = 0.50
    turn_steer: float = 0.35
    estimated_turn_step_degrees: float = 20.0
    max_turn_degrees: float = 120.0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("startup enabled must be a boolean")
        values = (
            self.forward_seconds,
            self.forward_velocity,
            self.settle_seconds,
            self.turn_steer,
            self.estimated_turn_step_degrees,
            self.max_turn_degrees,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("startup route values must be finite")
        if self.forward_seconds < 0 or self.settle_seconds <= 0:
            raise ValueError("startup forward time must be non-negative and settle time positive")
        if not 0 < self.forward_velocity <= 1:
            raise ValueError("startup forward velocity must be in (0, 1]")
        if not 0.20 < self.turn_steer <= 1:
            raise ValueError("startup turn steer must select a TonyPi turn gait")
        if not 0 < self.estimated_turn_step_degrees <= self.max_turn_degrees <= 180:
            raise ValueError("startup turn angles must satisfy 0 < step <= max <= 180")

    @property
    def maximum_turn_steps(self) -> int:
        # Floor guarantees the configured angular safety bound is never exceeded.
        return max(1, int(self.max_turn_degrees // self.estimated_turn_step_degrees))


@dataclass(frozen=True, slots=True)
class StartupDecision:
    state: ControlState
    command: RobotCommand
    reason: str
    request_step: bool = False
    target_acquired: bool = False
    failed: bool = False


class StartupRoute:
    """Forward, settle, then scan right one calibrated step at a time."""

    def __init__(self, config: StartupRouteConfig) -> None:
        self.config = config
        self.reset()

    @property
    def active(self) -> bool:
        return self._phase not in ("idle", "complete", "failed")

    @property
    def waiting_for_step(self) -> bool:
        return self._step_pending

    @property
    def completed_steps(self) -> int:
        return self._completed_steps

    def arm(self, now: float) -> None:
        self._phase = "forward" if self.config.forward_seconds > 0 else "settle"
        self._phase_started = now
        self._completed_steps = 0
        self._step_pending = False

    def reset(self) -> None:
        self._phase = "idle"
        self._phase_started = 0.0
        self._completed_steps = 0
        self._step_pending = False

    def update(self, detection: Detection, now: float) -> StartupDecision:
        if self._phase == "forward":
            if now - self._phase_started < self.config.forward_seconds:
                return StartupDecision(
                    ControlState.STARTUP_FORWARD,
                    RobotCommand(velocity=self.config.forward_velocity),
                    "startup_forward",
                )
            self._phase = "settle"
            self._phase_started = now

        if self._phase in ("settle", "turn_settle"):
            if now - self._phase_started < self.config.settle_seconds:
                state = (
                    ControlState.STARTUP_FORWARD
                    if self._phase == "settle"
                    else ControlState.STARTUP_TURN
                )
                return StartupDecision(state, RobotCommand.stop(), f"startup_{self._phase}")
            self._phase = "turn"

        if self._phase == "turn":
            if detection.has_current_target:
                self._phase = "complete"
                return StartupDecision(
                    ControlState.STARTUP_TURN,
                    RobotCommand.stop(),
                    "startup_target_acquired",
                    target_acquired=True,
                )
            if self._step_pending:
                return StartupDecision(
                    ControlState.STARTUP_TURN,
                    RobotCommand.stop(),
                    "startup_step_wait",
                )
            if self._completed_steps >= self.config.maximum_turn_steps:
                self._phase = "failed"
                return StartupDecision(
                    ControlState.LOST_SAFE,
                    RobotCommand.stop(),
                    "startup_scan_exhausted",
                    failed=True,
                )
            return StartupDecision(
                ControlState.STARTUP_TURN,
                RobotCommand(steer=self.config.turn_steer),
                "startup_turn_right",
                request_step=True,
            )

        if self._phase == "failed":
            return StartupDecision(
                ControlState.LOST_SAFE,
                RobotCommand.stop(),
                "startup_scan_exhausted",
                failed=True,
            )
        return StartupDecision(
            ControlState.IDLE,
            RobotCommand.stop(),
            "startup_complete" if self._phase == "complete" else "startup_not_armed",
            target_acquired=self._phase == "complete",
        )

    def mark_step_started(self) -> None:
        if self._phase != "turn" or self._step_pending:
            raise RuntimeError("startup turn step is not ready")
        self._step_pending = True

    def mark_step_complete(self, now: float) -> None:
        if not self._step_pending:
            return
        self._step_pending = False
        self._completed_steps += 1
        self._phase = "turn_settle"
        self._phase_started = now

