from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "robot_side"))

import tonypi_server as ts


@pytest.fixture(autouse=True)
def _dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TONYPI_DRY_RUN", "1")


def _vector_line(v: float, steer: float) -> str:
    return json.dumps({"v": v, "steer": steer, "grab": False, "t": "t0"})


def test_vector_to_mode_deadzone_and_turn_priority() -> None:
    assert ts.vector_to_mode(0.0, 0.0) == "stand"
    assert ts.vector_to_mode(0.19, 0.0) == "stand"  # 死区内
    assert ts.vector_to_mode(-0.19, 0.0) == "stand"
    assert ts.vector_to_mode(0.21, 0.0) == "forward"
    assert ts.vector_to_mode(-0.21, 0.0) == "back"  # v<0 后退
    assert ts.vector_to_mode(0.0, 0.21) == "turn_r"
    assert ts.vector_to_mode(0.0, -0.21) == "turn_l"
    assert ts.vector_to_mode(0.9, 0.9) == "turn_r"  # 转向优先


def test_json_line_maps_to_discrete_gait_groups() -> None:
    session = ts.RobotSession(now=lambda: 100.0)
    plan = session.handle_line(_vector_line(0.5, 0.0))
    assert ("mode", "forward") in plan
    assert session.mode == "forward"
    # 与课程一致：切换模式本身不执行动作组，由步态节拍线程按 0.30s 执行
    assert session.tick_group() == ts.ACTION_FORWARD
    assert ("group", ts.ACTION_FORWARD) not in plan
    # 模式未变化时不产生新事件
    assert session.handle_line(_vector_line(0.5, 0.0)) == []
    plan = session.handle_line(_vector_line(-0.5, 0.0))
    assert ("mode", "back") in plan
    assert session.tick_group() == ts.ACTION_BACK


def test_watchdog_returns_to_stand_after_timeout() -> None:
    clock = {"t": 1000.0}
    session = ts.RobotSession(now=lambda: clock["t"])
    session.handle_line(_vector_line(0.5, 0.0))
    assert session.mode == "forward"
    clock["t"] = 1000.5
    assert session.watchdog_plan() == []  # 未超时
    clock["t"] = 1000.61  # 超过 WATCHDOG_S=0.60
    plan = session.watchdog_plan()
    assert ("mode", "stand") in plan
    assert ("group", ts.ACTION_STAND) in plan
    assert session.mode == "stand"
    assert session.tick_group() is None
    assert session.watchdog_plan() == []  # 已站立则不再触发


def test_course_cmd_mapping_preserved() -> None:
    session = ts.RobotSession(now=lambda: 10.0)
    assert session.handle_line("CMD:right_grip") == [("group", "outfire")]
    assert session.handle_line("CMD:right_trigger") == [("group", "stand_up_back")]
    assert session.handle_line("CMD:left_trigger") == [("group", "stand_up_front")]


def test_cmd_cooldown_blocks_repeated_command_within_window() -> None:
    clock = {"t": 10.0}
    session = ts.RobotSession(now=lambda: clock["t"])
    assert session.handle_line("CMD:right_grip") == [("group", "outfire")]
    assert session.handle_line("CMD:right_grip") == []  # 0.50s 冷却内被忽略
    clock["t"] = 10.0 + ts.CMD_COOLDOWN_S + 0.01
    assert session.handle_line("CMD:right_grip") == [("group", "outfire")]  # 冷却结束恢复


def test_unknown_cmd_ignored_and_counted() -> None:
    session = ts.RobotSession(now=lambda: 0.0)
    assert session.handle_line("CMD:wave") == [("unknown", "wave")]
    assert session.unknown_cmds == 1
    assert session.handle_line("CMD:wave") == []  # 冷却窗口内不重复计数
    assert session.unknown_cmds == 1
    assert session.handle_line("CMD:dance") == [("unknown", "dance")]
    assert session.unknown_cmds == 2


