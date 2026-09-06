from __future__ import annotations

import json

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


def obstacle_event(payload: dict, *, session_id: int = 0) -> RuntimeEvent:
    return RuntimeEvent("obstacle", json.dumps(payload, ensure_ascii=False), session_id=session_id)


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


def test_obstacle_event_updates_telemetry_and_filters_foreign_session() -> None:
    model = running_model(31)
    model.apply_event(
        obstacle_event(
            {
                "state": "CAUTION",
                "reason": "sonar_close",
                "action": "hold",
                "distance": 240.0,
                "vision_blocked": True,
            },
            session_id=31,
        )
    )
    assert model.obstacle_state == "CAUTION"
    assert model.obstacle_reason == "sonar_close"
    assert model.obstacle_distance == "240"
    assert model.vision_blocked == "是"
    assert not model.latched_blocked
    model.apply_event(obstacle_event({"state": "CLEAR", "reason": "far"}, session_id=99))
    assert model.obstacle_state == "CAUTION"  # foreign-session telemetry is dropped
    assert model.obstacle_distance == "240"


def test_avoiding_transitions_count_and_fields_keep_last_reading() -> None:
    model = running_model(32)
    model.apply_event(obstacle_event({"state": "CAUTION", "reason": "close", "distance": 200}, session_id=32))
    assert model.obstacle_distance == "200"
    model.apply_event(obstacle_event({"state": "AVOIDING", "reason": "deep", "action": "backup"}, session_id=32))
    assert model.avoid_count == 1
    model.apply_event(obstacle_event({"state": "AVOIDING", "reason": "still_deep", "action": "turn"}, session_id=32))
    assert model.avoid_count == 1  # staying in AVOIDING does not re-count
    assert model.obstacle_distance == "200"  # absent distance keeps the last reading
    model.apply_event(obstacle_event({"state": "COOLDOWN", "reason": "recovering"}, session_id=32))
    model.apply_event(obstacle_event({"state": "AVOIDING", "reason": "again"}, session_id=32))
    assert model.avoid_count == 2
    model.apply_event(obstacle_event({"state": "CLEAR", "reason": "open", "vision_blocked": False}, session_id=32))
    assert model.vision_blocked == "否"


def test_obstacle_bad_json_is_logged_not_fatal() -> None:
    model = running_model(33)
    model.apply_event(RuntimeEvent("obstacle", "{not-json", session_id=33))
    assert model.obstacle_state == "无数据"
    assert not model.latched_blocked
    assert any("解析失败" in line for line in model.logs)
    model.apply_event(RuntimeEvent("obstacle", json.dumps(["wrong", "shape"]), session_id=33))
    assert model.obstacle_state == "无数据"
    model.apply_event(RuntimeEvent("obstacle", json.dumps({"reason": "missing state"}), session_id=33))
    assert model.obstacle_state == "无数据"
    model.apply_event(RuntimeEvent("obstacle", json.dumps({"state": "  "}), session_id=33))
    assert model.obstacle_state == "无数据"


def test_blocked_event_latches_blocks_start_and_reset_clears_it() -> None:
    model = running_model(34)
    model.apply_event(obstacle_event({"state": "BLOCKED", "reason": "max_avoids"}, session_id=34))
    assert model.latched_blocked
    assert not model.can_start
    assert any("BLOCKED" in line for line in model.logs)
    model.apply_event(RuntimeEvent("finished", "obstacle_blocked", session_id=34))
    assert model.session_state is SessionState.STOPPED
    assert model.latched_blocked  # the latch survives the session end
    assert not model.can_start
    assert model.can_reset
    model.reset()
    assert not model.latched_blocked
    assert model.can_start
    assert model.obstacle_state == "无数据"


def test_finished_resets_obstacle_telemetry_but_keeps_latch() -> None:
    model = running_model(35)
    model.apply_event(
        obstacle_event(
            {
                "state": "BLOCKED",
                "reason": "stuck",
                "action": "stop",
                "distance": 120,
                "vision_blocked": True,
                "avoid_count": 3,
            },
            session_id=35,
        )
    )
    model.apply_event(RuntimeEvent("finished", "obstacle_blocked", session_id=35))
    assert model.obstacle_state == "无数据"
    assert model.obstacle_reason == ""
    assert model.obstacle_distance == "--"
    assert model.vision_blocked == "--"
    assert model.avoid_count == 0
    assert model.latched_blocked  # same treatment as the estop latch


