from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from hcirobot.model import RobotCommand
from hcirobot.robot import FanoutRobot, MirrorTcpRobot, RecordingRobot


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("mirror did not deliver events")


@contextmanager
def mirror_receiver():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(2)
    lines = []

    def receive():
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(2)
            buffer = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    lines.append(line.decode())

    worker = threading.Thread(target=receive)
    worker.start()
    mirror = MirrorTcpRobot("127.0.0.1", listener.getsockname()[1], minimum_send_interval=0)
    try:
        yield mirror, lines
    finally:
        mirror.close()
        worker.join(3)
        listener.close()
        assert not worker.is_alive()
        assert mirror._mirror_worker is None or not mirror._mirror_worker.is_alive()


@pytest.mark.parametrize("status", ["done", "cancelled", "error"])
def test_fanout_sends_start_immediately_then_primary_terminal_once(status):
    class Primary(RecordingRobot):
        outcome = "accepted"

        def step_status(self, _):
            return self.outcome

    with mirror_receiver() as (mirror, lines):
        primary = Primary()
        robot = FanoutRobot(primary, (mirror,))
        ident = robot.start_step(RobotCommand(steer=-0.35))
        wait_for(lambda: len(lines) == 1)
        assert lines[0].startswith("MIRROR_STEP:")
        assert json.loads(lines[0][12:]) == {
            "id": ident, "phase": "start", "steer": -0.35, "lateral": 0,
        }
        assert robot.step_status(ident) == "accepted"
        robot.heartbeat_step(ident)
        primary.outcome = status
        assert robot.step_status(ident) == status
        assert robot.step_status(ident) == status
        robot.send(RobotCommand.stop())
        wait_for(lambda: len(lines) == 4)
        assert [json.loads(line[12:])["phase"] for line in lines[:3]] == ["start", "heartbeat", status]
        assert json.loads(lines[3])["steer"] == 0
        assert len(primary.steps) == 1


def test_mirror_delay_does_not_block_primary_and_expired_start_is_not_replayed(monkeypatch):
    mirror = MirrorTcpRobot("localhost")
    entered, release = threading.Event(), threading.Event()
    sent = []

    def connect():
        entered.set()
        release.wait(2)

    monkeypatch.setattr(mirror._client, "connect", connect)
    monkeypatch.setattr(mirror._client, "_send_bytes", sent.append)
    try:
        primary = RecordingRobot()
        robot = FanoutRobot(primary, (mirror,))
        start = time.perf_counter()
        ident = robot.start_step(RobotCommand(steer=0.35))
        assert entered.wait(0.2)
        robot.heartbeat_step(ident)
        assert robot.step_status(ident) == "done"
        assert time.perf_counter() - start < 0.2
        # Simulate expiry without spending a real reconnect timeout.
        with mirror._outbox_ready:
            mirror._outbox = type(mirror._outbox)((data, 0) for data, _ in mirror._outbox)
        time.sleep(0.60)  # Allow Windows clock tick granularity at the deadline.
        release.set()
        wait_for(lambda: not mirror._outbox)
        mirror.close()
        assert sent == []
    finally:
        release.set()
        mirror.close()


def test_manual_stop_stays_after_queued_start(monkeypatch):
    mirror = MirrorTcpRobot("localhost")
    entered, release = threading.Event(), threading.Event()
    sent = []

    def connect():
        entered.set()
        release.wait(1)

    monkeypatch.setattr(mirror._client, "connect", connect)
    monkeypatch.setattr(mirror._client, "_send_bytes", sent.append)
    try:
        mirror.mirror_step({"id": "one", "phase": "start", "steer": 0.35, "lateral": 0})
        assert entered.wait(0.2)
        mirror.send(RobotCommand.stop())
        release.set()
        wait_for(lambda: len(sent) == 2)
        assert sent[0].startswith(b"MIRROR_STEP:")
        assert json.loads(sent[1])["steer"] == 0
    finally:
        release.set()
        mirror.close()
