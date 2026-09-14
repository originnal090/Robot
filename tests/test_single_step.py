from __future__ import annotations

import json
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hcirobot.model import RobotCommand
from hcirobot.robot import FanoutRobot, TcpRobotClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "robot_side"))
import tonypi_server as ts


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


def make_action(path, durations=(40, 40, 40)):
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE ActionGroup (id INTEGER PRIMARY KEY, duration, servo1)")
        database.executemany("INSERT INTO ActionGroup VALUES (?, ?, ?)",
                             [(i, duration, 500 + i) for i, duration in enumerate(durations)])


class Board:
    def __init__(self):
        self.frames = []
        self.first_frame = threading.Event()

    def setBusServoPulse(self, servo, pulse, duration):
        self.frames.append((servo, pulse, duration, time.perf_counter()))
        self.first_frame.set()


@contextmanager
def connected_service(tmp_path, *, durations=(40, 40, 40), missing=False):
    board = Board()
    if not missing:
        make_action(tmp_path / "turn_right_small_step.d6a", durations)
    service = ts.TonyPiService(
        config=ts.ServiceConfig(host="127.0.0.1", port=0, mode=ts.MODE_HARDWARE,
                                step_interval_s=0.01),
        runtime=ts.RuntimeHardware(board=board, agc=SimpleNamespace(runActionGroup=lambda _: None)),
    )
    service.action_directory = tmp_path
    accept = threading.Thread(target=service.serve_forever)
    motion = threading.Thread(target=service._motion_loop)
    accept.start()
    motion.start()
    client = None
    try:
        def listening():
            sock = service._server_socket
            try:
                return sock is not None and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
            except OSError:
                return False

        wait_for(listening)
        port = service._server_socket.getsockname()[1]
        client = TcpRobotClient("127.0.0.1", port, minimum_send_interval=0)
        client.connect()
        wait_for(lambda: client.supports_steps)
        yield service, client, board
    finally:
        if client is not None:
            client.close()
        service.stop()
        accept.join(2)
        motion.join(2)
        assert not accept.is_alive() and not motion.is_alive()


def test_real_tcp_executes_once_and_done_follows_last_frame(tmp_path):
    with connected_service(tmp_path) as (_, client, board):
        ident = client.start_step(RobotCommand(steer=0.3))
        assert client.step_status(ident) == "accepted"
        wait_for(lambda: client.step_status(ident) == "done")
        assert len(board.frames) == 3
        assert time.perf_counter() - board.frames[-1][3] >= 0.04
        # Retries before/after completion cannot replay the same ID.
        for _ in range(3):
            client._send_bytes(("STEP:" + json.dumps({"id": ident, "steer": 0.3}) + "\n").encode())
        time.sleep(0.08)
        assert len(board.frames) == 3
        assert client.step_status(ident) == "done"


def test_full_control_loop_with_real_service_completes_one_step(tmp_path):
    from hcirobot.app import SessionControl, run_loop
    from hcirobot.controller import ControllerConfig, VisualApproachController
    from hcirobot.model import Detection
    from hcirobot.video import SyntheticBallSource, SyntheticConfig

    with connected_service(tmp_path) as (_, client, board):
        session, events = SessionControl(), []

        def sink(event):
            events.append(event)
            if event.kind == "step" and event.message.endswith(": done"):
                session.request_stop()

        result = run_loop(
            SyntheticBallSource(SyntheticConfig(fps=50, realtime=True)),
            SimpleNamespace(process=lambda _: Detection(True, True, 384, 240, 30, 640, 480)),
            VisualApproachController(ControllerConfig(sense_seconds=0.04)),
            client, armed=True, session=session, event_sink=sink,
        )
        assert result.termination == "stop_requested"
        assert len(board.frames) == 3
        assert sum(e.kind == "step" and e.message.endswith(": requested") for e in events) == 1
        assert any(e.output_source == "step_wait" for e in events)


def test_duplicate_while_running_and_busy_request_never_start_extra_action(tmp_path):
    with connected_service(tmp_path, durations=(100, 100)) as (_, client, board):
        ident = client.start_step(RobotCommand(steer=0.3))
        assert board.first_frame.wait(1)
        client._send_bytes((f'STEP:{{"id":"{ident}","steer":0.3}}\n'
                            'STEP:{"id":"other","steer":0.3}\n').encode())
        wait_for(lambda: client.step_status(ident) == "done")
        client._send_bytes(b'STEP:{"id":"other","steer":0.3}\n')
        time.sleep(0.05)
        assert len(board.frames) == 2


def test_cancel_before_dispatch_and_retry_never_reaches_actuator():
    replies = []
    conn = SimpleNamespace(sendall=lambda data: replies.append(data))
    service = ts.TonyPiService(config=ts.ServiceConfig(mode=ts.MODE_DRY_RUN))
    request = 'STEP:{"id":"pending","steer":0.3}'
    assert service._handle_step_line(conn, request)
    with service._lock:
        service._cancel_step_locked()
    calls = []
    service._play_step = calls.append
    assert service._run_step()
    assert not calls
    service._handle_step_line(conn, request)
    assert service._step is None
    assert b'"cancelled"' in replies[-1]


