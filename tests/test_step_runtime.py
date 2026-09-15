from __future__ import annotations

import time

import pytest

from hcirobot.app import SessionControl, run_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.model import ControlState, Detection, RobotCommand
from hcirobot.robot import RecordingRobot
from hcirobot.startup_route import StartupRoute, StartupRouteConfig
from hcirobot.video import SyntheticBallSource, SyntheticConfig


class Detector:
    def process(self, _frame):
        return Detection(True, True, 384, 240, 30, 640, 480)


class DelayedRobot(RecordingRobot):
    def __init__(self, outcome="done", polls=12):
        super().__init__()
        self.outcome = outcome
        self.polls = polls
        self.polled = 0
        self.pending = False
        self.cancelled = False
        self.heartbeats = 0

    def start_step(self, command):
        ident = super().start_step(command)
        self.pending = True
        self.polled = 0
        return ident

    def send(self, command):
        if self.pending:
            assert command == RobotCommand.stop()
            self.cancelled = True
        super().send(command)

    def step_status(self, ident):
        self.polled += 1
        if self.polled < self.polls:
            return "accepted"
        self.pending = False
        return "cancelled" if self.cancelled else self.outcome

    def heartbeat_step(self, ident):
        self.heartbeats += 1


def runtime(robot, *, sink=None, session=None, timeout=3, legacy=False):
    return run_loop(
        SyntheticBallSource(SyntheticConfig(fps=50, realtime=True)),
        Detector(), VisualApproachController(ControllerConfig(
            sense_seconds=0.06, settle_seconds=0.06,
            turn_seconds_near=0.02, turn_seconds_far=0.04,
            step_timeout_seconds=timeout, single_step_turns=not legacy,
        )), robot, armed=True, event_sink=sink, session=session, max_frames=40,
    )


def test_runtime_waits_for_done_then_settles_before_sensing():
    robot, session = DelayedRobot(), SessionControl()
    events = []
    completion_time = None

    def sink(event):
        nonlocal completion_time
        events.append(event)
        if event.kind == "step" and event.message.endswith(": done"):
            completion_time = time.perf_counter()
        if event.output_source == "step_wait":
            assert event.decision is None
            assert len(robot.steps) == 1
            assert not robot.cancelled  # No STOP emitted while waiting.
        if completion_time is not None and event.decision is not None:
            assert time.perf_counter() - completion_time >= 0.05
            session.request_stop()

    result = runtime(robot, sink=sink, session=session)
    assert result.termination == "stop_requested"
    assert completion_time is not None
    assert len(robot.steps) == 1
    assert robot.heartbeats > 0
    assert sum(e.output_source == "step_wait" for e in events) >= 5


@pytest.mark.parametrize("outcome", ["cancelled", "error"])
def test_unexpected_terminal_step_status_stops_autonomy(outcome):
    robot = DelayedRobot(outcome=outcome)
    result = runtime(robot)
    assert result.termination == "step_" + outcome
    assert result.final_state is ControlState.LOST_SAFE
    assert len(robot.steps) == 1
    assert robot.commands[-1] == RobotCommand.stop()


def test_lost_ack_times_out_without_retrying_motion():
    robot = DelayedRobot(polls=10000)
    result = runtime(robot, timeout=0.10)
    assert result.termination == "step_timeout"
    assert result.final_state is ControlState.LOST_SAFE
    assert len(robot.steps) == 1
    assert robot.cancelled


def test_disarm_cancels_pending_step_without_treating_cancel_as_failure():
    robot, session = DelayedRobot(), SessionControl()

    def sink(event):
        if event.kind == "step" and event.message.endswith(": requested"):
            session.request_disarm()
        if event.kind == "step" and event.message.endswith(": cancelled"):
            session.request_stop()

    result = runtime(robot, sink=sink, session=session)
    assert result.termination == "stop_requested"
    assert result.final_state is ControlState.IDLE
    assert robot.cancelled and len(robot.steps) == 1


def test_old_server_requires_explicit_legacy_mode():
    robot = DelayedRobot()
    robot.supports_steps = False
    result = runtime(robot)
    assert result.termination == "step_protocol_unavailable"
    assert all(c == RobotCommand.stop() for c in robot.commands)
    assert not robot.steps
    legacy = DelayedRobot()
    legacy.supports_steps = False
    result = runtime(legacy, legacy=True)
    assert result.termination == "max_frames"
    assert any(c.steer for c in legacy.commands) and not legacy.steps


def test_startup_route_moves_forward_then_takes_one_right_step_before_handoff():
    robot, session, events = DelayedRobot(polls=3), SessionControl(), []

    class RouteDetector:
        config = ControllerConfig()  # only update_config compatibility is needed here

        def process(self, _frame):
            if robot.steps and not robot.pending:
                return Detection(True, True, 320, 240, 30, 640, 480)
            return Detection(False, False, None, None, None, 640, 480)

    def sink(event):
        events.append(event)
        if event.decision is not None and event.decision.state is ControlState.ALIGNING:
            session.request_stop()

    result = run_loop(
        SyntheticBallSource(SyntheticConfig(fps=100, realtime=True)),
        RouteDetector(),
        VisualApproachController(ControllerConfig()),
        robot,
        armed=True,
        event_sink=sink,
        session=session,
        max_frames=100,
        startup_route=StartupRoute(
            StartupRouteConfig(
                enabled=True,
                forward_seconds=0.02,
                settle_seconds=0.02,
                estimated_turn_step_degrees=30,
                max_turn_degrees=120,
            )
        ),
    )
    assert result.termination == "stop_requested"
    assert len(robot.steps) == 1
    assert robot.steps[0][1].steer > 0
    assert any(command.velocity > 0 for command in robot.commands)
    assert any(event.decision and event.decision.state is ControlState.STARTUP_FORWARD for event in events)
    assert any(event.decision and event.decision.state is ControlState.STARTUP_TURN for event in events)
