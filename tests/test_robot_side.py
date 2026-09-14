from __future__ import annotations

import ast
import json
import socket
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


def _free_port(sock_type: int) -> int:
    with socket.socket(socket.AF_INET, sock_type) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_single_file_syntax_is_python_38_compatible() -> None:
    source = Path(ts.__file__).read_text(encoding="utf-8")
    ast.parse(source, filename=str(ts.__file__), feature_version=(3, 8))
    assert "TypeAlias" not in source


def test_legacy_dry_run_mode_is_supported() -> None:
    assert ts.load_config(environ={"TONYPI_DRY_RUN": "1"}).mode == ts.MODE_DRY_RUN
    assert ts.load_config(environ={"TONYPI_DRY_RUN": "0"}).mode == ts.MODE_HARDWARE


def test_explicit_mode_rejects_conflicting_legacy_flag() -> None:
    with pytest.raises(ts.ConfigError, match="conflicts"):
        ts.load_config(environ={"TONYPI_MODE": "hardware", "TONYPI_DRY_RUN": "1"})


def test_hardware_mode_missing_actuator_sdk_fails_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = __import__

    def missing_hiwonder(name, *args, **kwargs):
        if name == "hiwonder":
            raise ImportError("missing SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", missing_hiwonder)
    with pytest.raises(ts.StartupError, match="actuator SDK unavailable"):
        ts.prepare_runtime(ts.ServiceConfig(mode=ts.MODE_HARDWARE))


def test_hardware_mode_forbids_sonar_sim_by_default() -> None:
    with pytest.raises(ts.ConfigError, match="forbidden in hardware mode"):
        ts.load_config(environ={"TONYPI_MODE": "hardware", "TONYPI_SONAR_SIM": "flat:500"})
    config = ts.load_config(
        environ={
            "TONYPI_MODE": "hardware",
            "TONYPI_SONAR_SIM": "flat:500",
            "TONYPI_ALLOW_SIM_WITH_HARDWARE": "1",
        }
    )
    assert config.allow_sonar_sim_in_hardware is True


def test_required_sonar_in_dry_run_requires_simulation() -> None:
    with pytest.raises(ts.ConfigError, match="required Sonar"):
        ts.load_config(environ={"TONYPI_MODE": "dry-run", "TONYPI_REQUIRE_SONAR": "1"})
    config = ts.load_config(
        environ={
            "TONYPI_MODE": "dry-run",
            "TONYPI_REQUIRE_SONAR": "1",
            "TONYPI_SONAR_SIM": "flat:500",
        }
    )
    assert config.require_sonar is True


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("TONYPI_PORT", "0", "port"),
        ("TONYPI_DEADZONE", "1", "deadzone"),
        ("TONYPI_WATCHDOG", "-1", "watchdog"),
        ("TONYPI_DIST_INTERVAL", "0", "dist_interval"),
        ("TONYPI_HEAD_STEP_MS", "19", "head_step"),
        ("TONYPI_HEAD_PITCH_ID", "3", "servo IDs"),
        ("TONYPI_PITCH_CENTER", "999", "pitch min/center/max"),
        ("TONYPI_ACTION_FORWARD", "bad/name", "action group"),
    ],
)
def test_config_rejects_invalid_ranges(name: str, value: str, match: str) -> None:
    with pytest.raises(ts.ConfigError, match=match):
        ts.load_config(environ={"TONYPI_MODE": "dry-run", name: value})


def test_required_sonar_preflight_requires_valid_reading() -> None:
    config = ts.ServiceConfig(mode=ts.MODE_HARDWARE, require_sonar=True)

    ts.check_sonar_reading(
        config, ts.RuntimeHardware(sonar=SimpleNamespace(getDistance=lambda: 432))
    )

    with pytest.raises(ts.StartupError, match="disconnected sentinel"):
        ts.check_sonar_reading(
            config,
            ts.RuntimeHardware(
                sonar=SimpleNamespace(getDistance=lambda: ts.SONAR_DISCONNECTED_SENTINEL)
            ),
        )

    with pytest.raises(ts.StartupError, match="read failed"):
        ts.check_sonar_reading(
            config,
            ts.RuntimeHardware(
                sonar=SimpleNamespace(getDistance=lambda: (_ for _ in ()).throw(OSError("i2c")))
            ),
        )


