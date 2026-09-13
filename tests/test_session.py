from __future__ import annotations

from hcirobot.app import RuntimeEvent, SessionControl, run_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.model import ControlState, RobotCommand
from hcirobot.robot import RecordingRobot
from hcirobot.video import SyntheticBallSource, SyntheticConfig


def build_runtime(robot: RecordingRobot, session: SessionControl, sink):
    return run_loop(
        # Realtime pacing matches a live camera: the capture loop keeps only
        # the latest frame, so an unpaced source would shed almost everything
        # before the consumer sees it.
        SyntheticBallSource(SyntheticConfig(realtime=True)),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig()),
        robot,
        armed=False,
        session=session,
        event_sink=sink,
    )


class CancellableRobot(RecordingRobot):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def test_attach_after_stop_cancels_robot_immediately() -> None:
    session = SessionControl()
    session.request_stop()
    robot = CancellableRobot()
    session.attach_robot(robot)
    assert robot.cancelled


def test_session_can_arm_then_stop_from_events() -> None:
    session = SessionControl()
    robot = RecordingRobot()
    kinds: list[str] = []

    def sink(event: RuntimeEvent) -> None:
        kinds.append(event.kind)
        if event.kind == "started":
            session.request_arm()
        if event.kind == "frame" and event.frame_count >= 12:
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
    events: list[RuntimeEvent] = []

    def sink(event: RuntimeEvent) -> None:
        events.append(event)
        if event.kind == "started":
            session.request_arm()
        if event.kind == "frame" and event.frame_count >= 2:
            session.request_estop()

    result = build_runtime(robot, session, sink)
    assert result.termination == "estop"
    assert result.final_state is ControlState.LOST_SAFE
    assert robot.commands[-1] == RobotCommand.stop()
    estop = next(event for event in events if event.kind == "estop")
    assert (estop.output_v, estop.output_steer, estop.output_grab) == (0.0, 0.0, False)
    assert estop.output_source == "estop"
    assert estop.armed is False
    finished = events[-1]
    assert finished.kind == "finished"
    assert finished.output_source == "estop"
    assert finished.armed is False


def test_stop_before_arm_never_generates_motion() -> None:
    session = SessionControl()
    robot = RecordingRobot()
    events: list[RuntimeEvent] = []

    def sink(event: RuntimeEvent) -> None:
        events.append(event)
        if event.kind == "frame" and event.frame_count >= 2:
            session.request_stop()

    result = build_runtime(robot, session, sink)
    assert result.termination == "stop_requested"
    assert all(command == RobotCommand.stop() for command in robot.commands)
    frames = [event for event in events if event.kind == "frame"]
    assert frames
    assert all(event.output_source == "unarmed_hold" for event in frames)
    assert all(not event.armed for event in frames)
    assert events[-1].output_source == "shutdown"
    assert events[-1].armed is False
