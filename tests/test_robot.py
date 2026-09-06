from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from hcirobot.mock_server import MockRobotServer
from hcirobot.model import RobotCommand
from hcirobot.robot import (
    RecordingRobot,
    TcpRobotClient,
    encode_legacy_command,
    encode_raw_payload,
)


def test_legacy_payload_matches_course_server_contract() -> None:
    payload = json.loads(encode_legacy_command(RobotCommand(0.25, -0.35)))
    assert set(payload) == {"v", "steer", "grab", "t"}
    assert payload["v"] == 0.25
    assert payload["steer"] == -0.35
    assert payload["grab"] is False
    assert isinstance(payload["t"], str)


def test_tcp_client_sends_commands_and_final_stop() -> None:
    server = MockRobotServer(port=0)
    ready = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(ready,), daemon=True)
    thread.start()
    assert ready.wait(2)

    client = TcpRobotClient("127.0.0.1", server.bound_port, minimum_send_interval=0)
    client.connect()
    client.send(RobotCommand(0.25, 0.0))
    client.close()
    thread.join(0.2)
    server.stop()
    thread.join(2)

    assert len(server.commands) >= 2
    assert server.commands[0]["v"] == 0.25
    assert server.commands[-1]["v"] == 0.0
    assert server.commands[-1]["steer"] == 0.0


def test_mock_server_handles_fragmented_and_coalesced_lines() -> None:
    server = MockRobotServer(port=0)
    ready = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(ready,), daemon=True)
    thread.start()
    assert ready.wait(2)
    first = encode_legacy_command(RobotCommand(0.2, 0.0))
    second = encode_legacy_command(RobotCommand(0.0, 0.2))
    with socket.create_connection(("127.0.0.1", server.bound_port)) as sock:
        sock.sendall(first[:5])
        sock.sendall(first[5:] + second)
    deadline = time.monotonic() + 1.0
    while len(server.commands) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    server.stop()
    thread.join(2)
    assert [(item["v"], item["steer"]) for item in server.commands] == [(0.2, 0.0), (0.0, 0.2)]


def _start_server() -> tuple[MockRobotServer, threading.Thread]:
    server = MockRobotServer(port=0)
    ready = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(ready,), daemon=True)
    thread.start()
    assert ready.wait(2)
    return server, thread


def test_recording_robot_records_actions_and_raw_payloads() -> None:
    robot = RecordingRobot()
    robot.send_action("nod")
    robot.send_raw({"v": -0.5, "steer": 0.25, "grab": False, "t": "t0"})
    assert robot.actions == ["nod"]
    assert robot.raw_payloads == [{"v": -0.5, "steer": 0.25, "grab": False, "t": "t0"}]
    with pytest.raises(ValueError):
        robot.send_action("bad name!")
    with pytest.raises(ValueError):
        robot.send_raw({"v": 2.0, "steer": 0.0, "grab": False, "t": "t0"})
    assert robot.actions == ["nod"]
    assert len(robot.raw_payloads) == 1


def test_tcp_client_send_action_round_trip() -> None:
    server, thread = _start_server()
    client = TcpRobotClient("127.0.0.1", server.bound_port, minimum_send_interval=0)
    client.connect()
    client.send_action("nod")
    client.send_action("shake")
    with pytest.raises(ValueError):
        client.send_action("drop table")  # 空格不在 [A-Za-z0-9_] 内
    with pytest.raises(ValueError):
        client.send_action("x" * 33)  # 超过 32 字符
    client.close()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        cmds = [item for item in server.commands if "cmd" in item]
        if len(cmds) >= 2:
            break
        time.sleep(0.01)
    server.stop()
    thread.join(2)

    cmds = [item for item in server.commands if "cmd" in item]
    assert cmds == [{"cmd": "nod"}, {"cmd": "shake"}]


def test_tcp_client_send_raw_round_trip_and_rejects_invalid() -> None:
    server, thread = _start_server()
    client = TcpRobotClient("127.0.0.1", server.bound_port, minimum_send_interval=0)
    client.connect()
    client.send_raw({"v": -0.4, "steer": 0.3, "grab": False, "t": "t0"})
    for bad in (
        {"steer": 0.0, "grab": False, "t": "t0"},  # 缺 v
        {"v": 1.5, "steer": 0.0, "grab": False, "t": "t0"},  # v 超范围
        {"v": 0.0, "steer": -1.1, "grab": False, "t": "t0"},  # steer 超范围
        {"v": float("nan"), "steer": 0.0, "grab": False, "t": "t0"},
        {"v": float("inf"), "steer": 0.0, "grab": False, "t": "t0"},
        {"v": "fast", "steer": 0.0, "grab": False, "t": "t0"},
        {"v": True, "steer": 0.0, "grab": False, "t": "t0"},  # bool 不是速度
        {"v": 0.1, "grab": False, "t": "t0"},  # 缺 steer
    ):
        with pytest.raises(ValueError):
            client.send_raw(bad)
    client.close()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        vectors = [item for item in server.commands if "v" in item]
        if len(vectors) >= 2:  # 有效帧 + close() 的最终站立帧都已被服务端处理
            break
        time.sleep(0.01)
    server.stop()
    thread.join(2)

    assert vectors[0]["v"] == -0.4
    assert vectors[0]["steer"] == 0.3
    assert vectors[-1]["v"] == 0.0  # close() 补发的最终站立帧


