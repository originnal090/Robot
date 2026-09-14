"""run_loop integration tests for the reactive obstacle policy."""

from __future__ import annotations

import inspect
import json
import threading

import pytest

from hcirobot.app import SessionControl, run_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.mock_server import MockRobotServer
from hcirobot.model import ControlState, RobotCommand
from hcirobot.navigation import ObstacleConfig, ObstaclePolicy, ObstacleState
from hcirobot.robot import RecordingRobot, TcpRobotClient
from hcirobot.video import SyntheticBallSource, SyntheticConfig

HOLD_READINGS = [200.0] + [300.0] * 90  # one close reading, then parked in the stop..clear band


class StepClock:
    """Deterministic clock: every read advances the timeline."""

    def __init__(self, step: float = 0.05) -> None:
        self.value = -step
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class TelemetryRobot(RecordingRobot):
    """RecordingRobot whose latest_distance() replays a per-update script.

    Script entries are millimetre readings stamped with the shared test clock;
    a ``("stale", mm)`` entry stamps the reading far outside stale_after_s.
    """

    def __init__(self, readings: list, clock: StepClock) -> None:
        super().__init__()
        self.readings = list(readings)
        self.clock = clock
        self.index = 0

    def latest_distance(self) -> tuple[float, float] | None:
        if self.index >= len(self.readings):
            return None
        entry = self.readings[self.index]
        self.index += 1
        if entry is None:
            return None
        if isinstance(entry, tuple):
            return (float(entry[1]), self.clock() - 10.0)
        return (float(entry), self.clock())


class BareRobot:
    """Minimal backend without distance telemetry."""

    def __init__(self) -> None:
        self.commands: list[RobotCommand] = []
        self.raw_payloads: list[dict] = []

    def send(self, command: RobotCommand) -> None:
        self.commands.append(command)

    def send_action(self, name: str) -> None:
        return None

    def send_raw(self, payload: dict) -> None:
        self.raw_payloads.append(dict(payload))

    def close(self) -> None:
        if not self.commands or self.commands[-1] != RobotCommand.stop():
            self.send(RobotCommand.stop())


def build_sink(
    events: list, session=None, arm: bool = False, manual=None, stop_after_frame: int | None = None
):
    session = session or SessionControl()

    def sink(event) -> None:
        events.append(event)
        if event.kind == "started":
            if arm:
                session.request_arm()
            if manual is not None:
                session.request_manual(*manual)
        if (
            stop_after_frame is not None
            and event.kind == "frame"
            and event.frame_count >= stop_after_frame
        ):
            session.request_stop()

    return session, sink


def obstacle_events(events: list) -> list[dict]:
    payloads = []
    for event in events:
        if event.kind == "obstacle":
            payloads.append(json.loads(event.message))
    return payloads


def default_runtime(
    robot, *, policy, armed=False, session=None, sink=None, max_frames=0, clock=None
):
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    return run_loop(
        # Realtime pacing: the capture loop keeps only the latest frame, so an
        # unpaced source sheds frames before the consumer can process them.
        SyntheticBallSource(SyntheticConfig(fps=100.0, realtime=True)),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig(approach_mode="slow_realtime")),
        robot,
        armed=armed,
        session=session,
        event_sink=sink,
        max_frames=max_frames,
        obstacle_policy=policy,
        **kwargs,
    )


def test_policy_requires_distance_telemetry() -> None:
    events: list = []
    session, sink = build_sink(events)
    with pytest.raises(
        ValueError, match="obstacle policy requires a robot backend with distance telemetry"
    ):
        default_runtime(
            BareRobot(), policy=ObstaclePolicy(ObstacleConfig()), session=session, sink=sink
        )