@pytest.mark.parametrize("payload", [
    '{"id":"bad","steer":true}', '{"id":"bad","steer":NaN}',
    '{"id":"bad","steer":0.3,"lateral":0.3}',
    '{"id":"bad","steer":0.1}', '{"id":"bad","v":0.6,"steer":0.3}',
])
def test_invalid_step_cannot_execute_or_fall_through_to_legacy(payload):
    replies = []
    conn = SimpleNamespace(sendall=lambda data: replies.append(data))
    service = ts.TonyPiService(config=ts.ServiceConfig(mode=ts.MODE_DRY_RUN))
    assert service._handle_step_line(conn, "STEP:" + payload)
    assert service._step is None and service.session.mode == "stand"
    assert b'"error"' in replies[-1]


def test_full_dedupe_table_refuses_new_ids_without_eviction():
    replies = []
    conn = SimpleNamespace(sendall=lambda data: replies.append(data))
    service = ts.TonyPiService(config=ts.ServiceConfig(mode=ts.MODE_DRY_RUN))
    service._step_history = {str(i): {"id": str(i), "status": "done", "detail": ""}
                             for i in range(1024)}
    service._handle_step_line(conn, 'STEP:{"id":"new","steer":0.3}')
    assert service._step is None
    assert b'session_step_limit' in replies[-1]
    service._handle_step_line(conn, 'STEP:{"id":"0","steer":0.3}')
    assert service._step is None and b'"done"' in replies[-1]


def test_stop_cancels_after_current_frame_without_false_done(tmp_path):
    with connected_service(tmp_path, durations=(100, 100, 100)) as (_, client, board):
        ident = client.start_step(RobotCommand(steer=0.3))
        assert board.first_frame.wait(1)
        client.send(RobotCommand.stop())
        wait_for(lambda: client.step_status(ident) == "cancelled")
        assert len(board.frames) == 1
        assert time.perf_counter() - board.frames[0][3] >= 0.10
        client._send_bytes((f'STEP:{{"id":"{ident}","steer":0.3}}\n').encode())
        time.sleep(0.05)
        assert len(board.frames) == 1


def test_disconnect_cancels_and_does_not_replay_on_new_connection(tmp_path):
    with connected_service(tmp_path, durations=(100, 100, 100)) as (service, client, board):
        client.start_step(RobotCommand(steer=0.3))
        assert board.first_frame.wait(1)
        client.cancel()
        wait_for(lambda: service._step is None)
        assert len(board.frames) == 1
        assert service.session.mode == "stand"


@pytest.mark.parametrize("missing,durations", [(True, (40,)), (False, (40, -1))])
def test_missing_or_invalid_action_is_error_before_any_servo_output(tmp_path, missing, durations):
    with connected_service(tmp_path, missing=missing, durations=durations) as (_, client, board):
        ident = client.start_step(RobotCommand(steer=0.3))
        wait_for(lambda: client.step_status(ident) == "error")
        assert board.frames == []


def test_hardware_exception_is_not_reported_as_success(tmp_path):
    with connected_service(tmp_path) as (_, client, board):
        def fail(*_):
            raise OSError("servo transport failed")
        board.setBusServoPulse = fail
        ident = client.start_step(RobotCommand(steer=0.3))
        wait_for(lambda: client.step_status(ident) == "error")


def test_heartbeat_loss_cancels_before_another_frame(tmp_path):
    with connected_service(tmp_path, durations=(100, 100)) as (service, client, board):
        ident = client.start_step(RobotCommand(steer=0.3))
        assert board.first_frame.wait(1)
        with service._lock:
            service._step["heartbeat"] = time.monotonic() - 2
        wait_for(lambda: client.step_status(ident) == "cancelled")
        assert len(board.frames) == 1


def test_heartbeat_keeps_long_step_alive(tmp_path):
    with connected_service(tmp_path, durations=(900, 900)) as (_, client, board):
        ident = client.start_step(RobotCommand(steer=0.3))
        deadline = time.monotonic() + 3
        while client.step_status(ident) == "accepted" and time.monotonic() < deadline:
            client.heartbeat_step(ident)
            time.sleep(0.2)
        assert client.step_status(ident) == "done"
        assert len(board.frames) == 2


def test_status_parser_ignores_unknown_ids_and_cannot_rewind_terminal_status():
    client = TcpRobotClient("localhost")
    client._steps = {"expected": "accepted"}
    for message in [b'STEP_STATUS:garbage', b'STEP_STATUS:[]',
                    b'STEP_STATUS:{"id":"unknown","status":"done"}']:
        client._handle_telemetry_line(message)
    assert client.step_status("expected") == "accepted"
    client._handle_telemetry_line(b'STEP_STATUS:{"id":"expected","status":"cancelled"}')
    client._handle_telemetry_line(b'STEP_STATUS:{"id":"expected","status":"done"}')
    client._handle_telemetry_line(b'STEP_STATUS:{"id":"expected","status":"accepted"}')
    assert client.step_status("expected") == "cancelled"


def test_unsupported_server_cannot_receive_single_step_as_legacy_motion():
    client = TcpRobotClient("localhost")
    with pytest.raises(ValueError, match="STEP_V1"):
        client.start_step(RobotCommand(steer=0.3))


def test_fanout_uses_primary_ack_without_legacy_motion_on_mirror(tmp_path):
    with connected_service(tmp_path) as (_, client, board):
        mirror_calls = []
        mirror = SimpleNamespace(send=lambda command: mirror_calls.append(command))
        robot = FanoutRobot(client, (mirror,))
        assert robot.supports_steps
        ident = robot.start_step(RobotCommand(steer=0.3))
        robot.heartbeat_step(ident)
        wait_for(lambda: robot.step_status(ident) == "done")
        assert len(board.frames) == 3 and not mirror_calls
