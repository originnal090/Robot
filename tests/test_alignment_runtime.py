"""Exercise the actual output path with stable observations and interruptions."""
from __future__ import annotations

import pytest

from hcirobot.app import ManualCommand, SessionControl, run_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.model import Detection
from hcirobot.navigation import ObstacleConfig, ObstaclePolicy
from hcirobot.robot import RecordingRobot
from hcirobot.video import SyntheticBallSource, SyntheticConfig


class Clock:
    now = 0.0

    def __call__(self):
        self.now += 0.05
        return self.now


class Detector:
    error = 0.2

    def process(self, _frame):
        return Detection(True, True, (1 + self.error) * 320, 240, 58, 640, 480)


class Session(SessionControl):
    manual_frames = 0

    def current_manual(self):
        if self.manual_frames:
            self.manual_frames -= 1
            return ManualCommand(0.0, -0.3, 999)
        return None


class Robot(RecordingRobot):
    blocked = False

    def latest_distance(self):
        # Fresh for the entire deterministic test timeline.
        return (200 if self.blocked else 800, 0.0)


@pytest.mark.parametrize("interruption", ["manual", "obstacle", "disarm"])
def test_interruptions_cannot_replay_previously_sensed_turn(interruption):
    clock, detector, session, robot = Clock(), Detector(), Session(), Robot()
    events = []
    interrupted_at = None
    restored = False

    def sink(event):
        nonlocal interrupted_at, restored
        if event.kind != "frame":
            return
        events.append(event)
        if interrupted_at is None and event.output_source == "walk_burst":
            assert event.output_steer > 0
            interrupted_at = event.frame_count
            detector.error = -0.2
            if interruption == "manual":
                session.manual_frames = 2
            elif interruption == "obstacle":
                robot.blocked = True
            else:
                session.request_disarm()
        elif interrupted_at is not None and not restored:
            if interruption == "obstacle":
                robot.blocked = False
            elif interruption == "disarm":
                session.request_arm()
            restored = True

    result = run_loop(
        SyntheticBallSource(SyntheticConfig(fps=100, realtime=True)),
        detector, VisualApproachController(ControllerConfig(sense_seconds=0.2)), robot,
        armed=True, session=session, clock=clock, event_sink=sink, max_frames=40,
        obstacle_policy=ObstaclePolicy(ObstacleConfig(
            trigger_frames=100, vision_enabled=False, stale_after_s=100,
        )),
    )
    assert result.termination == "max_frames"
    assert result.frames == 40
    assert interrupted_at is not None
    later = [e for e in events if e.frame_count > interrupted_at]
    turns = [e for e in later if e.output_source == "walk_burst"]
    assert turns and all(e.output_steer < 0 for e in turns)
    first_turn = turns[0].frame_count
    assert any(e.output_source == "settle" and e.frame_count < first_turn for e in later)
    assert any(e.decision is not None and e.frame_count < first_turn for e in later)
    assert all(e.output_steer == e.output_v == 0 for e in later
               if e.decision is not None and e.output_source == "autonomy")


def test_lateral_burst_is_reported_and_stops_during_sensing():
    events = []
    result = run_loop(
        SyntheticBallSource(SyntheticConfig(fps=100, realtime=True)),
        Detector(), VisualApproachController(ControllerConfig(
            near_lateral_enabled=True, sense_seconds=0.2,
        )), RecordingRobot(), armed=True, clock=Clock(), event_sink=events.append,
        max_frames=30,
    )
    assert result.termination == "max_frames"
    bursts = [e for e in events if e.output_source == "walk_burst"]
    assert bursts and all(e.output_lateral == 0.3 for e in bursts)
    assert all(e.output_lateral == 0 for e in events if e.decision is not None)
    assert events[-1].output_lateral == 0
