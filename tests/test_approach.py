from __future__ import annotations

import pytest

from hcirobot.app import run_loop
from hcirobot.approach import ApproachGate
from hcirobot.controller import (
    APPROACH_SPEEDS,
    ControllerConfig,
    VisualApproachController,
)
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.model import ControlState
from hcirobot.robot import RecordingRobot
from hcirobot.video import SyntheticBallSource, SyntheticConfig


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def gate_config(**overrides) -> ControllerConfig:
    values = {
        "approach_mode": "normal_then_slow",
        "walk_seconds_far": 1.5,
        "walk_seconds_near": 0.5,
        "settle_seconds": 0.4,
        "sense_seconds": 0.7,
    }
    values.update(overrides)
    return ControllerConfig(**values)


def test_gate_cycles_sense_walk_settle() -> None:
    clock = FakeClock()
    gate = ApproachGate(lambda: gate_config(), clock)
    assert gate.advance(None) == "sense"  # looks before it leaps
    clock.now = 0.7
    assert gate.advance(None) == "walk"  # no target seen yet -> far duration
    clock.now = 1.0
    assert gate.advance(None) == "walk"
    clock.now = 2.2
    assert gate.advance(None) == "settle"
    clock.now = 2.6
    assert gate.advance(None) == "sense"


def test_gate_walk_duration_shrinks_with_target_size() -> None:
    config = gate_config(arrival_radius_ratio=0.10, slow_radius_ratio=0.06)
    assert ApproachGate.walk_duration(None, config) == pytest.approx(1.5)
    assert ApproachGate.walk_duration(0.04, config) == pytest.approx(1.5)
    assert ApproachGate.walk_duration(0.10, config) == pytest.approx(0.5)
    assert ApproachGate.walk_duration(0.08, config) == pytest.approx(1.0)  # midway
    clock = FakeClock()
    gate = ApproachGate(lambda: config, clock)
    gate.advance(0.08)  # sense
    clock.now = config.sense_seconds
    assert gate.advance(0.08) == "walk"


def test_gate_hold_settle_aborts_walk() -> None:
    clock = FakeClock()
    gate = ApproachGate(lambda: gate_config(), clock)
    gate.advance(None)
    clock.now = 0.7
    assert gate.advance(None) == "walk"
    clock.now = 0.8
    gate.hold_settle()
    assert gate.advance(None) == "settle"


def test_approach_speed_templates_per_mode() -> None:
    cases = {
        # radius far below slow -> far speed; at arrival edge -> 0
        "fast_then_slow": (0.85, 0.30),
        "normal": (0.60, 0.60),
        "normal_then_slow": (0.60, 0.30),
        "slow_realtime": (0.30, 0.30),
    }
    assert cases == APPROACH_SPEEDS
    for mode, (far_v, near_v) in cases.items():
        config = ControllerConfig(approach_mode=mode)
        controller = VisualApproachController(config)
        assert controller._approach_speed(0.0) == pytest.approx(far_v)
        assert controller._approach_speed(config.slow_radius_ratio) == pytest.approx(far_v)
        assert controller._approach_speed(config.arrival_radius_ratio) == 0.0
        mid = (config.arrival_radius_ratio + config.slow_radius_ratio) / 2
        expected = near_v + 0.5 * (far_v - near_v)
        assert controller._approach_speed(mid) == pytest.approx(expected)


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ValueError, match="approach mode"):
        ControllerConfig(approach_mode="teleport")
    with pytest.raises(ValueError, match="positive"):
        ControllerConfig(walk_seconds_far=0.0)
    with pytest.raises(ValueError, match="walk_seconds_near"):
        ControllerConfig(walk_seconds_near=2.0, walk_seconds_far=1.0)


class StepClock:
    def __init__(self, step: float = 0.1) -> None:
        self.value = -step
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def test_gated_run_loop_reaches_arrival_with_burst_pattern() -> None:
    robot = RecordingRobot()
    events: list = []
    config = ControllerConfig(
        approach_mode="normal_then_slow",
        walk_seconds_far=0.2,
        walk_seconds_near=0.1,
        settle_seconds=0.1,
        sense_seconds=0.2,
    )
    result = run_loop(
        SyntheticBallSource(SyntheticConfig(realtime=True)),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(config),
        robot,
        armed=True,
        clock=StepClock(),
        event_sink=events.append,
    )
    assert result.final_state is ControlState.ARRIVED
    assert result.termination == "arrived"
    sources = [event.output_source for event in events if event.kind == "frame"]
    # Walk bursts repeat the sensed intent at the normal speed band; the final
    # approach command comes from the sense window at the mode's template speed.
    assert "walk_burst" in sources
    assert "settle" in sources
    assert "autonomy" in sources
    velocities = [
        event.output_v
        for event in events
        if event.kind == "frame" and event.output_source == "walk_burst"
    ]
    assert velocities and any(v == pytest.approx(0.60) for v in velocities)


def test_slow_realtime_mode_has_no_gating() -> None:
    robot = RecordingRobot()
    events: list = []
    config = ControllerConfig(approach_mode="slow_realtime")
    result = run_loop(
        SyntheticBallSource(SyntheticConfig(realtime=True)),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(config),
        robot,
        armed=True,
        clock=StepClock(),
        event_sink=events.append,
    )
    assert result.final_state is ControlState.ARRIVED
    sources = [event.output_source for event in events if event.kind == "frame"]
    assert "walk_burst" not in sources
    assert "settle" not in sources
    velocities = [event.output_v for event in events if event.kind == "frame"]
    assert velocities and all(v <= 0.30 + 1e-9 for v in velocities)