def test_check_mode_is_action_free(monkeypatch: pytest.MonkeyPatch) -> None:
    tcp_port = _free_port(socket.SOCK_STREAM)
    udp_port = _free_port(socket.SOCK_DGRAM)
    moved = []
    monkeypatch.setattr(ts, "execute", lambda *args, **kwargs: moved.append((args, kwargs)))
    code = ts.main(
        [
            "--mode",
            "dry-run",
            "--check",
            "--host",
            "127.0.0.1",
            "--port",
            str(tcp_port),
            "--udp-color-host",
            "127.0.0.1",
            "--udp-color-port",
            str(udp_port),
        ]
    )
    assert code == 0
    assert moved == []


def test_vector_to_mode_deadzone_and_turn_priority() -> None:
    assert ts.vector_to_mode(0.0, 0.0) == "stand"
    assert ts.vector_to_mode(0.19, 0.0) == "stand"  # 死区内
    assert ts.vector_to_mode(-0.19, 0.0) == "stand"
    assert ts.vector_to_mode(0.21, 0.0) == "forward_slow"  # 慢速档（one_step）
    assert ts.vector_to_mode(-0.21, 0.0) == "back_slow"
    assert ts.vector_to_mode(0.6, 0.0) == "forward"  # 常速档
    assert ts.vector_to_mode(0.9, 0.0) == "forward_fast"  # 快速档
    assert ts.vector_to_mode(0.0, 0.21) == "turn_r_slow"
    assert ts.vector_to_mode(0.0, -0.5) == "turn_l"
    assert ts.vector_to_mode(0.0, -0.8) == "turn_l_fast"
    assert ts.vector_to_mode(0.9, 0.9) == "turn_r_fast"  # 转向优先


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


def test_legacy_session_can_allow_head_command_while_moving() -> None:
    session = ts.RobotSession(now=lambda: 0.0)
    session.handle_line(_vector_line(0.5, 0.0))
    session.now = lambda: 10.0  # 越过冷却窗口
    assert session.handle_line("CMD:nod")[0][0] == "head"
    assert session.mode == "forward"  # 兼容直接构造 RobotSession 的旧行为


def test_configured_session_rejects_non_stand_cmd_while_moving_by_default() -> None:
    session = ts.RobotSession.from_config(ts.ServiceConfig(mode=ts.MODE_DRY_RUN))
    session.handle_line(_vector_line(0.5, 0.0))

    assert session.handle_line("CMD:nod") == []
    assert session.handle_line("CMD:right_grip") == []
    assert session.handle_line("CMD:stand")


def test_configured_session_can_explicitly_allow_cmd_while_moving() -> None:
    config = ts.ServiceConfig(mode=ts.MODE_DRY_RUN, allow_cmd_while_moving=True)
    session = ts.RobotSession.from_config(config)
    session.handle_line(_vector_line(0.5, 0.0))

    assert session.handle_line("CMD:nod")[0][0] == "head"
    assert session.mode == "forward"


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


def test_required_sonar_consecutive_failures_latch_fault() -> None:
    config = ts.ServiceConfig(
        mode=ts.MODE_DRY_RUN,
        sonar_sim="flat:500",
        require_sonar=True,
        dist_interval_s=0.02,
        sonar_failure_limit=3,
        sonar_warn_interval_s=0.0,
    )
    service = ts.TonyPiService(config=config)
    done = threading.Event()

    def broken_source() -> int:
        raise OSError("i2c lost")

    worker = threading.Thread(target=service._distance_loop, args=(broken_source, done))
    worker.start()
    worker.join(1.0)
    assert not worker.is_alive()
    assert service.fault is not None
    assert "required Sonar failed 3 consecutive reads" in service.fault
    assert service._stop.is_set()


