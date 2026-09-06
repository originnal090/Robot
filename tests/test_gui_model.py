from __future__ import annotations

from hcirobot.app import RuntimeEvent
from hcirobot.controller import ControlDecision
from hcirobot.gui_model import ZERO_COMMAND, GuiModel, SessionState
from hcirobot.model import ControlState, Detection, RobotCommand


def frame_event(
    state: ControlState = ControlState.IDLE,
    *,
    session_id: int = 0,
    frame_count: int = 7,
    velocity: float = 0.0,
    steer: float = 0.0,
) -> RuntimeEvent:
    detection = Detection(True, True, 320, 240, 40, 640, 480, control_confirmed=True)
    decision = ControlDecision(state, RobotCommand(velocity, steer), "test", 0.0, 40 / 480)
    return RuntimeEvent(
        "frame",
        "test",
        detection=detection,
        decision=decision,
        frame_count=frame_count,
        session_id=session_id,
    )


def running_model(session_id: int) -> GuiModel:
    model = GuiModel()
    model.begin_start()
    model.apply_event(RuntimeEvent("started", "session started", session_id=session_id))
    return model


def test_preview_does_not_arm_and_arm_requires_fresh_frame() -> None:
    model = GuiModel()
    assert model.can_start
    assert not model.can_arm
    model.begin_start()
    assert not model.armed
    model.apply_event(RuntimeEvent("started"))
    assert not model.can_arm
    model.apply_event(frame_event())
    assert model.can_arm
    assert not model.armed


def test_model_tracks_frame_and_estop_latches() -> None:
    model = GuiModel()
    model.begin_start()
    model.apply_event(RuntimeEvent("started"))
    model.apply_event(frame_event(ControlState.APPROACHING))
    assert model.frame_count == 7
    assert model.target == "已确认"
    assert model.control_state == "APPROACHING"
    model.latch_estop()
    assert model.estop_latched
    assert not model.can_start
    assert model.control_state == "LOST_SAFE"


def test_idle_estop_can_be_reset() -> None:
    model = GuiModel()
    model.latch_estop()
    assert model.session_state is SessionState.STOPPED
    assert model.can_reset
    model.reset()
    assert not model.estop_latched
    assert model.can_start


def test_reset_only_clears_stopped_fault() -> None:
    model = GuiModel()
    model.fail("network down")
    assert model.session_state is SessionState.FAILED
    assert model.can_reset
    model.reset()
    assert model.session_state is SessionState.STOPPED
    assert not model.fault
    assert not model.armed


def test_log_is_bounded() -> None:
    model = GuiModel()
    for index in range(700):
        model.append_log(str(index))
    assert len(model.logs) == 500
    assert model.logs[0] == "200"


def test_stop_request_zeroes_telemetry_and_finished_confirms() -> None:
    model = running_model(3)
    model.apply_event(
        frame_event(ControlState.APPROACHING, session_id=3, frame_count=9, velocity=0.25, steer=0.3)
    )
    assert model.command == "v=0.25  steer=+0.30"
    assert model.control_state == "APPROACHING"
    model.request_stop()
    assert model.session_state is SessionState.STOPPING
    assert model.command == ZERO_COMMAND
    assert model.control_state == "IDLE"
    assert any("已请求停止" in line for line in model.logs)
    assert not any("零向量" in line for line in model.logs)
    # The request alone is not confirmation; only finished ends the session.
    assert model.session_state is not SessionState.STOPPED
    model.apply_event(RuntimeEvent("finished", "stop_requested", session_id=3))
    assert model.session_state is SessionState.STOPPED
    assert model.command == ZERO_COMMAND
    assert model.control_state == "IDLE"
    assert model.target == "未确认"
    assert model.horizontal_error == "--"
    assert model.radius_ratio == "--"


def test_estop_zeroes_command_and_keeps_lost_safe_after_finished() -> None:
    model = running_model(1)
    model.apply_event(frame_event(ControlState.APPROACHING, session_id=1, velocity=0.2))
    model.latch_estop()
    assert model.command == ZERO_COMMAND
    assert model.control_state == "LOST_SAFE"
    model.apply_event(RuntimeEvent("finished", "estop", session_id=1))
    assert model.session_state is SessionState.STOPPED
    assert model.control_state == "LOST_SAFE"
    assert model.command == ZERO_COMMAND
    assert model.can_reset


def test_events_from_stale_session_are_ignored() -> None:
    model = running_model(7)
    assert model.session_id == 7
    model.apply_event(frame_event(ControlState.APPROACHING, session_id=99, frame_count=50))
    assert model.frame_count == 0  # late frame from a foreign session is dropped
    model.apply_event(frame_event(ControlState.APPROACHING, session_id=7, frame_count=2))
    assert model.frame_count == 2
    model.apply_event(RuntimeEvent("finished", "completed", session_id=7))
    assert model.session_state is SessionState.STOPPED
    # A same-session frame arriving after finished must not revive the session.
    model.apply_event(frame_event(ControlState.APPROACHING, session_id=7, frame_count=3))
    assert model.session_state is SessionState.STOPPED
    assert model.frame_count == 2
    assert model.command == ZERO_COMMAND
    model.apply_event(RuntimeEvent("armed", "autonomy armed", session_id=7))
    assert not model.armed


def test_begin_start_clears_previous_session_identity() -> None:
    model = running_model(4)
    model.apply_event(RuntimeEvent("finished", "completed", session_id=4))
    model.begin_start()
    assert model.session_id == 0
    model.apply_event(frame_event(ControlState.APPROACHING, session_id=4, frame_count=8))
    assert model.frame_count == 0  # old-session frame ignored before the new started event
    model.apply_event(RuntimeEvent("started", "session started", session_id=5))
    assert model.session_id == 5
    model.apply_event(frame_event(ControlState.APPROACHING, session_id=5, frame_count=1))
    assert model.frame_count == 1


def test_can_arm_requires_running_frame_and_clean_flags() -> None:
    model = GuiModel()
    assert not model.can_arm
    model.begin_start()
    assert not model.can_arm
    model.apply_event(RuntimeEvent("started", "session started", session_id=11))
    assert not model.can_arm  # started but no confirmed frame yet
    model.apply_event(frame_event(session_id=11))
    assert model.can_arm
    model.apply_event(RuntimeEvent("armed", "autonomy armed", session_id=11))
    assert not model.can_arm  # already armed
    estopped = running_model(12)
    estopped.apply_event(frame_event(session_id=12))
    estopped.latch_estop()
    assert not estopped.can_arm
    faulted = running_model(13)
    faulted.apply_event(frame_event(session_id=13))
    faulted.fail("boom")
    assert not faulted.can_arm


def test_manual_actions_gate_on_running_disarmed_clean_session() -> None:
    model = GuiModel()
    assert not model.can_manual
    model = running_model(21)
    assert model.can_manual
    model.apply_event(RuntimeEvent("armed", "autonomy armed", session_id=21))
    assert not model.can_manual
    estopped = running_model(22)
    estopped.latch_estop()
    assert not estopped.can_manual
    faulted = running_model(23)
    faulted.fail("boom")
    assert not faulted.can_manual
