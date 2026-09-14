from __future__ import annotations

import threading
import time

import numpy as np

from hcirobot.app import RuntimeEvent, SessionControl, run_loop, run_manual_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.gui_model import ZERO_COMMAND, GuiModel, SessionState
from hcirobot.model import ControlState, RobotCommand
from hcirobot.robot import RecordingRobot


class PacedRepeatSource:
    def __init__(self, count: int = 8, delay: float = 0.02) -> None:
        self.count = count
        self.delay = delay
        self.closed = False
        self.frame = np.zeros((48, 64, 3), dtype=np.uint8)

    def __iter__(self):
        for _ in range(self.count):
            if self.closed:
                return
            yield self.frame.copy()
            time.sleep(self.delay)

    def close(self) -> None:
        self.closed = True


class OneFrameThenBlockSource:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def __iter__(self):
        yield np.zeros((48, 64, 3), dtype=np.uint8)
        self.closed.wait(2)

    def close(self) -> None:
        self.closed.set()


class BrokenLiveSource:
    def __iter__(self):
        raise OSError("camera reconnect exhausted")
        yield

    def close(self) -> None:
        pass


def _controller() -> VisualApproachController:
    return VisualApproachController(ControllerConfig(approach_mode="slow_realtime"))


def test_repeated_frames_drop_autonomy_but_keep_manual_control_alive() -> None:
    robot = RecordingRobot()
    control = SessionControl()
    events: list[RuntimeEvent] = []

    def sink(event: RuntimeEvent) -> None:
        events.append(event)
        if event.kind == "manual_fallback":
            control.request_manual(0.4, -0.2, 0.3, source="gamepad")
        elif event.kind == "manual" and event.output_v == 0.4:
            control.request_stop()

    result = run_loop(
        PacedRepeatSource(count=100),
        RedBallDetector(DetectorConfig()),
        _controller(),
        robot,
        armed=True,
        frame_timeout_seconds=0.3,
        duplicate_frame_limit=2,
        frame_stale_stop_seconds=0.1,
        preserve_manual_on_video_failure=True,
        session=control,
        event_sink=sink,
    )

    assert result.termination == "stop_requested"
    assert result.final_state is ControlState.IDLE
    assert any(command.steer != 0 for command in robot.commands)
    assert robot.raw_payloads[-1]["v"] == 0.4
    assert robot.raw_payloads[-1]["steer"] == -0.2
    assert any(event.kind == "video_stale" for event in events)
    assert any(event.kind == "manual_fallback" for event in events)
    assert any(event.kind == "manual" for event in events)
    assert events[-1].kind == "finished"


def test_missing_new_frame_keeps_manual_control_past_video_timeout() -> None:
    robot = RecordingRobot()
    control = SessionControl()
    events: list[RuntimeEvent] = []
    stop_timer: threading.Timer | None = None
    started = time.monotonic()

    def sink(event: RuntimeEvent) -> None:
        nonlocal stop_timer
        events.append(event)
        if event.kind == "manual_fallback" and stop_timer is None:
            control.request_manual(0.3, 0.1, 0.4, source="gamepad")
            stop_timer = threading.Timer(0.2, control.request_stop)
            stop_timer.start()

    result = run_loop(
        OneFrameThenBlockSource(),
        RedBallDetector(DetectorConfig()),
        _controller(),
        robot,
        armed=True,
        frame_timeout_seconds=0.15,
        frame_stale_stop_seconds=0.03,
        duplicate_frame_limit=2,
        preserve_manual_on_video_failure=True,
        session=control,
        event_sink=sink,
    )
    if stop_timer is not None:
        stop_timer.join(timeout=1.0)

    assert result.termination == "stop_requested"
    assert time.monotonic() - started > 0.15
    stale = next(event for event in events if event.kind == "video_stale")
    assert stale.output_source == "video_stale_hold"
    assert robot.raw_payloads[-1]["v"] == 0.3
    assert robot.commands[-1] == RobotCommand.stop()


def test_manual_loop_operates_without_video_source() -> None:
    robot = RecordingRobot()
    control = SessionControl()
    events: list[RuntimeEvent] = []
    stopped = False

    def sink(event: RuntimeEvent) -> None:
        nonlocal stopped
        events.append(event)
        if event.kind == "started":
            control.request_manual(0.45, -0.2, 0.2, source="gamepad")
        elif event.kind == "manual" and event.output_v == 0.45 and not stopped:
            stopped = True
            control.request_stop()

    result = run_manual_loop(robot, session=control, event_sink=sink, poll_seconds=0.005)

    assert result.termination == "stop_requested"
    assert robot.raw_payloads
    assert robot.raw_payloads[-1]["v"] == 0.45
    assert robot.raw_payloads[-1]["steer"] == -0.2
    assert robot.commands[-1] == RobotCommand.stop()
    assert all(event.frame is None for event in events)


def test_exhausted_video_retries_preserve_robot_link_for_manual_control() -> None:
    robot = RecordingRobot()
    control = SessionControl()
    events: list[RuntimeEvent] = []

    def sink(event: RuntimeEvent) -> None:
        events.append(event)
        if event.kind == "manual_fallback":
            control.request_manual(-0.25, 0.3, 0.2, source="gamepad")
        elif event.kind == "manual" and event.output_v == -0.25:
            control.request_stop()

    result = run_loop(
        BrokenLiveSource(),
        RedBallDetector(DetectorConfig()),
        _controller(),
        robot,
        armed=True,
        frame_timeout_seconds=0.2,
        video_retry=True,
        preserve_manual_on_video_failure=True,
        session=control,
        event_sink=sink,
    )

    assert result.termination == "stop_requested"
    assert robot.raw_payloads[-1]["v"] == -0.25
    assert any(event.kind == "manual_fallback" for event in events)
    assert not any(event.kind == "error" for event in events)


def test_control_only_model_enables_manual_and_never_autonomy() -> None:
    model = GuiModel()
    model.begin_start(obstacle_enabled=True, control_only=True)
    model.apply_event(RuntimeEvent("started", session_id=7))

    assert model.session_state is SessionState.RUNNING
    assert model.video_status == "已禁用"
    assert model.can_manual
    assert not model.can_arm
    assert not model.obstacle_enabled

    model.apply_event(
        RuntimeEvent(
            "manual",
            session_id=7,
            frame_count=2,
            output_v=0.4,
            output_steer=0.1,
            output_source="gamepad",
        )
    )
    assert model.control_state == "MANUAL"
    assert model.command == "v=0.40  steer=+0.10"
    assert model.output_source == "gamepad"

    model.apply_event(RuntimeEvent("finished", "stop_requested", session_id=7))
    assert model.session_state is SessionState.STOPPED
    assert model.video_status == "已禁用"
    assert model.command == ZERO_COMMAND


def test_video_stale_switches_to_manual_without_faulting_session() -> None:
    model = GuiModel()
    model.begin_start()
    model.apply_event(RuntimeEvent("started", session_id=11))
    model.apply_event(
        RuntimeEvent(
            "video_stale",
            "检测到重复帧，已停止自治输出",
            session_id=11,
            output_source="video_stale_hold",
        )
    )
    assert model.video_status == "画面冻结"
    assert not model.can_arm

    model.apply_event(
        RuntimeEvent(
            "manual_fallback",
            "图传异常，自治已解除；手柄控制保持可用",
            session_id=11,
            output_source="video_stale_hold",
        )
    )
    assert model.session_state is SessionState.RUNNING
    assert model.control_state == "MANUAL"
    assert model.can_manual
    assert not model.fault
