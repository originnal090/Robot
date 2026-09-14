from __future__ import annotations

import pytest

from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.model import ControlState, Detection, RobotCommand


def target(error: float, radius_ratio: float = 0.08) -> Detection:
    width, height = 640, 480
    return Detection(
        candidate_detected=True,
        detected=True,
        center_x=(error + 1.0) * width / 2.0,
        center_y=height / 2,
        radius=radius_ratio * min(width, height),
        frame_width=width,
        frame_height=height,
    )


def missing(confirmed: bool = False) -> Detection:
    return Detection(False, confirmed, None, None, None, 640, 480)


def armed_controller(**overrides) -> VisualApproachController:
    config = ControllerConfig(**overrides)
    controller = VisualApproachController(config)
    controller.arm(0.0)
    return controller


def test_search_reverses_and_times_out() -> None:
    controller = armed_controller(search_reverse_seconds=2.0, max_search_seconds=12.0)
    assert controller.update(missing(), 1.999).command.steer == pytest.approx(0.35)
    assert controller.update(missing(), 2.0).command.steer == pytest.approx(-0.35)
    decision = controller.update(missing(), 12.0)
    assert decision.state is ControlState.LOST_SAFE
    assert decision.command == RobotCommand.stop()


def test_align_approach_slow_and_arrive() -> None:
    controller = armed_controller(align_frames=3, arrival_frames=3)
    decision = controller.update(target(-0.4), 0.1)
    assert decision.state is ControlState.ALIGNING
    assert decision.command.velocity == 0
    assert decision.command.steer < 0

    for index in range(3):
        decision = controller.update(target(0.02), 0.2 + index * 0.1)
    assert decision.state is ControlState.APPROACHING
    assert decision.command.velocity == pytest.approx(0.60)  # far, normal_then_slow

    near = controller.update(target(0.0, 0.14), 0.6)
    assert near.command.velocity == pytest.approx(0.45)  # midway, normal_then_slow
    for index in range(3):
        decision = controller.update(target(0.0, 0.18), 0.7 + index * 0.1)
        assert decision.command.velocity == 0
    assert decision.state is ControlState.ARRIVED

    assert controller.update(missing(), 2.0).state is ControlState.ARRIVED


def test_current_frame_loss_stops_before_searching() -> None:
    controller = armed_controller(align_frames=1, lost_frames=3)
    controller.update(target(0.0), 0.1)
    assert controller.state is ControlState.APPROACHING

    first = controller.update(missing(confirmed=True), 0.2)
    second = controller.update(missing(confirmed=True), 0.3)
    third = controller.update(missing(), 0.4)
    assert first.command == RobotCommand.stop()
    assert second.command == RobotCommand.stop()
    assert third.state is ControlState.SEARCHING
    assert third.command.velocity == 0
    assert third.command.steer != 0


def test_alignment_hysteresis_requires_two_frames() -> None:
    controller = armed_controller(align_frames=1, misalign_frames=2)
    controller.update(target(0.0), 0.1)
    assert controller.state is ControlState.APPROACHING
    first = controller.update(target(0.15), 0.2)
    second = controller.update(target(0.15), 0.3)
    assert first.state is ControlState.APPROACHING
    assert second.state is ControlState.ALIGNING
    assert second.command.velocity == 0


def test_first_confirmed_close_target_stops_even_when_off_center() -> None:
    controller = armed_controller()
    close = controller.update(target(0.5, 0.20), 0.1)
    assert close.state is ControlState.SEARCHING
    assert close.command == RobotCommand.stop()


def test_arrival_requires_consecutive_visible_frames() -> None:
    controller = armed_controller(align_frames=1, arrival_frames=3, lost_frames=3)
    controller.update(target(0.0), 0.1)
    for index in range(3):
        close = controller.update(target(0.0, 0.18), 0.2 + index * 0.2)
        assert close.command == RobotCommand.stop()
        if index < 2:
            controller.update(missing(confirmed=True), 0.3 + index * 0.2)
            assert controller.state is ControlState.APPROACHING
    assert controller.state is ControlState.APPROACHING
    controller.update(target(0.0, 0.18), 1.0)
    controller.update(target(0.0, 0.18), 1.1)
    arrived = controller.update(target(0.0, 0.18), 1.2)
    assert arrived.state is ControlState.ARRIVED


def test_close_off_center_target_stops_during_arrival_confirmation() -> None:
    controller = armed_controller(align_frames=1, arrival_frames=3)
    controller.update(target(0.0), 0.1)
    close = controller.update(target(0.12, 0.18), 0.2)
    assert close.state is ControlState.APPROACHING
    assert close.command == RobotCommand.stop()


