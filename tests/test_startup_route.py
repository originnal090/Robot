from __future__ import annotations

from hcirobot.model import ControlState, Detection, RobotCommand
from hcirobot.startup_route import StartupRoute, StartupRouteConfig


def missing() -> Detection:
    return Detection(False, False, None, None, None, 640, 480)


def target() -> Detection:
    return Detection(True, True, 320.0, 240.0, 30.0, 640, 480)


def test_optional_forward_then_continuous_right_until_six_confirmed_frames() -> None:
    route = StartupRoute(
        StartupRouteConfig(
            enabled=True,
            forward_enabled=True,
            forward_seconds=1.0,
            settle_seconds=0.5,
            acquire_frames=6,
        )
    )
    route.arm(0.0)
    assert route.update(missing(), 0.9).command == RobotCommand(velocity=0.60)
    assert route.update(missing(), 1.0).command == RobotCommand.stop()
    turn = route.update(missing(), 1.5)
    assert turn.state is ControlState.STARTUP_TURN
    assert turn.command.steer > 0
    for index in range(5):
        confirming = route.update(target(), 1.6 + index / 10)
        assert not confirming.target_acquired
        assert confirming.command.steer > 0
    acquired = route.update(target(), 2.1)
    assert acquired.target_acquired
    assert acquired.command == RobotCommand.stop()
    assert not route.active


def test_forward_is_disabled_by_default() -> None:
    route = StartupRoute(StartupRouteConfig(enabled=True, forward_seconds=16))
    route.arm(0.0)
    assert route.update(missing(), 0.0).command == RobotCommand.stop()
    turn = route.update(missing(), 0.5)
    assert turn.command.velocity == 0
    assert turn.command.steer > 0


def test_forward_can_be_selected_for_one_arm_only() -> None:
    route = StartupRoute(StartupRouteConfig(enabled=True, forward_seconds=16))

    route.arm(0.0, forward=True)
    assert route.update(missing(), 1.0).command.velocity > 0
    route.reset()
    route.arm(2.0)
    assert route.update(missing(), 2.0).command == RobotCommand.stop()


def test_forward_duration_can_be_overridden_for_one_arm() -> None:
    route = StartupRoute(StartupRouteConfig(enabled=True, forward_seconds=16))

    route.arm(0.0, forward=True, forward_seconds=32.0)

    assert route.update(missing(), 31.9).command.velocity > 0
    assert route.update(missing(), 32.0).command == RobotCommand.stop()
    route.reset()
    route.arm(40.0, forward=True)
    assert route.update(missing(), 56.0).command == RobotCommand.stop()


def test_unconfirmed_candidate_does_not_end_scan() -> None:
    route = StartupRoute(StartupRouteConfig(enabled=True, acquire_frames=6))
    route.arm(0.0)
    candidate = Detection(True, False, 320.0, 240.0, 30.0, 640, 480)
    decision = route.update(candidate, 0.5)
    assert not decision.target_acquired
    assert decision.command.steer > 0


def test_target_confirmation_streak_resets_after_one_missing_frame() -> None:
    route = StartupRoute(StartupRouteConfig(enabled=True, settle_seconds=0.1, acquire_frames=6))
    route.arm(0.0)
    for index in range(3):
        assert not route.update(target(), 0.1 + index / 100).target_acquired
    assert not route.update(missing(), 0.2).target_acquired
    for index in range(5):
        assert not route.update(target(), 0.3 + index / 100).target_acquired
    assert route.update(target(), 0.4).target_acquired
