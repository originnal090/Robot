from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass

from .model import ControlState, Detection, RobotCommand

_ACTION_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,32}")

# Approach strategies.  Modes 1-3 gate motion into walk/settle/sense cycles
# (see ApproachGate); "slow_realtime" is classic continuous control.  Speed
# pairs are (far, near) velocity magnitudes chosen to land in the robot-side
# gait bands: <=0.45 one_step, <=0.75 normal, >0.75 fast.
APPROACH_MODES = ("fast_then_slow", "normal", "normal_then_slow", "slow_realtime")
ARRIVAL_SIZE_VERSIONS = ("v1", "v2")
APPROACH_SPEEDS: dict[str, tuple[float, float]] = {
    "fast_then_slow": (0.85, 0.30),
    "normal": (0.60, 0.60),
    "normal_then_slow": (0.60, 0.30),
    "slow_realtime": (0.30, 0.30),
}


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    align_enter_error: float = 0.08
    align_exit_error: float = 0.14
    arrival_radius_ratio: float = 0.18
    # Arrival is accepted only near image centre and while the stopped size
    # samples agree. This rejects a close false box at the frame edge and one-frame jumps.
    arrival_max_error: float = 0.10
    arrival_radius_stability_ratio: float = 0.18
    arrival_size_version: str = "v2"
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
    # A near, centred ball that reaches the bottom image boundary is entering
    # the camera blind zone.  After several confirmed clipped frames, advance
    # slowly for a short calibrated interval instead of treating disappearance
    # as an ordinary lost target.
    final_approach_enabled: bool = False
    final_approach_radius_ratio: float = 0.12
    final_approach_bottom_ratio: float = 0.88
    final_approach_frames: int = 3
    final_approach_velocity: float = 0.30
    final_approach_seconds: float = 0.60
    # CMD name sent once when autonomy reaches ARRIVED ("" disables).  Uses the
    # course button-name namespace so both TCP_connect.py and tonypi_server.py
    # accept it: "right_grip" maps to the crouch-and-extinguish (outfire) group.
    arrival_action: str = "right_grip"
    # Walk-burst approach (modes 1-3): walk -> settle -> sense phase cycle.
    # Walk length shrinks as the ball grows: near = more frequent re-sensing.
    approach_mode: str = "normal_then_slow"
    walk_seconds_far: float = 1.5
    walk_seconds_near: float = 0.5
    settle_seconds: float = 0.4
    sense_seconds: float = 0.7
    # Fixed request windows for small/normal gait tiers, not step-angle control.
    turn_seconds_near: float = 0.18
    turn_seconds_far: float = 0.45
    turn_full_error: float = 0.45  # Below this error, use the small-step group.
    single_step_turns: bool = True
    step_timeout_seconds: float = 10.0
    # Opt-in until the lateral gait has been calibrated on the actual floor.
    near_lateral_enabled: bool = False
    near_lateral_max_error: float = 0.25
    near_lateral_speed: float = 0.30

    def __post_init__(self) -> None:
        float_values = (
            self.align_enter_error,
            self.align_exit_error,
            self.arrival_radius_ratio,
            self.arrival_max_error,
            self.arrival_radius_stability_ratio,
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
            self.walk_seconds_far,
            self.walk_seconds_near,
            self.settle_seconds,
            self.sense_seconds,
            self.turn_seconds_near,
            self.turn_seconds_far,
            self.turn_full_error,
            self.step_timeout_seconds,
            self.near_lateral_max_error,
            self.near_lateral_speed,
            self.final_approach_radius_ratio,
            self.final_approach_bottom_ratio,
            self.final_approach_velocity,
            self.final_approach_seconds,
        )
        if not all(math.isfinite(value) for value in float_values):
            raise ValueError("all controller values must be finite")
        if not 0 <= self.align_enter_error < self.align_exit_error <= 1:
            raise ValueError("alignment thresholds must satisfy 0 <= enter < exit <= 1")
        if not 0 < self.slow_radius_ratio < self.arrival_radius_ratio < 1:
            raise ValueError("radius thresholds must satisfy 0 < slow < arrival < 1")
        if not 0 <= self.arrival_max_error <= self.align_exit_error:
            raise ValueError("arrival max error must be between 0 and align_exit_error")
        if not 0 <= self.arrival_radius_stability_ratio <= 1:
            raise ValueError("arrival radius stability ratio must be between 0 and 1")
        if self.arrival_size_version not in ARRIVAL_SIZE_VERSIONS:
            raise ValueError("arrival size version must be one of: v1, v2")
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
        if not isinstance(self.final_approach_enabled, bool):
            raise TypeError("final_approach_enabled must be a boolean")
        if not 0 < self.final_approach_radius_ratio < 1:
            raise ValueError("final approach radius must be in (0, 1)")
        if (
            self.final_approach_enabled
            and self.final_approach_radius_ratio > self.arrival_radius_ratio
        ):
            raise ValueError("enabled final approach radius must not exceed arrival radius")
        if not 0 < self.final_approach_bottom_ratio <= 1:
            raise ValueError("final approach bottom ratio must be in (0, 1]")
        if self.final_approach_frames < 1:
            raise ValueError("final approach frames must be positive")
        if not 0 < self.final_approach_velocity <= 0.45:
            raise ValueError("final approach velocity must be in the slow gait band (0, 0.45]")
        if self.final_approach_seconds <= 0:
            raise ValueError("final approach seconds must be positive")
        if self.arrival_action and not _ACTION_NAME_PATTERN.fullmatch(self.arrival_action):
            raise ValueError(
                "arrival action must be 1-32 alphanumeric/underscore characters or empty"
            )
        if self.approach_mode not in APPROACH_MODES:
            raise ValueError(f"approach mode must be one of: {', '.join(APPROACH_MODES)}")
        if (
            min(
                self.walk_seconds_far,
                self.walk_seconds_near,
                self.settle_seconds,
                self.sense_seconds,
                self.turn_seconds_near,
                self.turn_seconds_far,
            )
            <= 0
        ):
            raise ValueError("approach phase durations must be positive")
        if self.walk_seconds_near > self.walk_seconds_far:
            raise ValueError("walk_seconds_near must not exceed walk_seconds_far")
        if self.turn_seconds_near > self.turn_seconds_far:
            raise ValueError("turn_seconds_near must not exceed turn_seconds_far")
        if not self.align_enter_error < self.turn_full_error <= 1:
            raise ValueError("turn_full_error must exceed align_enter_error and be <= 1")
        if not isinstance(self.near_lateral_enabled, bool):
            raise TypeError("near_lateral_enabled must be a boolean")
        if not isinstance(self.single_step_turns, bool):
            raise TypeError("single_step_turns must be a boolean")
        if self.step_timeout_seconds <= 0:
            raise ValueError("step_timeout_seconds must be positive")
        if not 0 < self.near_lateral_max_error <= 1:
            raise ValueError("near_lateral_max_error must be in (0, 1]")
        if not 0.20 < self.near_lateral_speed <= 0.45:
            raise ValueError("near_lateral_speed must be in the slow gait band (0.20, 0.45]")


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
        self._arrival_ratios: deque[float] = deque(maxlen=config.arrival_frames)
        self._reliable_radius_ratios: deque[float] = deque(
            maxlen=max(config.arrival_frames, config.final_approach_frames, 5)
        )
        self._handoff_streak = 0
        self._final_started_at: float | None = None
        self._lost_streak = 0
        self._last_correction_sign = 0
        self._fine_alignment = False

    def update_config(self, config: ControllerConfig) -> None:
        """Swap tuning parameters at runtime via an atomic reference swap."""
        self.config = config
        self._arrival_ratios = deque(maxlen=config.arrival_frames)
        self._reliable_radius_ratios = deque(
            maxlen=max(config.arrival_frames, config.final_approach_frames, 5)
        )
        self._arrival_streak = 0
        self._handoff_streak = 0

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

        if self.state is ControlState.FINAL_APPROACH:
            return self._update_final_approach(detection, now)

        current = detection.has_current_target
        if current:
            error = detection.horizontal_error
            radius_ratio = detection.arrival_radius_ratio(self.config.arrival_size_version)
            self._last_target_error = error
            self._lost_streak = 0
            if not detection.bottom_clipped:
                self._reliable_radius_ratios.append(radius_ratio)
            recent_reliable_radius = max(self._reliable_radius_ratios, default=0.0)
            handoff_eligible = (
                self.config.final_approach_enabled
                and detection.bottom_clipped
                and detection.bottom_ratio >= self.config.final_approach_bottom_ratio
                and abs(error) <= self.config.arrival_max_error
                and radius_ratio + 1e-9 >= self.config.slow_radius_ratio
                and recent_reliable_radius >= self.config.final_approach_radius_ratio
            )
            if handoff_eligible:
                self._handoff_streak += 1
                self._arrival_streak = 0
                self._arrival_ratios.clear()
                if self._handoff_streak >= self.config.final_approach_frames:
                    self.state = ControlState.FINAL_APPROACH
                    self.reason = "final_approach_started"
                    self._final_started_at = now
                    return self._decision(
                        RobotCommand(velocity=self.config.final_approach_velocity),
                        error,
                        radius_ratio,
                    )
                self.reason = "final_approach_pending"
                return self._decision(RobotCommand.stop(), error, radius_ratio)
            self._handoff_streak = 0
            arrival_eligible = (
                not (self.config.final_approach_enabled and detection.bottom_clipped)
                and radius_ratio >= self.config.arrival_radius_ratio
                and abs(error) <= self.config.arrival_max_error
            )
            if arrival_eligible:
                self._arrival_streak += 1
                self._arrival_ratios.append(radius_ratio)
                stable = self._arrival_size_stable()
                if self._arrival_streak >= self.config.arrival_frames and stable:
                    self.state = ControlState.ARRIVED
                    self.reason = "arrival_confirmed"
                elif self._arrival_streak >= self.config.arrival_frames:
                    self.reason = "arrival_size_unstable"
                else:
                    self.reason = "arrival_pending"
                return self._decision(RobotCommand.stop(), error, radius_ratio)
            self._arrival_streak = 0
            self._arrival_ratios.clear()
        else:
            error = None
            radius_ratio = None
            self._lost_streak += 1
            self._arrival_streak = 0
            self._arrival_ratios.clear()
            self._align_streak = 0
            self._misalign_streak = 0
            self._handoff_streak = 0

        if self.state is ControlState.SEARCHING:
            return self._update_search(current, error, radius_ratio, now)
        if self.state is ControlState.ALIGNING:
            return self._update_align(current, error, radius_ratio, now)
        return self._update_approach(current, error, radius_ratio, now)

    def _update_final_approach(self, detection: Detection, now: float) -> ControlDecision:
        """Finish the calibrated blind-zone segment with bounded straight motion."""
        assert self._final_started_at is not None
        if now - self._final_started_at >= self.config.final_approach_seconds:
            self.state = ControlState.ARRIVED
            self.reason = "final_approach_complete"
            self._final_started_at = None
            return self._decision(RobotCommand.stop())

        if detection.has_current_target:
            error = detection.horizontal_error
            radius_ratio = detection.arrival_radius_ratio(self.config.arrival_size_version)
            self._last_target_error = error
            if abs(error) > self.config.align_exit_error:
                self.state = ControlState.ALIGNING
                self.reason = "final_approach_alignment_lost"
                self._final_started_at = None
                self._align_streak = 0
                return self._decision(RobotCommand.stop(), error, radius_ratio)
            return self._decision(
                RobotCommand(velocity=self.config.final_approach_velocity),
                error,
                radius_ratio,
            )
        return self._decision(RobotCommand(velocity=self.config.final_approach_velocity))

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
                self._last_correction_sign = 0
                self._fine_alignment = False
                return self._update_approach(True, error, radius_ratio, now)
            # Confirmation must happen at rest; minimum-active steering here
            # used to push an already aligned target out of the deadband.
            self.reason = "alignment_pending"
            return self._decision(RobotCommand.stop(), error, radius_ratio)
        else:
            self._align_streak = 0
        steer = self._clamp(error * self.config.align_steer_gain, self.config.align_steer_max)
        steer = self._minimum_active(steer)
        self.reason = "aligning"
        return self._decision(self._correction(steer, error, radius_ratio), error, radius_ratio)

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

        if (
            radius_ratio >= self.config.arrival_radius_ratio
            and abs(error) > self.config.arrival_max_error
        ):
            self.state = ControlState.ALIGNING
            self.reason = "close_target_off_center"
            self._align_streak = 0
            return self._update_align(True, error, radius_ratio, now)

        if abs(error) > self.config.align_exit_error:
            self._misalign_streak += 1
            if self._misalign_streak >= self.config.misalign_frames:
                self.state = ControlState.ALIGNING
                self.reason = "alignment_lost"
                self._align_streak = 0
                return self._update_align(True, error, radius_ratio, now)
        else:
            self._misalign_streak = 0

        # Once aligned, retain that heading throughout the hysteresis band.
        # Starting another discrete turn at the tighter entry threshold defeats
        # hysteresis and produces alternating corrections on camera sway.
        if abs(error) > self.config.align_exit_error:
            steer = self._clamp(
                error * self.config.approach_steer_gain,
                self.config.approach_steer_max,
            )
            steer = self._minimum_active(steer)
            self.reason = "approach_correction"
            return self._decision(self._correction(steer, error, radius_ratio), error, radius_ratio)

        velocity = self._approach_speed(radius_ratio)
        self.reason = "approaching"
        return self._decision(RobotCommand(velocity=velocity), error, radius_ratio)

    def _begin_search(self, now: float) -> None:
        self._search_started_at = now
        if self._last_target_error is not None:
            self._search_initial_direction = 1 if self._last_target_error >= 0 else -1
        self._clear_streaks()

    def _correction(self, steer: float, error: float, radius_ratio: float) -> RobotCommand:
        if self.config.approach_mode != "slow_realtime":
            sign = 1 if error > 0 else -1
            if self._last_correction_sign and sign != self._last_correction_sign:
                self._fine_alignment = True
            self._last_correction_sign = sign
            if self._fine_alignment or abs(error) < self.config.turn_full_error:
                # Reduce the actual discrete step by selecting small_step.
                # Never try to obtain fractional steps through shorter timing.
                steer = math.copysign(min(abs(steer), 0.35), steer)
        if (
            self.config.near_lateral_enabled
            and self.config.approach_mode != "slow_realtime"
            and radius_ratio >= self.config.slow_radius_ratio
            and abs(error) <= self.config.near_lateral_max_error
        ):
            self.reason = "near_lateral_correction"
            return RobotCommand(lateral=math.copysign(self.config.near_lateral_speed, error))
        return RobotCommand(steer=steer)

    def _clear_streaks(self) -> None:
        self._last_correction_sign = 0
        self._fine_alignment = False
        self._align_streak = 0
        self._misalign_streak = 0
        self._arrival_streak = 0
        self._arrival_ratios.clear()
        self._reliable_radius_ratios.clear()
        self._handoff_streak = 0
        self._final_started_at = None
        self._lost_streak = 0

    def _arrival_size_stable(self) -> bool:
        if len(self._arrival_ratios) < self.config.arrival_frames:
            return False
        smallest = min(self._arrival_ratios)
        largest = max(self._arrival_ratios)
        return (largest - smallest) / max(smallest, 1e-9) <= (
            self.config.arrival_radius_stability_ratio
        )

    def _approach_speed(self, radius_ratio: float) -> float:
        if radius_ratio >= self.config.arrival_radius_ratio:
            return 0.0
        far_speed, near_speed = APPROACH_SPEEDS[self.config.approach_mode]
        span = self.config.arrival_radius_ratio - self.config.slow_radius_ratio
        if span <= 0:
            return near_speed
        scale = (self.config.arrival_radius_ratio - radius_ratio) / span
        scale = max(0.0, min(1.0, scale))
        return near_speed + (far_speed - near_speed) * scale

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
