from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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


# ================== 超声波距离遥测（DIST 行） ==================
def test_parse_sonar_sim_flat_is_constant() -> None:
    sample = ts.parse_sonar_sim("flat:500")
    assert sample(0.0) == 500
    assert sample(123.4) == 500


def test_parse_sonar_sim_sweep_is_deterministic_triangle() -> None:
    sample = ts.parse_sonar_sim("sweep:100:200:2.0")
    assert sample(0.0) == 100  # 从 min 出发
    assert sample(0.5) == 150
    assert sample(1.0) == 200  # 半周期到达 max
    assert sample(1.5) == 150
    assert sample(2.0) == 100  # 一个整周期后回到 min
    assert sample(3.0) == 200  # 周期重复


def test_parse_sonar_sim_steps_are_relative_to_t0() -> None:
    sample = ts.parse_sonar_sim("steps:500@1.0,200@3.5")
    assert sample(0.0) == 500
    assert sample(0.99) == 500
    assert sample(1.0) == 200
    assert sample(3.49) == 200
    assert sample(99.0) == 200  # 末段无限延续


@pytest.mark.parametrize(
    "spec",
    ["", "bogus", "sweep:200:100:1.0", "sweep:1:2:0", "steps:", "flat:", "flat"],
)
def test_parse_sonar_sim_rejects_invalid_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        ts.parse_sonar_sim(spec)


def test_build_distance_source_none_without_sim_or_sonar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TONYPI_SONAR_SIM", raising=False)
    monkeypatch.setattr(ts, "SONAR_MODULE", None)
    monkeypatch.setattr(ts, "dry_run_enabled", lambda: True)
    assert ts.build_distance_source() is None


def test_build_distance_source_prefers_sim_over_real_sonar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TONYPI_SONAR_SIM", "flat:432")

    class _FakeSonar:
        def getDistance(self) -> int:
            raise AssertionError("模拟源存在时不应触碰真实 Sonar")

    monkeypatch.setattr(ts, "SONAR_MODULE", SimpleNamespace(Sonar=_FakeSonar))
    monkeypatch.setattr(ts, "dry_run_enabled", lambda: False)
    source = ts.build_distance_source()
    assert source is not None
    assert source() == 432


def test_build_distance_source_uses_real_sonar_and_keeps_99999_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TONYPI_SONAR_SIM", raising=False)
    monkeypatch.setattr(ts, "dry_run_enabled", lambda: False)

    class _FakeSonar:
        def getDistance(self) -> int:
            return 99999  # 课程 SDK：传感器未连接哨兵，原样转发由 PC 端过滤

    monkeypatch.setattr(ts, "SONAR_MODULE", SimpleNamespace(Sonar=_FakeSonar))
    source = ts.build_distance_source()
    assert source is not None
    assert source() == 99999


def test_build_distance_source_bad_sim_spec_disables_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TONYPI_SONAR_SIM", "nonsense")
    assert ts.build_distance_source() is None


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data.decode("utf-8"))


def _run_distance_loop(service: ts.TonyPiService, source, seconds: float) -> list[str]:
    fake = _FakeConn()
    service._conn = fake
    done = threading.Event()
    worker = threading.Thread(target=service._distance_loop, args=(source, done), daemon=True)
    worker.start()
    time.sleep(seconds)
    done.set()
    worker.join(2.0)
    assert not worker.is_alive()
    return fake.sent


def test_distance_loop_sends_dist_beats_from_sim_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TONYPI_SONAR_SIM", "steps:100@0.2,200@0.4,300@0.6")
    monkeypatch.setattr(ts, "DIST_INTERVAL_S", 0.05)
    source = ts.build_distance_source()
    assert source is not None
    sent = _run_distance_loop(ts.TonyPiService(), source, seconds=1.0)
    assert sent, "distance loop 应至少发送一拍"
    assert all(line.startswith("DIST:") for line in sent)
    assert sent[0] == "DIST:100\n"  # 第一拍在第一个台阶切换前
    assert sent[-1] == "DIST:300\n"  # 最后一拍已越过 0.6s 台阶
    assert "DIST:200\n" in sent  # 中间台阶也被观测到
    # 节拍稳定性：1.0s / 0.05s 至少应发出大部分拍（None 拍不存在，不会缺拍）
    assert len(sent) >= 10