def test_encode_raw_payload_matches_jsonl_contract() -> None:
    line = encode_raw_payload({"v": -1.0, "steer": 1.0, "grab": False, "t": "t0"})
    payload = json.loads(line)
    assert payload == {"v": -1.0, "steer": 1.0, "grab": False, "t": "t0"}
    with pytest.raises(ValueError):
        encode_raw_payload({"v": 2.0, "steer": 0.0, "grab": False, "t": "t0"})


def test_cancel_interrupts_throttled_sleep() -> None:
    server, thread = _start_server()
    client = TcpRobotClient("127.0.0.1", server.bound_port, minimum_send_interval=30.0)
    client.connect()
    client.send(RobotCommand(0.1, 0.0))
    started = time.monotonic()

    def cancel_later() -> None:
        time.sleep(0.3)
        client.cancel()

    threading.Thread(target=cancel_later, daemon=True).start()
    with pytest.raises(ConnectionError):
        client.send(RobotCommand(0.2, 0.0))
    assert time.monotonic() - started < 5  # 没有等满 30s 的节流间隔
    client.close()
    server.stop()
    thread.join(2)


def test_cancel_unblocks_send_blocked_on_full_socket_buffers() -> None:
    # 接受连接但从不读取：TCP 缓冲区填满后 sendall 将无限阻塞
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = TcpRobotClient(
        "127.0.0.1",
        listener.getsockname()[1],
        minimum_send_interval=0,
        connect_timeout=30,
    )
    client.connect()
    conn, _ = listener.accept()

    progress: list[int] = []
    outcome: dict[str, Exception] = {}

    def sender() -> None:
        try:
            for _ in range(100000):
                client.send(RobotCommand(0.1, 0.0))
                progress.append(1)
        except ConnectionError as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=sender, daemon=True)
    worker.start()

    # 等待发送停滞（缓冲区已满、sendall 阻塞中）
    last_count, stalled = -1, 0
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and stalled < 2:
        time.sleep(0.25)
        count = len(progress)
        stalled = stalled + 1 if count == last_count and count > 0 else 0
        last_count = count
    assert last_count > 0
    assert not outcome  # 尚未出错：确实卡在 sendall 里

    client.cancel()
    worker.join(3)
    assert not worker.is_alive()  # cancel 立即解除了阻塞
    assert isinstance(outcome.get("error"), ConnectionError)
    with pytest.raises(ConnectionError):
        client.send(RobotCommand(0.0, 0.0))

    client.cancel()  # 幂等
    client.close()  # 与 cancel 兼容
    conn.close()
    listener.close()


def test_cancel_and_close_are_safe_without_connection() -> None:
    client = TcpRobotClient("127.0.0.1", 5075)
    client.cancel()  # 未连接也必须无异常
    client.close()
    client.close()
    with pytest.raises(ConnectionError):
        client.send(RobotCommand(0.0, 0.0))


class _LineServer:
    """最小服务端：接受一个连接，让测试直接向客户端读取线程喂遥测行。"""

    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(5)
        self.port = self.listener.getsockname()[1]
        self.conn: socket.socket | None = None

    def accept(self) -> socket.socket:
        conn, _ = self.listener.accept()
        self.conn = conn
        return conn

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.listener.close()


def _wait_for_distance(client: TcpRobotClient, timeout: float = 2.0) -> tuple[float, float] | None:
    deadline = time.monotonic() + timeout
    got = None
    while time.monotonic() < deadline:
        got = client.latest_distance()
        if got is not None:
            break
        time.sleep(0.01)
    return got


def test_recording_robot_latest_distance_is_none() -> None:
    assert RecordingRobot().latest_distance() is None