def test_fail_and_estop_reset_obstacle_telemetry() -> None:
    model = running_model(36)
    model.apply_event(obstacle_event({"state": "CAUTION", "reason": "close", "distance": 240}, session_id=36))
    model.fail("boom")
    assert model.obstacle_state == "无数据"
    assert model.obstacle_distance == "--"
    assert model.vision_blocked == "--"
    estopped = running_model(37)
    estopped.apply_event(obstacle_event({"state": "AVOIDING", "reason": "deep"}, session_id=37))
    estopped.latch_estop()
    assert estopped.obstacle_state == "无数据"
    assert estopped.avoid_count == 0


def test_begin_start_snapshots_obstacle_switch_and_resets_telemetry() -> None:
    model = running_model(38)
    model.apply_event(obstacle_event({"state": "CAUTION", "reason": "close"}, session_id=38))
    model.apply_event(RuntimeEvent("finished", "stop_requested", session_id=38))
    model.begin_start(obstacle_enabled=True)
    assert model.obstacle_enabled
    assert model.obstacle_state == "无数据"
    fresh = GuiModel()
    assert not fresh.obstacle_enabled
    fresh.begin_start()
    assert not fresh.obstacle_enabled


def test_can_toggle_obstacle_gates_by_state_and_arm() -> None:
    model = GuiModel()
    assert model.can_toggle_obstacle  # stopped and disarmed
    model.begin_start()
    assert not model.can_toggle_obstacle
    model.apply_event(RuntimeEvent("started", "session started", session_id=41))
    assert not model.can_toggle_obstacle
    model.request_stop()
    assert not model.can_toggle_obstacle  # still live while stopping
    model.apply_event(RuntimeEvent("finished", "stop_requested", session_id=41))
    assert model.can_toggle_obstacle
    faulted = running_model(42)
    faulted.fail("boom")
    assert faulted.can_toggle_obstacle  # FAILED allows reconfiguration


def test_obstacle_suppressed_key_logs_note_and_stays_tolerant() -> None:
    model = running_model(43)
    model.apply_event(
        obstacle_event(
            {"state": "AVOIDING", "reason": "deep", "action": "backup", "suppressed": True},
            session_id=43,
        )
    )
    assert model.obstacle_state == "AVOIDING"
    assert model.avoid_count == 1
    assert any("机动被压制（未武装）" in line for line in model.logs)
    # Missing key: no note, existing fields keep working.
    model.apply_event(
        obstacle_event({"state": "AVOIDING", "reason": "still_deep", "action": "turn"}, session_id=43)
    )
    assert model.avoid_count == 1  # staying in AVOIDING still does not re-count
    assert model.logs[-1] == "避障 AVOIDING（turn）：still_deep"
    # A non-bool suppressed value degrades to "no note" instead of crashing.
    model.apply_event(
        obstacle_event({"state": "CAUTION", "reason": "near", "suppressed": "yes"}, session_id=43)
    )
    assert model.obstacle_state == "CAUTION"
    assert model.logs[-1] == "避障 CAUTION：near"


def test_blocked_event_with_suppressed_keeps_latch_and_note() -> None:
    model = running_model(44)
    model.apply_event(
        obstacle_event(
            {"state": "BLOCKED", "reason": "max_avoids", "action": "stop", "suppressed": True},
            session_id=44,
        )
    )
    assert model.latched_blocked
    assert not model.can_start
    assert any("BLOCKED" in line and "机动被压制（未武装）" in line for line in model.logs)


def test_obstacle_extended_payload_keys_are_tolerated() -> None:
    model = running_model(45)
    model.apply_event(
        obstacle_event(
            {
                "state": "CAUTION",
                "reason": "sonar_close",
                "distance": None,
                "vision_blocked": None,
                "avoid_count": 2,
                "suppressed": False,
            },
            session_id=45,
        )
    )
    assert model.obstacle_state == "CAUTION"
    assert model.obstacle_distance == "--"  # null distance keeps the placeholder
    assert model.vision_blocked == "--"  # null vision_blocked keeps the placeholder
    assert model.avoid_count == 2  # explicit counter wins over transition counting
    assert not any("机动被压制" in line for line in model.logs)


def test_begin_start_logs_obstacle_semantics_only_when_enabled() -> None:
    model = GuiModel()
    model.begin_start(obstacle_enabled=True)
    assert any(
        "自治保持未武装" in line and "避障停车生效" in line and "仅在武装后执行" in line
        for line in model.logs
    )
    plain = GuiModel()
    plain.begin_start()
    assert any("自治保持未武装" in line for line in plain.logs)
    assert all("避障停车生效" not in line for line in plain.logs)