def test_approach_uses_tonypi_discrete_motion_modes() -> None:
    controller = armed_controller(align_frames=1)
    forward = controller.update(target(0.0), 0.1)
    assert forward.command.velocity > 0
    assert forward.command.steer == 0

    correction = controller.update(target(0.16), 0.2)
    assert correction.command.velocity == 0
    assert abs(correction.command.steer) > 0.20


def test_alignment_confirmation_stops_and_requires_consecutive_frames() -> None:
    controller = armed_controller(align_frames=3)
    controller.update(target(-0.4), 0.1)
    for t, error in [(0.2, -0.06), (0.3, 0.06)]:
        decision = controller.update(target(error), t)
        assert decision.command == RobotCommand.stop()
        assert decision.state is ControlState.ALIGNING
    controller.update(missing(), 0.4)
    assert controller.update(target(0.02), 0.5).state is ControlState.ALIGNING
    assert controller.update(target(0.02), 0.6).command == RobotCommand.stop()
    assert controller.update(target(0.02), 0.7).state is ControlState.APPROACHING


def test_aligned_heading_survives_camera_sway_in_hysteresis_band() -> None:
    controller = armed_controller(align_frames=1)
    controller.update(target(0.0), 0.0)
    for t, error in enumerate([0.10, -0.12, 0.13, -0.09]):
        decision = controller.update(target(error), t + 1)
        assert decision.state is ControlState.APPROACHING
        assert decision.command.steer == 0
        assert decision.command.velocity > 0


def test_cross_side_correction_selects_small_step_until_alignment_confirmed() -> None:
    controller = armed_controller(align_frames=1, misalign_frames=1)
    assert controller.update(target(0.6), 0.1).command.steer > 0.45
    # The same large error on the other side must change the gait itself.
    for t, error in [(0.2, -0.6), (0.3, -0.8), (0.4, 0.6)]:
        command = controller.update(target(error), t).command
        assert 0.20 < abs(command.steer) <= 0.45
        assert command.steer * error > 0
    controller.update(target(0.0), 0.5)
    assert controller.update(target(0.6), 0.6).command.steer > 0.45


def test_small_error_selects_small_step_before_crossing() -> None:
    controller = armed_controller()
    # Old gain 1.2 * 0.4 = 0.48 selected the normal turn group.
    assert 0.20 < controller.update(target(0.4), 0.1).command.steer <= 0.45


def test_reacquisition_clears_previous_small_step_latch() -> None:
    controller = armed_controller(lost_frames=1)
    controller.update(target(0.6), 0.1)
    controller.update(target(-0.6), 0.2)
    controller.update(missing(), 0.3)
    assert controller.update(target(0.6), 0.4).command.steer > 0.45


@pytest.mark.parametrize("sign", [-1, 1])
def test_optional_near_lateral_is_exclusive_and_limited(sign: int) -> None:
    controller = armed_controller(near_lateral_enabled=True)
    near = controller.update(target(sign * 0.20, 0.12), 0.1)
    assert near.command == RobotCommand(lateral=sign * 0.30)
    for error, radius in [(sign * 0.20, 0.05), (sign * 0.5, 0.12)]:
        command = controller.update(target(error, radius), 0.2).command
        assert command.lateral == 0
        assert command.steer * sign > 0
    default = armed_controller().update(target(sign * 0.20, 0.12), 0.1)
    assert default.command.lateral == 0


@pytest.mark.parametrize("values", [
    {"turn_seconds_far": float("nan")},
    {"sense_seconds": float("inf")},
    {"turn_seconds_near": 0},
    {"turn_seconds_near": 0.5, "turn_seconds_far": 0.4},
    {"turn_full_error": 0.05},
    {"near_lateral_enabled": "yes"},
    {"near_lateral_speed": 0.1},
])
def test_invalid_pulse_config_rejected(values: dict) -> None:
    with pytest.raises((TypeError, ValueError)):
        ControllerConfig(**values)


def test_invalid_controller_values_are_rejected() -> None:
    invalid = (
        {"max_search_seconds": float("nan")},
        {"search_steer": 2.0},
        {"approach_steer_max": -1.0},
        {"align_steer_gain": float("inf")},
    )
    for values in invalid:
        with pytest.raises(ValueError):
            ControllerConfig(**values)


def test_estop_is_latched_until_reset() -> None:
    controller = armed_controller()
    controller.estop("operator_estop")
    assert controller.update(target(0.0), 0.1).state is ControlState.LOST_SAFE
    controller.reset()
    assert controller.state is ControlState.IDLE
