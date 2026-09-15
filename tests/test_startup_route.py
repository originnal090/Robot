from __future__ import annotations

from hcirobot.model import ControlState, Detection, RobotCommand
from hcirobot.startup_route import StartupRoute, StartupRouteConfig


def missing() -> Detection:
    return Detection(False, False, None, None, None, 640, 480)


def target() -> Detection:
    return Detection(True, True, 320.0, 240.0, 30.0, 640, 480)


def test_forward_settle_then_bounded_right_steps() -> None:
    route = StartupRoute(
        StartupRouteConfig(
            enabled=True,
            forward_seconds=1.0,
            settle_seconds=0.5,
            estimated_turn_step_degrees=30,
            max_turn_degrees=120,
        )
    )
    route.arm(0.0)
    assert route.update(missing(), 0.9).command == RobotCommand(velocity=0.60)
    assert route.update(missing(), 1.0).command == RobotCommand.stop()
    for index in range(4):
        turn = route.update(missing(), 1.5 + index)
        assert turn.state is ControlState.STARTUP_TURN
        assert turn.request_step and turn.command.steer > 0
        route.mark_step_started()
        route.mark_step_complete(1.5 + index)
        route.update(missing(), 2.0 + index)
    failed = route.update(missing(), 5.5)
    assert failed.failed
    assert failed.state is ControlState.LOST_SAFE


def test_confirmed_target_hands_off_after_turn_settle() -> None:
    route = StartupRoute(StartupRouteConfig(enabled=True, forward_seconds=0))
    route.arm(0.0)
    decision = route.update(target(), 0.5)
    assert decision.target_acquired
    assert not route.active


def test_unconfirmed_candidate_does_not_end_scan() -> None:
    route = StartupRoute(StartupRouteConfig(enabled=True, forward_seconds=0))
    route.arm(0.0)
    candidate = Detection(True, False, 320.0, 240.0, 30.0, 640, 480)
    decision = route.update(candidate, 0.5)
    assert decision.request_step
