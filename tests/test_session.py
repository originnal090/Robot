from __future__ import annotations

from hcirobot.app import RuntimeEvent, SessionControl, run_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.model import ControlState, RobotCommand
from hcirobot.robot import RecordingRobot
from hcirobot.video import SyntheticBallSource, SyntheticConfig


def build_runtime(robot: RecordingRobot, session: SessionControl, sink):
    return run_loop(
        SyntheticBallSource(SyntheticConfig()),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig()),
        robot,
        armed=False,
        session=session,
        event_sink=sink,
    )


def test_session_can_arm_then_stop_from_events() -> None:
    session = SessionControl()
    robot = RecordingRobot()
    kinds: list[str] = []

    def sink(event: RuntimeEvent) -> None:
        kinds.append(event.kind)
        if event.kind == "started":
            session.request_arm()
        if event.kind == "frame" and event.frame_count == 12:
            session.request_stop()

    result = build_runtime(robot, session, sink)
    assert result.termination == "stop_requested"
    assert "armed" in kinds
    assert "stopping" in kinds
    assert kinds[-1] == "finished"
    assert any(command != RobotCommand.stop() for command in robot.commands)
    assert robot.commands[-1] == RobotCommand.stop()


def test_session_estop_is_latched_and_stops_robot() -> None:
    session = SessionControl()
    robot = RecordingRobot()

    def sink(event: RuntimeEvent) -> None:
        if event.kind == "started":
            session.request_arm()
        if event.kind == "frame" and event.frame_count == 2:
            session.request_estop()

    result = build_runtime(robot, session, sink)
    assert result.termination == "estop"
    assert result.final_state is ControlState.LOST_SAFE
    assert robot.commands[-1] == RobotCommand.stop()


def test_stop_before_arm_never_generates_motion() -> None:
    session = SessionControl()
    robot = RecordingRobot()

    def sink(event: RuntimeEvent) -> None:
        if event.kind == "frame" and event.frame_count == 2:
            session.request_stop()

    result = build_runtime(robot, session, sink)
    assert result.termination == "stop_requested"
    assert all(command == RobotCommand.stop() for command in robot.commands)
