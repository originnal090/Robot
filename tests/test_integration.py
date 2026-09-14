from __future__ import annotations

import threading
import time

import pytest

from hcirobot.app import run_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.model import ControlState, RobotCommand
from hcirobot.robot import RecordingRobot
from hcirobot.video import SyntheticBallSource, SyntheticConfig


class StepClock:
    def __init__(self, step: float = 0.1) -> None:
        self.value = -step
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class BlockingSource:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def __iter__(self):
        self.closed.wait(5)
        return
        yield

    def close(self) -> None:
        self.closed.set()


class BrokenDetector:
    def process(self, _frame):
        raise ValueError("synthetic detector failure")


class BrokenCloseSource(SyntheticBallSource):
    def close(self) -> None:
        raise OSError("synthetic close failure")


class UnstoppableSource:
    def __iter__(self):
        threading.Event().wait(5)
        return
        yield

    def close(self) -> None:
        return None


def test_source_close_failure_still_stops_robot() -> None:
    robot = RecordingRobot()
    with pytest.raises(OSError, match="synthetic close failure"):
        run_loop(
            BrokenCloseSource(SyntheticConfig()),
            RedBallDetector(DetectorConfig()),
            VisualApproachController(ControllerConfig(approach_mode="slow_realtime")),
            robot,
            armed=True,
            max_frames=1,
        )
    assert robot.commands[-1] == RobotCommand.stop()


def test_unstoppable_source_is_reported_after_robot_stop() -> None:
    robot = RecordingRobot()
    with pytest.raises(RuntimeError, match="video capture thread did not stop"):
        run_loop(
            UnstoppableSource(),
            RedBallDetector(DetectorConfig()),
            VisualApproachController(ControllerConfig(approach_mode="slow_realtime")),
            robot,
            armed=True,
            frame_timeout_seconds=0.01,
        )
    assert robot.commands[-1] == RobotCommand.stop()


def test_early_exit_does_not_leave_capture_thread() -> None:
    baseline = {thread.ident for thread in threading.enumerate() if thread.name == "video-capture"}
    run_loop(
        SyntheticBallSource(SyntheticConfig()),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig(approach_mode="slow_realtime")),
        RecordingRobot(),
        armed=True,
        max_frames=1,
    )
    remaining = {thread.ident for thread in threading.enumerate() if thread.name == "video-capture"}
    assert remaining == baseline


def test_blocked_video_read_enters_safe_state() -> None:
    robot = RecordingRobot()
    started = time.monotonic()
    result = run_loop(
        BlockingSource(),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig(approach_mode="slow_realtime")),
        robot,
        armed=True,
        frame_timeout_seconds=0.05,
    )
    assert time.monotonic() - started < 0.5
    assert result.final_state is ControlState.LOST_SAFE
    assert result.states_seen[-1] is ControlState.LOST_SAFE
    assert robot.commands[-1] == RobotCommand.stop()


def test_runtime_error_explicitly_sends_stop() -> None:
    robot = RecordingRobot()
    events = []
    with pytest.raises(ValueError, match="synthetic detector failure"):
        run_loop(
            SyntheticBallSource(SyntheticConfig()),
            BrokenDetector(),
            VisualApproachController(ControllerConfig(approach_mode="slow_realtime")),
            robot,
            armed=True,
            event_sink=events.append,
        )
    assert robot.commands[-1] == RobotCommand.stop()
    error = events[-1]
    assert error.kind == "error"
    assert error.output_source == "shutdown"
    assert (error.output_v, error.output_steer, error.output_grab) == (0.0, 0.0, False)
    assert error.armed is False


def test_synthetic_closed_loop_reaches_target() -> None:
    robot = RecordingRobot()
    result = run_loop(
        # Realtime pacing keeps the sampled synthetic sequence deterministic
        # under latest-wins frame dropping.
        SyntheticBallSource(SyntheticConfig(fps=100.0, realtime=True)),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig(approach_mode="slow_realtime")),
        robot,
        armed=True,
        clock=StepClock(),
    )
    assert result.final_state is ControlState.ARRIVED
    assert result.states_seen == (
        ControlState.SEARCHING,
        ControlState.ALIGNING,
        ControlState.APPROACHING,
        ControlState.ARRIVED,
    )
    assert any(command.steer != 0 and command.velocity == 0 for command in robot.commands)
    assert any(command.velocity > 0 for command in robot.commands)
    assert all(not (command.velocity > 0 and command.steer != 0) for command in robot.commands)
    assert robot.commands[-1] == RobotCommand.stop()


def test_video_end_enters_safe_state() -> None:
    robot = RecordingRobot()
    result = run_loop(
        SyntheticBallSource(SyntheticConfig()),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig(arrival_radius_ratio=0.5)),
        robot,
        armed=True,
        clock=StepClock(),
    )
    assert result.final_state is ControlState.LOST_SAFE
    assert result.states_seen[-1] is ControlState.LOST_SAFE
    assert robot.commands[-1] == RobotCommand.stop()