def test_nod_moves_pitch_servo_within_safe_range() -> None:
    session = ts.RobotSession(now=lambda: 0.0)
    plan = session.handle_line("CMD:nod")
    head = [item for item in plan if item[0] == "head"]
    assert len(head) == 3
    assert all(item[1] == ts.HEAD_PITCH_ID for item in head)
    pulses = [item[2] for item in head]
    assert all(ts.PITCH_MIN <= p <= ts.PITCH_MAX for p in pulses)
    assert head[-1][2] == ts.PITCH_CENTER  # 最后回到中立位
    assert len(set(pulses)) == 3  # 上摆/下摆/中立，摆幅非零
    assert all(item[3] == ts.HEAD_STEP_MS for item in head)


def test_shake_moves_yaw_servo_within_safe_range() -> None:
    session = ts.RobotSession(now=lambda: 0.0)
    plan = session.handle_line("CMD:shake")
    head = [item for item in plan if item[0] == "head"]
    assert len(head) == 3
    assert all(item[1] == ts.HEAD_YAW_ID for item in head)
    pulses = [item[2] for item in head]
    assert all(ts.YAW_MIN <= p <= ts.YAW_MAX for p in pulses)
    assert head[-1][2] == ts.YAW_CENTER
    assert len(set(pulses)) == 3


def test_nod_and_shake_do_not_change_gait_mode() -> None:
    session = ts.RobotSession(now=lambda: 0.0)
    session.handle_line(_vector_line(0.5, 0.0))
    session.now = lambda: 10.0  # 越过冷却窗口
    assert session.handle_line("CMD:nod")[0][0] == "head"
    assert session.mode == "forward"  # 头部动作不影响连续步态


def test_stand_cmd_halts_gait_and_runs_stand_group() -> None:
    session = ts.RobotSession(now=lambda: 0.0)
    session.handle_line(_vector_line(0.5, 0.0))
    assert session.mode == "forward"
    plan = session.handle_line("CMD:stand")
    assert ("mode", "stand") in plan
    assert ("group", ts.ACTION_STAND) in plan
    assert session.mode == "stand"
    assert session.tick_group() is None


def test_malformed_lines_are_ignored() -> None:
    session = ts.RobotSession(now=lambda: 0.0)
    assert session.handle_line("") == []
    assert session.handle_line("   ") == []
    assert session.handle_line("not json") == []
    assert session.handle_line('["v"]') == []  # JSON 但不是对象
    assert session.handle_line('{"v": "abc", "steer": 0}') == []  # 数值非法
    assert session.mode == "stand"
    assert session.unknown_cmds == 0


def test_head_swing_clamps_to_pulse_and_axis_limits() -> None:
    plan = ts.head_swing(ts.HEAD_PITCH_ID, ts.PITCH_CENTER, 10_000, (ts.PITCH_MIN, ts.PITCH_MAX))
    for item in plan:
        assert ts.PULSE_MIN <= item[2] <= ts.PULSE_MAX
        assert ts.PITCH_MIN <= item[2] <= ts.PITCH_MAX
    plan = ts.head_swing(ts.HEAD_YAW_ID, ts.YAW_CENTER, 10_000, (ts.YAW_MIN, ts.YAW_MAX))
    for item in plan:
        assert ts.YAW_MIN <= item[2] <= ts.YAW_MAX


def test_dry_run_executor_prints_without_hardware(capsys: pytest.CaptureFixture[str]) -> None:
    ts.execute(ts.nod_plan())
    out = capsys.readouterr().out
    assert "[DRY] head servo 1" in out
    ts.execute(ts.shake_plan())
    assert "[DRY] head servo 2" in capsys.readouterr().out
    ts.execute([("group", ts.ACTION_STAND)])
    assert "[DRY] run action group: stand" in capsys.readouterr().out
    ts.execute([("unknown", "dance")])
    out = capsys.readouterr().out
    assert "dance" in out
    assert "no mapping" in out
    ts.execute([("mode", "forward")])
    assert "[MODE] forward" in capsys.readouterr().out


def test_new_connection_forces_stand_like_course_service() -> None:
    session = ts.RobotSession(now=lambda: 0.0)
    session.handle_line(_vector_line(0.5, 0.0))
    plan = session.force_stand()
    assert ("mode", "stand") in plan
    assert ("group", ts.ACTION_STAND) in plan
    # 断开时是非强制：已站立则不再执行站立动作组
    assert session.settle_stand() == []