def test_optional_sonar_warning_is_rate_limited(capsys: pytest.CaptureFixture[str]) -> None:
    config = ts.ServiceConfig(
        mode=ts.MODE_DRY_RUN,
        dist_interval_s=0.02,
        sonar_warn_interval_s=60.0,
    )
    service = ts.TonyPiService(config=config)
    done = threading.Event()
    calls = {"n": 0}

    def broken_source() -> int:
        calls["n"] += 1
        if calls["n"] >= 4:
            done.set()
        raise OSError("optional sonar fault")

    service._distance_loop(broken_source, done)
    assert capsys.readouterr().out.count("[WARN] Sonar:") == 1


def test_stop_closes_connection_and_listener_sockets() -> None:
    class _Closable:
        def __init__(self) -> None:
            self.shutdown_calls = 0
            self.close_calls = 0

        def shutdown(self, _how: int) -> None:
            self.shutdown_calls += 1

        def close(self) -> None:
            self.close_calls += 1

    service = ts.TonyPiService()
    sockets = [_Closable(), _Closable(), _Closable()]
    service._conn, service._server_socket, service._udp_socket = sockets
    service.stop()
    assert service._stop.is_set()
    assert all(sock.shutdown_calls == 1 for sock in sockets)
    assert all(sock.close_calls == 1 for sock in sockets)


def test_stale_gait_waiting_for_actuator_is_discarded_after_stand() -> None:
    calls: list[str] = []

    class _RecordingAgc:
        def runActionGroup(self, name: str) -> None:
            calls.append(name)

    config = ts.ServiceConfig(mode=ts.MODE_HARDWARE)
    service = ts.TonyPiService(config=config, runtime=ts.RuntimeHardware(agc=_RecordingAgc()))
    service.session.mode = "forward"
    service._actuator_lock.acquire()
    worker = threading.Thread(target=service._run_current_group)
    worker.start()
    time.sleep(0.03)
    with service._lock:
        service.session.mode = "stand"
    calls.append("stand")  # action holding the actuator completed first
    service._actuator_lock.release()
    worker.join(1.0)

    assert not worker.is_alive()
    assert calls == ["stand"]


def test_actuator_execution_is_serialized() -> None:
    config = ts.ServiceConfig(mode=ts.MODE_HARDWARE)

    class _RecordingAgc:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        def runActionGroup(self, _name: str) -> None:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            time.sleep(0.03)
            self.active -= 1

    agc = _RecordingAgc()
    service = ts.TonyPiService(config=config, runtime=ts.RuntimeHardware(agc=agc))
    workers = [
        threading.Thread(target=service._execute_plan, args=([("group", "stand")],))
        for _ in range(3)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(1.0)
    assert agc.max_active == 1
    assert service.fault is None


# ================== 回归：硬件执行器故障锁存并安全退出 ==================
def test_actuator_fault_latches_and_stops_service() -> None:
    class _BrokenBoard:
        def __init__(self) -> None:
            self.calls = 0

        def setPWMServoPulse(self, servo_id: int, pulse: int, use_time: int) -> None:
            self.calls += 1
            raise OSError("i2c servo fault")

    config = ts.ServiceConfig(mode=ts.MODE_HARDWARE)
    board = _BrokenBoard()
    service = ts.TonyPiService(config=config, runtime=ts.RuntimeHardware(board=board))
    assert service._execute_plan(ts.nod_plan(config)) is False
    assert board.calls == 1  # 首次硬件故障即停止，不继续发送余下序列
    assert service.fault is not None
    assert "actuator failure" in service.fault
    assert service._stop.is_set()
    assert service.session.mode == "stand"


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
    worker = threading.Thread(target=service._distance_loop, args=(lambda: 4321, done), daemon=True)
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