def test_hold_suppresses_every_motion_command() -> None:
    clock = StepClock()
    robot = TelemetryRobot(HOLD_READINGS, clock)
    policy = ObstaclePolicy(ObstacleConfig())
    events: list = []
    session, sink = build_sink(events, arm=True, stop_after_frame=20)
    result = default_runtime(
        robot, policy=policy, armed=False, session=session, sink=sink, clock=clock
    )
    assert result.termination == "stop_requested"
    assert robot.commands, "run must have sent commands"
    assert all(command == RobotCommand.stop() for command in robot.commands)
    assert robot.raw_payloads == []  # no maneuver: readings never completed the debounce
    assert policy.avoid_count == 0
    assert policy.state is ObstacleState.CAUTION
    payloads = obstacle_events(events)
    assert payloads and payloads[0]["state"] == "CAUTION"
    assert payloads[0]["action"] == "hold"
    frames = [event for event in events if event.kind == "frame"]
    assert any(event.output_source == "obstacle_hold" for event in frames)
    assert all(
        (event.output_v, event.output_steer, event.output_grab) == (0.0, 0.0, False)
        for event in frames
    )


def test_maneuver_streams_raw_vectors_then_autonomy_resumes() -> None:
    clock = StepClock()
    readings = [800.0] * 4 + [120.0] + [800.0] * 90  # one deep reading triggers a single avoid
    robot = TelemetryRobot(readings, clock)
    policy = ObstaclePolicy(ObstacleConfig())
    events: list = []
    session, sink = build_sink(events)
    result = default_runtime(
        robot, policy=policy, armed=True, session=session, sink=sink, clock=clock
    )
    raws = robot.raw_payloads
    assert len(raws) >= 2, "backup and turn segments must reach the wire as raw frames"
    backup = [payload for payload in raws if payload["v"] < 0]
    turns = [payload for payload in raws if payload["v"] == 0.0 and payload["steer"] != 0.0]
    assert backup, "backup segment missing"
    assert turns, "turn segment missing"
    assert all(payload["v"] == pytest.approx(-0.35) for payload in backup)
    assert all(payload["steer"] == pytest.approx(0.35) for payload in turns)
    assert all(payload["grab"] is False for payload in raws)
    assert all("t" in payload for payload in raws)
    # Raw payloads are consecutive (continuous streaming across frames), then stop.
    first_turn = raws.index(turns[0])
    assert raws[: len(backup)] == backup
    assert raws[len(backup) : first_turn + len(turns)] == turns
    assert policy.avoid_count == 1
    assert policy.state is ObstacleState.CLEAR
    # After the maneuver the autonomy pipeline drives the robot again.
    assert any(command.velocity > 0 for command in robot.commands)
    states = [payload["state"] for payload in obstacle_events(events)]
    # CLEAR while far, BACKUP (deep reading skips the CAUTION debounce), TURN, COOLDOWN, CLEAR again.
    assert states == ["CLEAR", "BACKUP", "TURN", "COOLDOWN", "CLEAR"]
    maneuver_frames = [
        event
        for event in events
        if event.kind == "frame" and event.output_source == "obstacle_maneuver"
    ]
    assert maneuver_frames
    assert any(event.output_v < 0 for event in maneuver_frames)
    assert any(event.output_v == 0 and event.output_steer != 0 for event in maneuver_frames)
    assert all(event.obstacle_state is not None for event in maneuver_frames)
    assert result.termination in ("arrived", "video_ended", "max_frames")


def test_blocked_latch_terminates_lost_safe() -> None:
    clock = StepClock()
    config = ObstacleConfig(backup_seconds=0.1, turn_seconds=0.1, cooldown_seconds=0.1)
    robot = TelemetryRobot([120.0] * 40, clock)  # deep obstruction forever
    policy = ObstaclePolicy(config)
    events: list = []
    session, sink = build_sink(events)
    result = default_runtime(
        robot, policy=policy, armed=True, session=session, sink=sink, clock=clock
    )
    assert result.termination == "obstacle_blocked"
    assert result.final_state is ControlState.LOST_SAFE
    assert result.states_seen[-1] is ControlState.LOST_SAFE
    assert robot.commands[-1] == RobotCommand.stop()
    assert policy.latched_blocked is True
    assert policy.avoid_count == config.max_avoids + 1
    payloads = obstacle_events(events)
    assert payloads[-1]["state"] == "BLOCKED"
    assert payloads[-1]["reason"] == "blocked_latched"
    assert robot.raw_payloads, "avoid maneuvers must have streamed before the latch"