def test_distance_loop_skips_beats_when_source_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ts, "DIST_INTERVAL_S", 0.05)
    calls = {"n": 0}

    def none_source() -> int | None:
        calls["n"] += 1
        return None

    sent = _run_distance_loop(ts.TonyPiService(), none_source, seconds=0.3)
    assert sent == []  # None 拍不发送
    assert calls["n"] > 2  # 但节拍循环本身在继续


def test_distance_loop_survives_raising_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts, "DIST_INTERVAL_S", 0.05)
    calls = {"n": 0}

    def broken_source() -> int | None:
        calls["n"] += 1
        raise RuntimeError("sensor gone")

    sent = _run_distance_loop(ts.TonyPiService(), broken_source, seconds=0.3)
    assert sent == []
    assert calls["n"] > 2  # 异常被记日志跳过，循环不退出


def test_distance_loop_sends_nothing_without_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts, "DIST_INTERVAL_S", 0.05)
    service = ts.TonyPiService()
    service._conn = None
    done = threading.Event()
    worker = threading.Thread(target=service._distance_loop, args=(lambda: 123, done), daemon=True)
    worker.start()
    time.sleep(0.2)
    done.set()
    worker.join(2.0)
    assert not worker.is_alive()  # 无连接时不发送也不崩溃


# ================== 回归：头部舵机故障不拖垮服务（M5） ==================
def test_nod_survives_servo_fault_and_session_continues(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ts, "dry_run_enabled", lambda: False)  # 覆盖 autouse dry-run

    class _BrokenBoard:
        def __init__(self) -> None:
            self.calls = 0

        def setPWMServoPulse(self, servo_id: int, pulse: int, use_time: int) -> None:
            self.calls += 1
            raise OSError("i2c servo fault")

    board = _BrokenBoard()
    session = ts.RobotSession(now=lambda: 0.0)
    plan = session.handle_line("CMD:nod")
    assert len([item for item in plan if item[0] == "head"]) == 3
    ts.execute(plan, board=board)  # 舵机 I²C 故障不应沿 execute 抛出
    assert board.calls == 3
    assert "[ERR] head servo" in capsys.readouterr().out
    # 服务会话继续：故障后同一会话仍能正常处理后续控制与命令
    plan = session.handle_line(_vector_line(0.5, 0.0))
    assert ("mode", "forward") in plan
    assert session.tick_group() == ts.ACTION_FORWARD
    plan = session.handle_line("CMD:shake")
    assert len([item for item in plan if item[0] == "head"]) == 3
    ts.execute(plan, board=board)
    assert board.calls == 6


# ================== 回归：并发发送不产生交错帧（L3） ==================
def test_color_forward_and_distance_telemetry_do_not_interleave_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RecordingConn:
        """记录整条字节流并暴露并发写峰值，用于校验帧完整性与串行化。"""

        def __init__(self) -> None:
            self.stream = bytearray()
            self.active = 0
            self.max_active = 0

        def sendall(self, data: bytes) -> None:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            time.sleep(0.001)  # 扩大写窗口：无写锁时两线程易交叉
            self.stream += data
            self.active -= 1

    monkeypatch.setattr(ts, "DIST_INTERVAL_S", 0.001)  # 提高遥测发送频率制造并发
    conn = _RecordingConn()
    service = ts.TonyPiService()
    service._conn = conn
    done = threading.Event()
    worker = threading.Thread(
        target=service._distance_loop, args=(lambda: 4321, done), daemon=True
    )
    worker.start()
    try:
        for _ in range(50):
            service._send_to_unity_text("COLOR_SIGNAL:RED")
    finally:
        done.set()
        worker.join(2.0)
    assert not worker.is_alive()

    # 连接级写锁：并发期间同一时刻至多一个线程在写
    assert conn.max_active == 1
    # 整条字节流可逐行解析为完整帧：每帧以 \n 结尾，无半行/交错
    lines = bytes(conn.stream).split(b"\n")
    assert lines[-1] == b""  # 最后一个字节是行分隔符
    frames = lines[:-1]
    assert frames
    for frame in frames:
        if frame.startswith(b"DIST:"):
            assert int(frame[len(b"DIST:") :]) == 4321
        else:
            assert frame == b"COLOR_SIGNAL:RED"
