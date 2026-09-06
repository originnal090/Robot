"""Unit tests for the reactive obstacle-avoidance navigation module."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import cv2
import numpy as np
import pytest

from hcirobot.navigation import (
    ObstacleConfig,
    ObstaclePolicy,
    ObstacleState,
    PolicyDecision,
    VisionObstacleHeuristic,
    obstacle_config,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def make_floor_frame(seed: int, *, obstacle: bool = False) -> np.ndarray:
    """Deterministic wood-grain floor with grain stripes and per-frame noise."""
    rng = np.random.default_rng(seed)
    frame = np.empty((480, 640, 3), dtype=np.float64)
    frame[..., 0] = 110.0
    frame[..., 1] = 135.0
    frame[..., 2] = 160.0
    for y in range(0, 480, 14):
        frame[y : y + 2, :, :] += 18.0
    frame += rng.normal(0.0, 8.0, frame.shape)
    frame = np.clip(frame, 0.0, 255.0)
    if obstacle:
        cv2.rectangle(frame, (170, 285), (470, 480), (20, 80, 120), -1)
        for x in range(170, 470, 12):
            cv2.line(frame, (x, 285), (x, 480), (10, 45, 70), 2)
        cv2.rectangle(frame, (170, 285), (470, 480), (5, 30, 50), 4)
    return frame.astype(np.uint8)


def make_dark_noise_frame() -> np.ndarray:
    """Regression frame for shadow-degeneracy false positives.

    The analysed lower-center region is pure black under 0-3 uniform sensor
    noise while the bottom reference band is bright white: before the Canny
    threshold floors landed, the collapsed 0/1 band turned the noise into
    wall-to-wall edges and the dual evidence latched a blocked verdict.
    """
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 4, (480, 640, 3)).astype(np.uint8)
    frame[240:, 160:480] = 0  # analysis region (rows 264-456, cols 160-480): pure black
    frame[int(480 * 0.94) : int(480 * 0.995), :] = 255  # bright reference band
    return frame


class TestObstacleConfig:
    def test_defaults_match_contract(self) -> None:
        config = ObstacleConfig()
        assert config.enabled is True
        assert config.stop_mm == 250.0
        assert config.avoid_mm == 150.0
        assert config.clear_mm == 350.0
        assert config.stale_after_s == 1.0
        assert config.trigger_frames == 2
        assert config.max_avoids == 3
        assert config.backup_velocity == -0.35
        assert config.backup_seconds == 0.5
        assert config.turn_steer == 0.35
        assert config.turn_seconds == 0.6
        assert config.cooldown_seconds == 1.0
        assert config.vision_enabled is True
        assert config.vision_trigger_frames == 4
        assert config.vision_occupancy_ratio == 0.35

    def test_obstacle_config_uses_defaults_for_missing_keys(self) -> None:
        assert obstacle_config({}) == ObstacleConfig()
        partial = obstacle_config({"stop_mm": 300.0, "trigger_frames": 5})
        assert partial.stop_mm == 300.0
        assert partial.trigger_frames == 5
        assert partial.avoid_mm == ObstacleConfig().avoid_mm
        full = obstacle_config(dict(dataclasses.asdict(ObstacleConfig()), turn_steer=0.5))
        assert full.turn_steer == 0.5
        assert full == ObstacleConfig(turn_steer=0.5)

    def test_boundary_values_are_accepted(self) -> None:
        config = ObstacleConfig(backup_velocity=-1.0, turn_steer=1.0, vision_occupancy_ratio=1.0)
        assert config.backup_velocity == -1.0
        assert config.turn_steer == 1.0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"avoid_mm": 0.0},
            {"avoid_mm": 250.0},  # avoid == stop
            {"stop_mm": 350.0},  # stop == clear
            {"clear_mm": 100.0},  # clear < stop
            {"stop_mm": float("nan")},
            {"stale_after_s": 0.0},
            {"backup_seconds": -0.5},
            {"turn_seconds": 0.0},
            {"cooldown_seconds": -1.0},
            {"trigger_frames": 0},
            {"max_avoids": 0},
            {"vision_trigger_frames": 0},
            {"backup_velocity": 0.0},
            {"backup_velocity": -1.5},
            {"backup_velocity": float("inf")},
            {"turn_steer": 0.0},
            {"turn_steer": 1.5},
            {"vision_occupancy_ratio": 0.0},
            {"vision_occupancy_ratio": 1.5},
        ],
    )
    def test_invalid_values_are_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            ObstacleConfig(**kwargs)


class TestVisionObstacleHeuristic:
    def test_flat_textured_floor_with_noise_stays_clear(self) -> None:
        heuristic = VisionObstacleHeuristic(ObstacleConfig())
        for seed in range(1, 7):
            assert heuristic.observe_frame(make_floor_frame(seed)) is False

    def test_large_foreign_object_reports_blocked_after_debounce(self) -> None:
        heuristic = VisionObstacleHeuristic(ObstacleConfig())  # vision_trigger_frames = 4
        results = [heuristic.observe_frame(make_floor_frame(seed, obstacle=True)) for seed in range(11, 17)]
        assert results == [False, False, False, True, True, True]

    def test_real_fixture_ball_scene_reports_clear(self) -> None:
        for name in ("frame-0050.png", "frame-0140.png", "frame-0180.png"):
            frame = cv2.imread(str(FIXTURE_DIR / name))
            assert frame is not None, name
            heuristic = VisionObstacleHeuristic(ObstacleConfig())
            for _ in range(6):  # well past vision_trigger_frames: must never latch
                assert heuristic.observe_frame(frame) is False, name

    def test_heuristic_is_deterministic(self) -> None:
        frames = [make_floor_frame(seed, obstacle=bool(seed % 2)) for seed in range(1, 6)]
        first = VisionObstacleHeuristic(ObstacleConfig())
        second = VisionObstacleHeuristic(ObstacleConfig())
        assert [first.observe_frame(frame) for frame in frames] == [second.observe_frame(frame) for frame in frames]

    def test_insufficient_frame_resets_streak(self) -> None:
        heuristic = VisionObstacleHeuristic(ObstacleConfig())
        for _ in range(3):
            assert heuristic.observe_frame(make_floor_frame(1, obstacle=True)) is False
        assert heuristic.observe_frame(make_floor_frame(2, obstacle=False)) is False
        assert heuristic.observe_frame(make_floor_frame(3, obstacle=True)) is False  # streak restarted

    def test_disabled_vision_returns_none(self) -> None:
        heuristic = VisionObstacleHeuristic(ObstacleConfig(vision_enabled=False))
        assert heuristic.observe_frame(make_floor_frame(1, obstacle=True)) is None

    def test_dark_noisy_shadow_frame_never_reports_blocked(self) -> None:
        # Regression: region median ~0 used to collapse Canny to 0/1 so the pure
        # noise passed the edge-density evidence and the dark-vs-white-reference
        # colour deviation completed the false positive from the first frames.
        heuristic = VisionObstacleHeuristic(ObstacleConfig())
        frame = make_dark_noise_frame()
        results = [heuristic.observe_frame(frame) for _ in range(10)]
        assert all(result is False for result in results)

    def test_blown_out_bright_frame_never_reports_blocked(self) -> None:
        # Degenerate saturating illumination must be treated as no-evidence too.
        frame = np.full((480, 640, 3), 255, dtype=np.uint8)
        heuristic = VisionObstacleHeuristic(ObstacleConfig())
        assert all(heuristic.observe_frame(frame) is False for _ in range(10))


class TestPolicyDecision:
    def test_defaults_and_immutability(self) -> None:
        decision = PolicyDecision(action="passthrough")
        assert decision.velocity == 0.0
        assert decision.steer == 0.0
        assert decision.seconds == 0.0
        assert decision.reason == ""
        assert decision.state is ObstacleState.UNKNOWN
        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.action = "hold"  # type: ignore[misc]


class TestObstaclePolicyStateMachine:
    @staticmethod
    def sonar(mm: float, timestamp: float) -> tuple[float, float]:
        return (mm, timestamp)

    def test_no_data_is_passthrough_unknown(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        decision = policy.update(None, None, 0.0)
        assert decision.action == "passthrough"
        assert decision.state is ObstacleState.UNKNOWN
        assert decision.reason == "no_data"
        assert policy.state is ObstacleState.UNKNOWN
        assert policy.last_distance_mm is None

    def test_single_close_reading_holds_without_maneuver(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        decision = policy.update(self.sonar(200.0, 0.0), None, 0.0)
        assert decision.action == "hold"
        assert decision.state is ObstacleState.CAUTION
        assert decision.reason == "sonar_caution"
        assert policy.avoid_count == 0
        assert policy.last_distance_mm == 200.0
        decision = policy.update(self.sonar(600.0, 0.1), None, 0.1)
        assert decision.action == "passthrough"
        assert decision.state is ObstacleState.CLEAR
        assert decision.reason == "path_clear"
        assert policy.avoid_count == 0

    def test_consecutive_close_readings_trigger_backup(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        first = policy.update(self.sonar(200.0, 0.0), None, 0.0)
        assert first.action == "hold"  # debouncing: forward motion suppressed
        second = policy.update(self.sonar(200.0, 0.1), None, 0.1)
        assert second.action == "maneuver"
        assert second.state is ObstacleState.BACKUP
        assert second.velocity == pytest.approx(-0.35)
        assert second.steer == 0.0
        assert second.seconds == pytest.approx(0.5)
        assert policy.avoid_count == 1

    def test_deep_reading_triggers_immediately(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        decision = policy.update(self.sonar(120.0, 0.0), None, 0.0)
        assert decision.action == "maneuver"
        assert decision.state is ObstacleState.BACKUP
        assert policy.avoid_count == 1

    def test_cooldown_deep_reading_must_redebounce(self) -> None:
        # Regression: the mm<avoid_mm fast path used to fire mid-cooldown even
        # though the docstring promises a full fresh debounce after a maneuver.
        policy = ObstaclePolicy(ObstacleConfig(backup_seconds=0.2, turn_seconds=0.2, cooldown_seconds=2.0))
        assert policy.update(self.sonar(200.0, 0.0), None, 0.0).action == "hold"
        assert policy.update(self.sonar(200.0, 0.1), None, 0.1).action == "maneuver"
        assert policy.update(self.sonar(800.0, 0.4), None, 0.4).state is ObstacleState.TURN
        assert policy.update(self.sonar(800.0, 0.7), None, 0.7).state is ObstacleState.COOLDOWN
        # A single deep reading inside the cooldown must not re-trigger.
        single = policy.update(self.sonar(120.0, 1.0), None, 1.0)
        assert single.action == "passthrough"
        assert single.state is ObstacleState.COOLDOWN
        assert policy.avoid_count == 1
        # A clear frame restarts the fresh debounce from zero, and one more deep
        # frame is still only strike one of two.
        assert policy.update(self.sonar(800.0, 1.1), None, 1.1).action == "passthrough"
        assert policy.update(self.sonar(120.0, 1.3), None, 1.3).action == "passthrough"
        assert policy.avoid_count == 1
        # Cooldown over: the deep-reading fast path is available again.
        assert policy.update(self.sonar(120.0, 2.8), None, 2.8).action == "maneuver"
        assert policy.avoid_count == 2

    def test_cooldown_vision_blocked_must_redebounce(self) -> None:
        # Regression: a vision-blocked frame used to bypass the cooldown
        # debounce entirely even though the heuristic streak belongs to a
        # previous (pre-maneuver) judgement.
        policy = ObstaclePolicy(ObstacleConfig(backup_seconds=0.2, turn_seconds=0.2, cooldown_seconds=2.0))
        assert policy.update(self.sonar(200.0, 0.0), None, 0.0).action == "hold"
        assert policy.update(self.sonar(200.0, 0.1), None, 0.1).action == "maneuver"
        assert policy.update(self.sonar(800.0, 0.4), None, 0.4).state is ObstacleState.TURN
        assert policy.update(self.sonar(800.0, 0.7), None, 0.7).state is ObstacleState.COOLDOWN
        # Stale sonar: one vision-blocked frame mid-cooldown is only strike one.
        first = policy.update(self.sonar(200.0, -10.0), True, 1.0)
        assert first.action == "passthrough"
        assert first.state is ObstacleState.COOLDOWN
        assert policy.avoid_count == 1
        second = policy.update(self.sonar(200.0, -10.0), True, 1.1)
        assert second.action == "maneuver"
        assert "degraded" in second.reason
        assert policy.avoid_count == 2

    def test_cancel_maneuver_aborts_residual_phase_and_enters_cooldown(self) -> None:
        # Regression: a manual pulse overriding an ongoing avoidance used to
        # leave the BACKUP/TURN phase clock running, so the residual phase
        # burst out once the pulse ended.
        now_holder = [0.3]
        policy = ObstaclePolicy(ObstacleConfig(), clock=lambda: now_holder[0])
        assert policy.update(self.sonar(200.0, 0.0), None, 0.0).action == "hold"
        start = policy.update(self.sonar(200.0, 0.1), None, 0.1)
        assert start.action == "maneuver"
        assert start.state is ObstacleState.BACKUP
        policy.cancel_maneuver()
        assert policy.state is ObstacleState.COOLDOWN
        assert policy.latched_blocked is False
        first = policy.update(self.sonar(800.0, 0.4), None, 0.4)
        assert first.action == "passthrough"
        assert first.reason == "manual_override"
        # No residual backup/turn vector may come out for the whole cooldown.
        for step in range(1, 8):
            moment = 0.4 + 0.1 * step
            decision = policy.update(self.sonar(800.0, moment), None, moment)
            assert decision.action != "maneuver"
        # Once the cooldown elapses the policy resumes normally.
        resumed = policy.update(self.sonar(800.0, 1.5), None, 1.5)
        assert resumed.action == "passthrough"
        assert resumed.state is ObstacleState.CLEAR

    def test_cancel_maneuver_is_noop_outside_maneuver(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        policy.cancel_maneuver()
        assert policy.state is ObstacleState.UNKNOWN
        assert policy.update(None, None, 0.0).reason == "no_data"

    @pytest.mark.parametrize("reading", [5.0, 6000.0])
    def test_out_of_range_sonar_is_invalid(self, reading: float) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        decision = policy.update(self.sonar(reading, 0.0), None, 0.0)
        assert decision.action == "passthrough"
        assert decision.reason == "no_data"
        assert decision.state is ObstacleState.UNKNOWN
        assert policy.last_distance_mm is None

    def test_stale_sonar_without_vision_is_no_data(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        decision = policy.update(self.sonar(200.0, 0.0), None, 2.0)  # 2.0s > stale_after_s
        assert decision.reason == "no_data"
        assert decision.state is ObstacleState.UNKNOWN

    def test_hysteresis_requires_full_clear_distance(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        assert policy.update(self.sonar(200.0, 0.0), None, 0.0).state is ObstacleState.CAUTION
        # 300mm is inside the stop..clear band: not counted, but not enough to resume either.
        band = policy.update(self.sonar(300.0, 0.1), None, 0.1)
        assert band.action == "hold"
        assert band.state is ObstacleState.CAUTION
        # 400mm >= clear_mm: path confirmed clear, streak resets.
        cleared = policy.update(self.sonar(400.0, 0.2), None, 0.2)
        assert cleared.action == "passthrough"
        assert cleared.state is ObstacleState.CLEAR
        # The debounce must start over from scratch.
        assert policy.update(self.sonar(200.0, 0.3), None, 0.3).action == "hold"
        assert policy.update(self.sonar(250.0, 0.4), None, 0.4).action == "hold"  # band holds streak
        triggered = policy.update(self.sonar(200.0, 0.5), None, 0.5)
        assert triggered.action == "maneuver"
        assert policy.avoid_count == 1

    def test_maneuver_two_phase_timing(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        assert policy.update(self.sonar(200.0, 100.0), None, 100.0).action == "hold"
        start = policy.update(self.sonar(200.0, 100.1), None, 100.1)
        assert start.state is ObstacleState.BACKUP
        assert start.seconds == pytest.approx(0.5)
        mid = policy.update(self.sonar(200.0, 100.3), None, 100.3)
        assert mid.state is ObstacleState.BACKUP
        assert mid.velocity == pytest.approx(-0.35)
        assert mid.seconds == pytest.approx(0.3)
        # Sensors cannot cut a maneuver short; even a conflicting vision flag is ignored.
        ignored = policy.update(self.sonar(800.0, 100.5), True, 100.5)
        assert ignored.action == "maneuver"
        assert ignored.state is ObstacleState.BACKUP
        assert ignored.seconds == pytest.approx(0.1)
        turn = policy.update(self.sonar(800.0, 100.6), None, 100.6)
        assert turn.action == "maneuver"
        assert turn.state is ObstacleState.TURN
        assert turn.velocity == 0.0
        assert turn.steer == pytest.approx(0.35)  # initial direction
        assert turn.seconds == pytest.approx(0.6)
        turn_late = policy.update(self.sonar(800.0, 101.0), None, 101.0)
        assert turn_late.state is ObstacleState.TURN
        assert turn_late.seconds == pytest.approx(0.2)
        cooldown = policy.update(self.sonar(800.0, 101.2), None, 101.2)
        assert cooldown.action == "passthrough"
        assert cooldown.state is ObstacleState.COOLDOWN
        assert cooldown.reason == "cooldown"
        still_cooling = policy.update(self.sonar(800.0, 101.5), None, 101.5)
        assert still_cooling.state is ObstacleState.COOLDOWN
        resumed = policy.update(self.sonar(800.0, 102.2), None, 102.2)
        assert resumed.action == "passthrough"
        assert resumed.state is ObstacleState.CLEAR
        assert resumed.reason == "path_clear"
        assert policy.avoid_count == 1

    def test_turn_direction_flips_only_while_still_blocked(self) -> None:
        # Never seeing the path clear between avoids flips the turn direction.
        stuck = ObstaclePolicy(ObstacleConfig())
        assert stuck.update(self.sonar(200.0, 0.0), None, 0.0).action == "hold"
        assert stuck.update(self.sonar(200.0, 0.1), None, 0.1).action == "maneuver"
        now = 0.3
        while now < 2.2:  # ride out backup/turn/cooldown on band readings (never >= clear_mm)
            decision = stuck.update(self.sonar(300.0, now), None, now)
            assert decision.action in ("maneuver", "passthrough")
            now = round(now + 0.2, 10)
        assert stuck.update(self.sonar(200.0, 2.3), None, 2.3).action == "hold"
        second = stuck.update(self.sonar(200.0, 2.4), None, 2.4)
        assert second.action == "maneuver"
        assert stuck.avoid_count == 2
        turn = stuck.update(self.sonar(800.0, 2.9), None, 2.9)
        assert turn.state is ObstacleState.TURN
        assert turn.steer == pytest.approx(-0.35)  # flipped

        # A confirmed clear reading between avoids keeps the original direction.
        recovered = ObstaclePolicy(ObstacleConfig())
        assert recovered.update(self.sonar(200.0, 0.0), None, 0.0).action == "hold"
        assert recovered.update(self.sonar(200.0, 0.1), None, 0.1).action == "maneuver"
        assert recovered.update(self.sonar(800.0, 0.2), None, 0.2).action == "maneuver"  # maneuver continues
        now = 0.6
        while now < 2.2:
            recovered.update(self.sonar(800.0, now), None, now)
            now = round(now + 0.2, 10)
        assert recovered.update(self.sonar(200.0, 2.3), None, 2.3).action == "hold"
        assert recovered.update(self.sonar(200.0, 2.4), None, 2.4).action == "maneuver"
        turn = recovered.update(self.sonar(800.0, 2.9), None, 2.9)
        assert turn.state is ObstacleState.TURN
        assert turn.steer == pytest.approx(0.35)  # unchanged: path was clear in between

    def test_max_avoids_latches_blocked(self) -> None:
        policy = ObstaclePolicy(
            ObstacleConfig(backup_seconds=0.1, turn_seconds=0.1, cooldown_seconds=0.1, max_avoids=1)
        )
        assert policy.update(self.sonar(200.0, 0.0), None, 0.0).action == "hold"
        assert policy.update(self.sonar(200.0, 0.1), None, 0.1).action == "maneuver"
        assert policy.latched_blocked is False
        # Ride out backup (ends 0.3) and turn (ends 0.5) and cooldown (ends 0.7).
        policy.update(self.sonar(800.0, 0.2), None, 0.2)
        policy.update(self.sonar(800.0, 0.3), None, 0.3)
        policy.update(self.sonar(800.0, 0.5), None, 0.5)
        policy.update(self.sonar(800.0, 0.6), None, 0.6)
        latched = policy.update(self.sonar(120.0, 0.7), None, 0.7)
        assert latched.action == "hold"
        assert latched.state is ObstacleState.BLOCKED
        assert latched.reason == "blocked_latched"
        assert policy.latched_blocked is True
        assert policy.avoid_count == 2
        # The latch is terminal regardless of later evidence.
        assert policy.update(self.sonar(800.0, 1.0), None, 1.0).reason == "blocked_latched"
        assert policy.update(None, True, 2.0).reason == "blocked_latched"

    def test_stale_sonar_degrades_to_vision(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        blocked = policy.update(self.sonar(200.0, 3.5), True, 5.0)  # 1.5s stale, vision says blocked
        assert blocked.action == "maneuver"
        assert blocked.state is ObstacleState.BACKUP
        assert "degraded" in blocked.reason
        assert policy.avoid_count == 1
        continuing = policy.update(self.sonar(200.0, 3.5), None, 5.2)
        assert continuing.action == "maneuver"
        assert continuing.state is ObstacleState.BACKUP
        assert "degraded" in continuing.reason
        clear = ObstaclePolicy(ObstacleConfig())
        decision = clear.update(self.sonar(200.0, 3.5), False, 5.0)
        assert decision.action == "passthrough"
        assert "degraded" in decision.reason

    def test_vision_never_overrides_clear_sonar(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        decision = policy.update(self.sonar(800.0, 0.0), True, 0.0)
        assert decision.action == "passthrough"
        assert decision.state is ObstacleState.CLEAR
        assert decision.reason == "path_clear"
        assert policy.avoid_count == 0
        # Even mid-debounce the fresh sonar verdict wins over vision.
        assert policy.update(self.sonar(200.0, 0.1), None, 0.1).action == "hold"
        resumed = policy.update(self.sonar(800.0, 0.2), True, 0.2)
        assert resumed.action == "passthrough"
        assert policy.avoid_count == 0

    def test_last_distance_mm_tracks_only_valid_readings(self) -> None:
        policy = ObstaclePolicy(ObstacleConfig())
        policy.update(self.sonar(480.0, 0.0), None, 0.0)
        assert policy.last_distance_mm == 480.0
        policy.update(self.sonar(200.0, 0.1), None, 0.1)
        assert policy.last_distance_mm == 200.0
        policy.update(self.sonar(1.0, 0.2), None, 0.2)  # out of range: ignored
        assert policy.last_distance_mm == 200.0
        policy.update(self.sonar(200.0, -50.0), None, 0.3)  # stale: ignored
        assert policy.last_distance_mm == 200.0

    def test_default_clock_is_monotonic(self) -> None:
        import inspect
        import time

        signature = inspect.signature(ObstaclePolicy.__init__)
        assert signature.parameters["clock"].default is time.monotonic
        policy = ObstaclePolicy(ObstacleConfig())
        assert policy.config == ObstacleConfig()