def test_manual_override_wins_over_obstacle_hold() -> None:
    clock = StepClock()
    robot = TelemetryRobot(HOLD_READINGS, clock)
    policy = ObstaclePolicy(ObstacleConfig())
    events: list = []
    session, sink = build_sink(events, arm=True, manual=(0.3, -0.2, 5.0), stop_after_frame=15)
    result = default_runtime(
        robot, policy=policy, armed=False, session=session, sink=sink, clock=clock
    )
    assert result.termination == "stop_requested"
    assert robot.raw_payloads, "manual pulses must stream"
    assert all(payload["v"] == pytest.approx(0.3) for payload in robot.raw_payloads)
    assert all(payload["steer"] == pytest.approx(-0.2) for payload in robot.raw_payloads)
    assert not any(payload["v"] < 0 for payload in robot.raw_payloads)  # no obstacle maneuver
    assert obstacle_events(events), "policy must keep evaluating while manual is active"
    assert policy.state is ObstacleState.CAUTION
    assert policy.avoid_count == 0
    frames = [event for event in events if event.kind == "frame"]
    assert frames
    assert all(event.output_source == "manual" for event in frames)
    assert all(event.output_v == pytest.approx(0.3) for event in frames)
    assert all(event.output_steer == pytest.approx(-0.2) for event in frames)
    assert all(event.armed for event in frames)
    assert all(event.obstacle_state in {"UNKNOWN", "CAUTION"} for event in frames)


def test_obstacle_payloads_carry_telemetry_keys() -> None:
    # Regression: the GUI panel never received distance/vision_blocked/avoid_count.
    clock = StepClock()
    robot = TelemetryRobot(HOLD_READINGS, clock)
    policy = ObstaclePolicy(ObstacleConfig())
    events: list = []
    session, sink = build_sink(events, arm=True, stop_after_frame=5)
    default_runtime(robot, policy=policy, armed=False, session=session, sink=sink, clock=clock)
    payloads = obstacle_events(events)
    assert payloads, "at least the CAUTION transition must be announced"
    first = payloads[0]
    assert first["state"] == "CAUTION"
    assert first["distance"] == pytest.approx(200.0)
    assert isinstance(first["vision_blocked"], bool)  # vision is enabled: a real per-frame verdict
    assert first["avoid_count"] == 0


def test_unarmed_session_never_streams_maneuver_vectors() -> None:
    # Regression: an un-armed session used to execute avoidance maneuvers and
    # drive the robot by itself; maneuvers must degrade to a plain hold.
    config = ObstacleConfig(
        backup_seconds=0.2, turn_seconds=0.2, cooldown_seconds=0.3, max_avoids=50
    )
    robot = TelemetryRobot([120.0] * 60, StepClock())
    policy = ObstaclePolicy(config)
    events: list = []
    session, sink = build_sink(events, stop_after_frame=25)
    result = default_runtime(
        robot, policy=policy, armed=False, session=session, sink=sink, clock=StepClock()
    )
    assert result.termination == "stop_requested"
    assert robot.raw_payloads == []  # no maneuver (and no manual) vector may reach the wire
    assert robot.commands and all(command == RobotCommand.stop() for command in robot.commands)
    payloads = obstacle_events(events)
    assert any(
        payload.get("action") == "maneuver" and payload.get("suppressed") is True
        for payload in payloads
    ), "the suppressed maneuver must still be reported as an event"
    assert policy.avoid_count >= 1
    suppressed_frames = [event for event in events if event.kind == "frame"]
    assert any(event.output_source == "unarmed_hold" for event in suppressed_frames)
    assert all(event.output_v == 0 and event.output_steer == 0 for event in suppressed_frames)
    assert all(not event.armed for event in suppressed_frames)

    # The very same obstructed scene with an armed session streams the maneuver.
    armed_robot = TelemetryRobot([120.0] * 60, StepClock())
    armed_policy = ObstaclePolicy(config)
    armed_events: list = []
    armed_session, armed_sink = build_sink(armed_events, stop_after_frame=25)
    armed_result = default_runtime(
        armed_robot,
        policy=armed_policy,
        armed=True,
        session=armed_session,
        sink=armed_sink,
        clock=StepClock(),
    )
    assert armed_result.termination == "stop_requested"
    assert any(payload["v"] < 0 for payload in armed_robot.raw_payloads), (
        "armed backup must reach the wire"
    )
    assert all("suppressed" not in payload for payload in obstacle_events(armed_events))