def test_tcp_client_reader_parses_dist_lines_with_fresh_timestamp() -> None:
    server = _LineServer()
    client = TcpRobotClient("127.0.0.1", server.port, minimum_send_interval=0)
    client.connect()
    conn = server.accept()
    started = time.monotonic()
    try:
        conn.sendall(b"DIST:320\n")
        got = _wait_for_distance(client)
        assert got is not None
        mm, ts = got
        assert mm == 320.0
        assert started <= ts <= time.monotonic()  # 收到时刻是新鲜的 monotonic 时间戳
        # 跨包分片的行也能正确拼装
        conn.sendall(b"DIST:2")
        conn.sendall(b"5\n")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            latest = client.latest_distance()
            if latest is not None and latest[0] == 25.0:
                break
            time.sleep(0.01)
        assert latest is not None and latest[0] == 25.0
    finally:
        client.close()
        server.close()


def test_tcp_client_reader_ignores_invalid_lines() -> None:
    server = _LineServer()
    client = TcpRobotClient("127.0.0.1", server.port, minimum_send_interval=0)
    client.connect()
    conn = server.accept()
    try:
        # 非法行（垃圾行 / 非整数 / 空 payload / 前后缀）全部忽略，不污染后续 DIST
        conn.sendall(b"HELLO\nDIST:abc\nDIST:\nxDIST:99x\nDIST: 77 \n")
        got = _wait_for_distance(client)
        assert got is not None
        assert got[0] == 77.0
    finally:
        client.close()
        server.close()


def test_tcp_client_reader_dispatches_color_and_distance_callbacks() -> None:
    events: list[tuple[str, str]] = []
    server = _LineServer()
    client = TcpRobotClient(
        "127.0.0.1",
        server.port,
        minimum_send_interval=0,
        on_message=lambda kind, payload: events.append((kind, payload)),
    )
    client.connect()
    conn = server.accept()
    try:
        conn.sendall(b"COLOR_SIGNAL:RED\nDIST:10\nCOLOR_SIGNAL:GREEN\n")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(events) < 3:
            time.sleep(0.01)
        assert events == [("color", "RED"), ("distance", "10"), ("color", "GREEN")]
    finally:
        client.close()
        server.close()


def test_close_stops_reader_thread_and_latest_distance_stays_readable() -> None:
    server = _LineServer()
    client = TcpRobotClient("127.0.0.1", server.port, minimum_send_interval=0)
    client.connect()
    conn = server.accept()
    try:
        conn.sendall(b"DIST:64\n")
        got = _wait_for_distance(client)
        assert got is not None and got[0] == 64.0
        client.close()
        reader = client._reader
        assert reader is not None
        reader.join(2.0)
        assert not reader.is_alive()  # close 后读取线程退出
        still = client.latest_distance()  # 关闭后最后读数仍可读
        assert still is not None and still[0] == 64.0
    finally:
        server.close()


def test_cancel_stops_reader_thread() -> None:
    server = _LineServer()
    client = TcpRobotClient("127.0.0.1", server.port, minimum_send_interval=0)
    client.connect()
    server.accept()  # 完成握手即可；取消测试不需要向连接写数据
    try:
        client.cancel()
        reader = client._reader
        assert reader is not None
        reader.join(2.0)
        assert not reader.is_alive()  # cancel 后读取线程退出
    finally:
        client.close()
        server.close()


def test_mock_server_distance_source_streaming_end_to_end() -> None:
    values = iter([10, None, 20])

    def source() -> int | None:
        return next(values, 20)

    server = MockRobotServer(port=0, watchdog=5.0, distance_source=source)
    ready = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(ready,), daemon=True)
    thread.start()
    assert ready.wait(2)
    client = TcpRobotClient("127.0.0.1", server.bound_port, minimum_send_interval=0)
    client.connect()
    try:
        # 第 1 拍 10 → 第 2 拍 None（跳过）→ 第 3 拍 20：流未中断且 latest_distance 生效
        deadline = time.monotonic() + 3.0
        got = None
        while time.monotonic() < deadline:
            got = client.latest_distance()
            if got is not None and got[0] == 20.0:
                break
            time.sleep(0.02)
        assert got is not None and got[0] == 20.0
        assert 0.0 < got[1] <= time.monotonic()
    finally:
        client.close()
        server.stop()
        thread.join(2)


def test_mock_server_none_distance_source_never_sends_but_control_still_works() -> None:
    server = MockRobotServer(port=0, watchdog=5.0, distance_source=lambda: None)
    ready = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(ready,), daemon=True)
    thread.start()
    assert ready.wait(2)
    client = TcpRobotClient("127.0.0.1", server.bound_port, minimum_send_interval=0)
    client.connect()
    try:
        client.send(RobotCommand(0.1, 0.0))
        time.sleep(0.7)  # 覆盖至少两拍距离遥测
        assert client.latest_distance() is None  # None 源不产生 DIST
    finally:
        client.close()
        server.stop()
        thread.join(2)
    assert any("v" in item for item in server.commands)  # 控制通道不受影响
