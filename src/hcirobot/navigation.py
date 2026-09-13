"""Reactive obstacle avoidance: tuning config, experimental vision heuristic, policy.

The sonar telemetry published by robot_side is the primary obstacle source; the
vision heuristic below is an experimental fallback that only speaks up when the
sonar data is stale or absent.  The policy is a small deterministic state
machine so a session can be replayed frame by frame in tests.
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np
from numpy.typing import NDArray

Frame = NDArray[np.uint8]

_SONAR_MIN_MM = 20.0
_SONAR_MAX_MM = 5000.0


class ObstacleState(str, Enum):
    """Discrete states of the reactive avoidance policy."""

    UNKNOWN = "UNKNOWN"
    CLEAR = "CLEAR"
    CAUTION = "CAUTION"
    BACKUP = "BACKUP"
    TURN = "TURN"
    COOLDOWN = "COOLDOWN"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class ObstacleConfig:
    """Mirrors the ``[obstacle]`` section of config.toml."""

    enabled: bool = True
    stop_mm: float = 250.0
    avoid_mm: float = 150.0
    clear_mm: float = 350.0
    stale_after_s: float = 1.0
    trigger_frames: int = 2
    max_avoids: int = 3
    backup_velocity: float = -0.35
    backup_seconds: float = 0.5
    turn_steer: float = 0.35
    turn_seconds: float = 0.6
    cooldown_seconds: float = 1.0
    vision_enabled: bool = True
    vision_trigger_frames: int = 4
    vision_occupancy_ratio: float = 0.35

    def __post_init__(self) -> None:
        numeric = (
            self.stop_mm,
            self.avoid_mm,
            self.clear_mm,
            self.stale_after_s,
            self.trigger_frames,
            self.max_avoids,
            self.backup_velocity,
            self.backup_seconds,
            self.turn_steer,
            self.turn_seconds,
            self.cooldown_seconds,
            self.vision_trigger_frames,
            self.vision_occupancy_ratio,
        )
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError("all obstacle values must be finite")
        if not 0 < self.avoid_mm < self.stop_mm < self.clear_mm:
            raise ValueError("distance thresholds must satisfy 0 < avoid < stop < clear")
        if min(
            self.stale_after_s,
            self.backup_seconds,
            self.turn_seconds,
            self.cooldown_seconds,
        ) <= 0:
            raise ValueError("obstacle timing values must be positive")
        if min(self.trigger_frames, self.max_avoids, self.vision_trigger_frames) < 1:
            raise ValueError("obstacle frame/attempt thresholds must be >= 1")
        if not -1.0 <= self.backup_velocity < 0.0:
            raise ValueError("backup velocity must satisfy -1 <= backup_velocity < 0")
        if not 0.0 < self.turn_steer <= 1.0:
            raise ValueError("turn steer must be between 0 and 1")
        if not 0.0 < self.vision_occupancy_ratio <= 1.0:
            raise ValueError("vision occupancy ratio must be between 0 and 1")


def obstacle_config(values: dict) -> ObstacleConfig:
    """Build a validated config, using defaults only for omitted known keys."""
    if not isinstance(values, dict):
        raise TypeError("configuration section obstacle must be a table")
    known_names = {field.name for field in dataclasses.fields(ObstacleConfig)}
    unknown = set(values) - known_names
    if unknown:
        raise ValueError(f"unknown obstacle configuration keys: {', '.join(sorted(unknown))}")
    config = ObstacleConfig(**values)
    if not isinstance(config.enabled, bool) or not isinstance(config.vision_enabled, bool):
        raise TypeError("obstacle enabled flags must be booleans")
    return config


class VisionObstacleHeuristic:
    """Experimental dual-evidence ground-plane occupancy filter.

    Analyses the lower-center region of a BGR frame (rows 0.55-0.95, columns
    0.25-0.75) on a fixed grid.  A cell counts as occupied only when two pieces
    of evidence agree: a high edge density after Gaussian blur + Canny, and a
    mean colour that deviates from the near-ground reference sampled from the
    bottom rows of the full frame.  Textured but flat floor fires edges without
    colour deviation; a smooth wall fires colour without edges, so both stay
    clear while a large foreign object on the ground trips both.

    The heuristic is deterministic and pure per frame; the only state is the
    causal trigger streak.  Frames are "blocked" only after
    ``vision_trigger_frames`` consecutive frames whose occupancy ratio reaches
    ``vision_occupancy_ratio``; any frame below the ratio resets the streak.
    Disabled vision returns ``None`` (no opinion).
    """

    _GRID_COLS = 6
    _GRID_ROWS = 4
    _ROW_BAND = (0.55, 0.95)
    _COL_BAND = (0.25, 0.75)
    _REFERENCE_BAND = (0.94, 0.995)
    _BLUR_KERNEL = (5, 5)
    _CANNY_SIGMA = 0.33
    _CANNY_LOW_FLOOR = 25.0
    _CANNY_HIGH_FLOOR = 50.0
    _DEGENERATE_MEDIAN_LOW = 2.0
    _DEGENERATE_MEDIAN_HIGH = 253.0
    _EDGE_DENSITY_THRESHOLD = 0.10
    _COLOR_DEVIATION_THRESHOLD = 35.0

    def __init__(self, config: ObstacleConfig) -> None:
        self._enabled = bool(config.vision_enabled)
        self._trigger_frames = max(1, int(config.vision_trigger_frames))
        self._occupancy_threshold = float(config.vision_occupancy_ratio)
        self._streak = 0

    def observe_frame(self, frame: Frame) -> bool | None:
        """Return True (blocked), False (clear) or None when vision is disabled."""
        if not self._enabled:
            return None
        array = np.asarray(frame)
        if array.ndim != 3 or array.shape[2] < 3:
            self._streak = 0
            return False
        ratio = self._frame_occupancy_ratio(array)
        if ratio is None:  # frame too small to grid: no evidence either way
            self._streak = 0
            return False
        if ratio >= self._occupancy_threshold:
            self._streak += 1
        else:
            self._streak = 0
        return self._streak >= self._trigger_frames

    def _frame_occupancy_ratio(self, array: NDArray[np.uint8]) -> float | None:
        height, width = array.shape[:2]
        row0, row1 = int(height * self._ROW_BAND[0]), int(height * self._ROW_BAND[1])
        col0, col1 = int(width * self._COL_BAND[0]), int(width * self._COL_BAND[1])
        region = array[row0:row1, col0:col1]
        region_height, region_width = region.shape[:2]
        if region_height < self._GRID_ROWS or region_width < self._GRID_COLS:
            return None
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, self._BLUR_KERNEL, 0)
        median = float(np.median(blurred))
        if median <= self._DEGENERATE_MEDIAN_LOW or median >= self._DEGENERATE_MEDIAN_HIGH:
            # Deep shadow or blown-out illumination: the adaptive Canny band would
            # collapse (0/1) and amplify pure sensor noise into wall-to-wall edges,
            # so the region carries no usable evidence: report 0 occupancy.
            return 0.0
        low = int(max(self._CANNY_LOW_FLOOR, (1.0 - self._CANNY_SIGMA) * median))
        high = int(max(self._CANNY_HIGH_FLOOR, (1.0 + self._CANNY_SIGMA) * median))
        edges = cv2.Canny(blurred, low, high)
        ref_rows = slice(int(height * self._REFERENCE_BAND[0]), int(height * self._REFERENCE_BAND[1]))
        reference = array[ref_rows, :, :].reshape(-1, 3).mean(axis=0)
        cell_height = region_height // self._GRID_ROWS
        cell_width = region_width // self._GRID_COLS
        occupied = 0
        total = self._GRID_ROWS * self._GRID_COLS
        for grid_row in range(self._GRID_ROWS):
            for grid_col in range(self._GRID_COLS):
                row = slice(grid_row * cell_height, (grid_row + 1) * cell_height)
                col = slice(grid_col * cell_width, (grid_col + 1) * cell_width)
                cell_edges = edges[row, col]
                density = float(np.count_nonzero(cell_edges)) / cell_edges.size
                cell = region[row, col].reshape(-1, 3).mean(axis=0)
                deviation = float(np.abs(cell - reference).max())
                if density >= self._EDGE_DENSITY_THRESHOLD and deviation >= self._COLOR_DEVIATION_THRESHOLD:
                    occupied += 1
        return occupied / total


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """One reactive-avoidance verdict for the current frame."""

    action: str  # "passthrough" | "hold" | "maneuver"
    velocity: float = 0.0
    steer: float = 0.0
    seconds: float = 0.0
    reason: str = ""
    state: ObstacleState = ObstacleState.UNKNOWN


class ObstaclePolicy:
    """Deterministic reactive avoidance state machine driven by sonar + vision.

    Sonar is dictatorial whenever a fresh, in-range reading exists; vision is
    only consulted when the sonar data is stale or absent (degraded mode) and
    never overrides a clear sonar reading.  Sub-``stop_mm`` readings debounce
    for ``trigger_frames`` frames (held under CAUTION) before an avoidance
    maneuver starts; a reading below ``avoid_mm`` satisfies the trigger at
    once.  A maneuver backs up for ``backup_seconds`` then turns
    ``turn_seconds`` in the current direction (flipped whenever the previous
    avoid never saw the path clear again), followed by a passthrough cooldown
    during which a fresh obstruction must re-satisfy the full debounce.
    Exceeding ``max_avoids`` latches the terminal BLOCKED state.
    """

    def __init__(self, config: ObstacleConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self._clock = clock
        self._vision = VisionObstacleHeuristic(config)
        self._state = ObstacleState.UNKNOWN
        self._avoid_count = 0
        self._last_distance_mm: float | None = None
        self._near_streak = 0
        self._cleared_since_avoid = True
        self._turn_direction = 1
        self._latched = False
        self._phase: ObstacleState | None = None
        self._phase_started = 0.0
        self._phase_duration = 0.0
        self._phase_degraded = False
        self._cooldown_reason: str | None = None

    @property
    def state(self) -> ObstacleState:
        return self._state

    @property
    def avoid_count(self) -> int:
        return self._avoid_count

    @property
    def last_distance_mm(self) -> float | None:
        return self._last_distance_mm

    @property
    def latched_blocked(self) -> bool:
        return self._latched

    def observe_frame(self, frame: Frame) -> bool | None:
        """Delegate to the vision heuristic (None when vision is disabled)."""
        return self._vision.observe_frame(frame)

    def cancel_maneuver(self) -> None:
        """Abort an in-flight BACKUP/TURN phase and drop into COOLDOWN.

        A manual pulse that overrides a running avoidance must not leave the
        phase clock ticking, otherwise the remaining backup/turn vector would
        burst out once the pulse expires.  Calling this while no maneuver is
        active is a safe no-op; the aborted maneuver is followed by a normal
        cooldown whose decisions are reported with reason ``manual_override``.
        """
        if self._phase not in (ObstacleState.BACKUP, ObstacleState.TURN):
            return
        self._phase = ObstacleState.COOLDOWN
        self._phase_started = self._clock()
        self._phase_duration = self.config.cooldown_seconds
        self._state = ObstacleState.COOLDOWN
        self._cooldown_reason = "manual_override"

    def update(self, sonar: tuple[float, float] | None, vision_blocked: bool | None, now: float) -> PolicyDecision:
        if self._latched:
            return self._decision("hold", state=ObstacleState.BLOCKED, reason="blocked_latched")
        if self._phase in (ObstacleState.BACKUP, ObstacleState.TURN):
            return self._advance_maneuver(now)  # maneuvers run on their own clock
        cooling = self._phase is ObstacleState.COOLDOWN and now - self._phase_started < self._phase_duration
        if self._phase is ObstacleState.COOLDOWN and not cooling:
            self._phase = None
            self._cooldown_reason = None
        distance = self._valid_distance(sonar, now)
        if distance is not None:
            return self._update_distance(distance, now, cooling)
        if vision_blocked is not None:
            return self._update_vision(bool(vision_blocked), now, cooling)
        if cooling:
            return self._decision("passthrough", state=ObstacleState.COOLDOWN, reason="cooldown")
        self._state = ObstacleState.UNKNOWN
        return self._decision("passthrough", state=ObstacleState.UNKNOWN, reason="no_data")

    def _valid_distance(self, sonar: tuple[float, float] | None, now: float) -> float | None:
        if sonar is None:
            return None
        try:
            mm, timestamp = float(sonar[0]), float(sonar[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not math.isfinite(mm) or not math.isfinite(timestamp):
            return None
        if now - timestamp > self.config.stale_after_s:
            return None
        if not _SONAR_MIN_MM <= mm <= _SONAR_MAX_MM:
            return None
        return mm

    def _update_distance(self, mm: float, now: float, cooling: bool) -> PolicyDecision:
        self._last_distance_mm = mm
        if mm >= self.config.clear_mm:
            self._near_streak = 0
            self._cleared_since_avoid = True
            if cooling:
                return self._cooldown_decision()
            self._state = ObstacleState.CLEAR
            return self._decision("passthrough", state=ObstacleState.CLEAR, reason="path_clear")
        if mm >= self.config.stop_mm:
            # Hysteresis band: not close enough to count, not far enough to resume.
            if self._near_streak > 0:
                if cooling:
                    return self._cooldown_decision()
                self._state = ObstacleState.CAUTION
                return self._decision("hold", state=ObstacleState.CAUTION, reason="sonar_caution")
            if cooling:
                return self._cooldown_decision()
            self._state = ObstacleState.CLEAR
            return self._decision("passthrough", state=ObstacleState.CLEAR, reason="path_clear")
        if mm < self.config.avoid_mm and not cooling:
            self._near_streak = self.config.trigger_frames  # deep obstruction: immediate trigger
        else:
            # Inside the cooldown even a deep reading only counts one frame: the
            # full debounce must re-accumulate before the next maneuver.
            self._near_streak += 1
        if self._near_streak >= self.config.trigger_frames:
            return self._trigger_avoid(now, degraded=False)
        if cooling:
            return self._cooldown_decision()
        self._state = ObstacleState.CAUTION
        return self._decision("hold", state=ObstacleState.CAUTION, reason="sonar_caution")

    def _update_vision(self, blocked: bool, now: float, cooling: bool) -> PolicyDecision:
        if blocked:
            if cooling:
                self._near_streak += 1  # cooldown: the pre-debounced verdict must re-accumulate too
            else:
                self._near_streak = self.config.trigger_frames  # heuristic already debounced upstream
            if self._near_streak >= self.config.trigger_frames:
                return self._trigger_avoid(now, degraded=True)
        else:
            # Degraded vision-clear intentionally leaves _cleared_since_avoid
            # untouched: consecutive degraded avoids therefore flip the turn
            # direction each time, which is the intended escape behaviour when
            # only the camera is available.
            self._near_streak = 0
        if cooling:
            return self._cooldown_decision()
        self._state = ObstacleState.UNKNOWN
        return self._decision("passthrough", state=ObstacleState.UNKNOWN, reason="vision_clear_degraded")

    def _trigger_avoid(self, now: float, degraded: bool) -> PolicyDecision:
        self._near_streak = 0
        self._avoid_count += 1
        if self._avoid_count > self.config.max_avoids:
            self._latched = True
            self._phase = None
            self._state = ObstacleState.BLOCKED
            return self._decision("hold", state=ObstacleState.BLOCKED, reason="blocked_latched")
        if self._avoid_count > 1 and not self._cleared_since_avoid:
            self._turn_direction = -self._turn_direction  # still obstructed after previous avoid: try the other way
        self._cleared_since_avoid = False
        self._phase_degraded = degraded
        self._phase = ObstacleState.BACKUP
        self._phase_started = now
        self._phase_duration = self.config.backup_seconds
        self._state = ObstacleState.BACKUP
        return self._decision(
            "maneuver",
            velocity=self.config.backup_velocity,
            seconds=self.config.backup_seconds,
            state=ObstacleState.BACKUP,
            reason="avoid_backup_degraded" if degraded else "avoid_backup",
        )

    def _advance_maneuver(self, now: float) -> PolicyDecision:
        phase_end = self._phase_started + self._phase_duration
        if now < phase_end:
            if self._phase is ObstacleState.BACKUP:
                return self._backup_decision(phase_end - now)
            return self._turn_decision(phase_end - now)
        if self._phase is ObstacleState.BACKUP:
            self._phase = ObstacleState.TURN
            self._phase_started = phase_end
            self._phase_duration = self.config.turn_seconds
            self._state = ObstacleState.TURN
            return self._turn_decision(self.config.turn_seconds)
        self._phase = ObstacleState.COOLDOWN
        self._phase_started = phase_end
        self._phase_duration = self.config.cooldown_seconds
        self._cooldown_reason = None
        self._state = ObstacleState.COOLDOWN
        return self._cooldown_decision()

    def _backup_decision(self, seconds: float) -> PolicyDecision:
        self._state = ObstacleState.BACKUP
        return self._decision(
            "maneuver",
            velocity=self.config.backup_velocity,
            seconds=seconds,
            state=ObstacleState.BACKUP,
            reason="avoid_backup_degraded" if self._phase_degraded else "avoid_backup",
        )

    def _turn_decision(self, seconds: float) -> PolicyDecision:
        self._state = ObstacleState.TURN
        return self._decision(
            "maneuver",
            steer=self._turn_direction * self.config.turn_steer,
            seconds=seconds,
            state=ObstacleState.TURN,
            reason="avoid_turn_degraded" if self._phase_degraded else "avoid_turn",
        )

    def _cooldown_decision(self) -> PolicyDecision:
        self._state = ObstacleState.COOLDOWN
        return self._decision(
            "passthrough",
            state=ObstacleState.COOLDOWN,
            reason=self._cooldown_reason or ("cooldown_degraded" if self._phase_degraded else "cooldown"),
        )

    @staticmethod
    def _decision(
        action: str,
        *,
        state: ObstacleState,
        reason: str,
        velocity: float = 0.0,
        steer: float = 0.0,
        seconds: float = 0.0,
    ) -> PolicyDecision:
        return PolicyDecision(
            action=action,
            velocity=velocity,
            steer=steer,
            seconds=max(0.0, seconds),
            reason=reason,
            state=state,
        )