def test_threshold_flicker_events_are_throttled() -> None:
    # Regression: CAUTION/CLEAR flapping near a threshold emitted one event per
    # frame; the change key plus the 0.2s minimum interval must damp that.
    clock = StepClock(step=0.015)
    readings = [400.0 if index % 2 == 0 else 200.0 for index in range(10)]
    robot = TelemetryRobot(readings, clock)
    policy = ObstaclePolicy(ObstacleConfig())
    events: list = []
    session, sink = build_sink(events, arm=True, stop_after_frame=10)
    result = default_runtime(
        robot, policy=policy, armed=False, session=session, sink=sink, clock=clock
    )
    assert result.termination == "stop_requested"
    payloads = obstacle_events(events)
    assert 0 < len(payloads) <= 4, (
        f"flicker must be throttled, got {len(payloads)} events for 10 frames"
    )
    assert all(payload["state"] in ("CAUTION", "CLEAR") for payload in payloads)
    assert policy.avoid_count == 0  # flicker alone must never trigger a maneuver


def test_policy_none_matches_baseline_run_loop() -> None:
    robot = RecordingRobot()
    events: list = []
    session, sink = build_sink(events)
    result = default_runtime(
        robot, policy=None, armed=True, session=session, sink=sink, clock=StepClock(step=0.1)
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
    assert obstacle_events(events) == []


def _distance_telemetry_landed() -> bool:
    try:
        parameters = inspect.signature(MockRobotServer.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False
    if "distance_source" not in parameters:
        return False
    return callable(getattr(TcpRobotClient, "latest_distance", None))


@pytest.mark.skipif(
    not _distance_telemetry_landed(),
    reason="TODO(integration-lands): mock_server distance_source telemetry not landed yet",
)
def test_tcp_mock_distance_near_obstacle_forces_stop_and_avoid(monkeypatch) -> None:
    ready = threading.Event()
    monkeypatch.setattr("hcirobot.mock_server._DIST_INTERVAL_S", 0.03)

    def distance_source() -> float:
        return 120.0

    server = MockRobotServer(port=0, watchdog=2.0, distance_source=distance_source)
    thread = threading.Thread(target=server.serve_forever, args=(ready,), daemon=True)
    thread.start()
    assert ready.wait(5.0), "mock robot server did not start"
    client = TcpRobotClient(
        "127.0.0.1", server.bound_port, connect_timeout=3.0, minimum_send_interval=0.0
    )
    client.connect()
    events: list = []
    try:
        result = run_loop(
            SyntheticBallSource(SyntheticConfig(fps=100.0, realtime=True)),
            RedBallDetector(DetectorConfig()),
            VisualApproachController(ControllerConfig(approach_mode="slow_realtime")),
            client,
            armed=True,
            max_frames=40,
            event_sink=events.append,
            obstacle_policy=ObstaclePolicy(ObstacleConfig()),
        )
    finally:
        server.stop()
        thread.join(timeout=2.0)
    assert result.termination in ("obstacle_blocked", "max_frames")
    received = [
        command for command in server.commands if isinstance(command, dict) and "v" in command
    ]
    assert any(command["v"] < 0 for command in received), "backup maneuver must reach the wire"
    assert any(command["v"] == 0.0 and command.get("steer") == 0.0 for command in received), (
        "obstacle holds must reach the wire as stop frames"
    )
    assert any(event.kind == "obstacle" for event in events)
