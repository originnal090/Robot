from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

pytest.importorskip("fastapi")
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from hcirobot.app import RuntimeEvent, SessionControl, run_loop, run_manual_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.frame_recorder import FrameRecorder
from hcirobot.gui_model import SessionState
from hcirobot.robot import RecordingRobot
from hcirobot.service import LEASE_SECONDS, ControlConflict, SessionService
from hcirobot.video import SyntheticBallSource
from hcirobot.web import create_app, main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def service(tmp_path):
    runtime = SessionService(ROOT / "config.toml")
    runtime.frame_recorder = FrameRecorder(root=tmp_path / "captures")
    return runtime


def wait_state(ws, lease, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lease:
            ws.send_json({"type": "heartbeat", "lease": lease})
        message = ws.receive_json()
        if message["type"] == "state" and predicate(message["state"]):
            return message["state"]
    raise AssertionError("expected state not received")


def acquire(client, ws):
    client_id = ws.receive_json()["client_id"]
    result = client.post("/api/control/claim", json={"client_id": client_id})
    assert result.status_code == 200, result.text
    return {"client_id": client_id, "lease": result.json()["lease"]}


def start(client, ws, *, control_only=False):
    owner = acquire(client, ws)
    response = client.post(
        "/api/session/start",
        json={
            **owner,
            "source": "synthetic",
            "backend": "recording",
            "obstacle": False,
            "control_only": control_only,
        },
    )
    assert response.status_code == 200, response.text
    state = wait_state(
        ws,
        owner["lease"],
        lambda s: s["session_state"] == "running" and (control_only or s["frame_ready"]),
    )
    return {**owner, "session_id": state["session_id"]}


def test_preview_manual_tuning_capture_and_estop(service):
    with (
        TestClient(create_app(service=service)) as client,
        client.websocket_connect("/ws/events") as ws,
    ):
        request = start(client, ws)
        assert service.model.armed is False
        seq, jpeg = service.jpeg()
        assert seq > 0
        assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape[2] == 3
        ws.send_json(
            {
                "type": "manual",
                "lease": request["lease"],
                "session_id": request["session_id"],
                "seq": 1,
                "velocity": 0.35,
                "steer": 0.0,
                "lateral": 0.0,
            }
        )
        state = wait_state(ws, request["lease"], lambda s: s["output"]["v"] == 0.35)
        assert state["output"]["source"] == "manual"
        wait_state(ws, request["lease"], lambda s: s["output"]["v"] == 0)
        before = service.active_detector.config
        bad = client.patch("/api/tuning", json={**request, "detection": {"lab_min": [999, 2, 3]}})
        assert bad.status_code == 400
        assert service.active_detector.config == before
        changed = client.patch(
            "/api/tuning",
            json={
                **request,
                "detection": {"minimum_contour_area": 65.0},
                "controller": {"align_enter_error": 0.07},
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["detection"]["processing_width"] == before.processing_width
        assert service.active_controller.config.align_enter_error == 0.07
        assert client.post("/api/capture", json={**request, "active": True}).status_code == 200
        captured = wait_state(ws, request["lease"], lambda s: s["capture"]["saved"] > 0)
        assert Path(captured["capture"]["directory"]).is_dir()
        assert client.post("/api/capture", json={**request, "active": False}).status_code == 200
        assert client.post("/api/session/estop", json={}).status_code == 200
        wait_state(ws, request["lease"], lambda s: s["capabilities"]["reset"])
        assert service.model.estop_latched
        assert service.jpeg()[1] is None
        assert client.post("/api/session/reset", json=request).status_code == 200
        assert not service.model.estop_latched


def test_second_observer_cannot_drive_but_can_estop(service):
    with (
        TestClient(create_app(service=service)) as client,
        client.websocket_connect("/ws/events") as a,
    ):
        request = start(client, a, control_only=True)
        with client.websocket_connect("/ws/events") as b:
            client_b = b.receive_json()["client_id"]
            assert (
                client.post("/api/control/claim", json={"client_id": client_b}).status_code == 409
            )
            forged = {**request, "client_id": client_b}
            assert client.post("/api/action/nod", json=forged).status_code == 409
            assert client.post("/api/session/estop", json={}).status_code == 200
        wait_state(a, request["lease"], lambda s: s["estop_latched"])


def test_disconnect_stops_owner_session_and_cannot_rearm(service):
    with TestClient(create_app(service=service)) as client:
        with client.websocket_connect("/ws/events") as ws:
            request = start(client, ws, control_only=True)
        deadline = time.monotonic() + LEASE_SECONDS + 2
        while service._worker_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not service._worker_alive()
        assert service.model.session_state is SessionState.STOPPED
        assert client.post("/api/session/arm", json=request).status_code == 409
        with client.websocket_connect("/ws/events") as observer:
            snapshot = observer.receive_json()["state"]
            assert snapshot["owner"] == "none"
            assert not snapshot["armed"]


def test_silent_owner_expires_without_websocket_disconnect(service):
    with (
        TestClient(create_app(service=service)) as client,
        client.websocket_connect("/ws/events") as ws,
    ):
        request = start(client, ws, control_only=True)
        # No heartbeat and no reads: the independent server watchdog still runs.
        deadline = time.monotonic() + LEASE_SECONDS + 2
        while service._worker_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not service._worker_alive()
        assert service.snapshot()["owner"] == "none"
        assert client.post("/api/action/nod", json=request).status_code == 409


def test_expired_lease_cannot_be_revived_and_old_manual_packet_rejected(service):
    now = [0.0]
    service._clock = lambda: now[0]
    client_id = service.connect()
    lease = service.claim(client_id)
    now[0] = LEASE_SECONDS + 0.01
    with pytest.raises(ControlConflict):
        service.heartbeat(client_id, lease)
    new_lease = service.claim(client_id)
    assert new_lease != lease
    service.model.begin_start(control_only=True)
    service.session_control = SessionControl()
    service.model.session_id = service.session_control.session_id
    service.model.apply_event(RuntimeEvent("started", session_id=service.model.session_id))
    with pytest.raises(ControlConflict):
        service.manual(client_id, lease, service.model.session_id, 1, 0.4, 0, 0)
    sid = service.model.session_id
    service.manual(client_id, new_lease, sid, 2, 0.4, 0, 0)
    with pytest.raises(ControlConflict):
        service.manual(client_id, new_lease, sid, 1, 0.9, 0, 0)
    with pytest.raises(ControlConflict):
        service.manual(client_id, new_lease, sid + 1, 3, 0.9, 0, 0)
    assert service.session_control.current_manual().velocity == 0.4


def test_session_start_has_model_initialization_lease_grace(service):
    now = [0.0]
    service._clock = lambda: now[0]
    service._run_session = lambda _values, _control: None
    client_id = service.connect()
    lease = service.claim(client_id)
    service.start_session(
        client_id,
        lease,
        {
            "source": "synthetic",
            "backend": "recording",
            "host": "127.0.0.1",
            "port": 5075,
            "mirror": None,
            "control_only": False,
            "local_gamepad": False,
            "obstacle": False,
            "approach_mode": "normal_then_slow",
            "edge_model_path": "",
        },
    )
    assert service._expires == 15.0
    now[0] = 1.0
    service.heartbeat(client_id, lease)
    assert service._expires == 16.0
    service._publish_event(RuntimeEvent("started", session_id=service.model.session_id))
    service.heartbeat(client_id, lease)
    assert service._expires == 1.0 + LEASE_SECONDS


def test_detector_backend_rejects_model_from_other_backend(service):
    client_id = service.connect()
    lease = service.claim(client_id)
    common = {
        "source": "synthetic",
        "backend": "recording",
        "host": "127.0.0.1",
        "port": 5075,
        "mirror": None,
        "control_only": False,
        "local_gamepad": False,
        "obstacle": False,
        "approach_mode": "normal_then_slow",
    }
    with pytest.raises(ValueError, match=r"\.om"):
        service.start_session(
            client_id,
            lease,
            common | {"detector_backend": "ascend", "detector_model_path": "svm.json"},
        )
    with pytest.raises(ValueError, match=r"\.json"):
        service.start_session(
            client_id,
            lease,
            common | {"detector_backend": "svm", "detector_model_path": "yolo.om"},
        )


def test_defaults_list_svm_variants_separately_from_ascend(service, tmp_path, monkeypatch):
    profile = (
        tmp_path / "artifacts" / "edge-route-20260915" / "route-balanced-a146-v2" / "profile.json"
    )
    profile.parent.mkdir(parents=True)
    (profile.parent / "model.npz").write_bytes(b"placeholder")
    profile.write_text('{"schema_version": 1, "model": "model.npz"}', encoding="utf-8")
    service.config["npu"] = {"enabled": True, "model": "/models/ball-yolo.om"}
    monkeypatch.chdir(tmp_path)

    defaults = service.defaults()

    assert defaults["ascend_model_path"] == "/models/ball-yolo.om"
    assert defaults["svm_model_path"].endswith("route-balanced-a146-v2/profile.json")
    assert defaults["detector_model_path"] == "/models/ball-yolo.om"
    assert defaults["svm_model_path"] != defaults["ascend_model_path"]
    assert defaults["svm_presets"][0]["available"] is True


def test_invalid_axes_do_not_reach_session(service):
    with (
        TestClient(create_app(service=service)) as client,
        client.websocket_connect("/ws/events") as ws,
    ):
        request = start(client, ws, control_only=True)
        for value in (2, "0.5", True, None):
            ws.send_json(
                {
                    "type": "manual",
                    "lease": request["lease"],
                    "session_id": request["session_id"],
                    "seq": 1,
                    "velocity": value,
                    "steer": 0.0,
                    "lateral": 0.0,
                }
            )
            while ws.receive_json()["type"] != "error":
                pass
            assert service.session_control.current_manual() is None


def test_no_manual_or_actions_after_arm_and_control_only_cannot_arm(service):
    with (
        TestClient(create_app(service=service)) as client,
        client.websocket_connect("/ws/events") as ws,
    ):
        request = start(client, ws, control_only=True)
        assert client.post("/api/session/arm", json=request).status_code == 409
        assert client.post("/api/action/nod", json=request).status_code == 200
        assert client.post("/api/action/unknown", json=request).status_code == 400


def test_pending_arm_blocks_manual_before_ack(service):
    client = service.connect()
    lease = service.claim(client)
    service.session_control = SessionControl()
    service.model.begin_start()
    service.model.session_id = service.session_control.session_id
    service.model.apply_event(RuntimeEvent("started", session_id=service.model.session_id))
    service.model.video_status = "画面正常"
    service.command(client, lease, service.model.session_id, "arm")
    with pytest.raises(ControlConflict):
        service.manual(client, lease, service.model.session_id, 1, 0.5, 0, 0)
    with pytest.raises(ControlConflict):
        service.action(client, lease, service.model.session_id, "nod")
    service.command(client, lease, service.model.session_id, "disarm")
    assert not service.session_control.consume_arm()
    assert service.session_control.consume_disarm()


def test_authentication_cookie_origin_and_static_assets(service):
    with TestClient(create_app(service=service, token="test-access")) as client:
        assert client.get("/").status_code == 200
        assert "app.js" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/state").status_code == 401
        assert client.post("/api/session/estop", json={}).status_code == 401
        with (
            pytest.raises(WebSocketDisconnect) as denied,
            client.websocket_connect("/ws/events"),
        ):
            pass
        assert denied.value.code == 1008
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
        result = client.post("/api/login", json={"token": "test-access"})
        assert "HttpOnly" in result.headers["set-cookie"]
        assert client.get("/api/state").status_code == 200
        assert (
            client.post(
                "/api/session/estop", json={}, headers={"Origin": "http://untrusted.example"}
            ).status_code
            == 403
        )
        assert not service.model.estop_latched
        with (
            pytest.raises(WebSocketDisconnect) as cross_origin,
            client.websocket_connect("/ws/events", headers={"Origin": "http://untrusted.example"}),
        ):
            pass
        assert cross_origin.value.code == 1008
        with client.websocket_connect("/ws/events", headers={"Origin": "http://testserver"}) as ws:
            assert ws.receive_json()["type"] == "state"


def test_missing_profile_fails_before_hardware_and_recovers(service, monkeypatch):
    opened = []
    monkeypatch.setattr(
        "hcirobot.session_runtime.build_source", lambda *a, **kw: opened.append(True)
    )
    with (
        TestClient(create_app(service=service)) as client,
        client.websocket_connect("/ws/events") as ws,
    ):
        owner = acquire(client, ws)
        result = client.post(
            "/api/session/start",
            json={
                **owner,
                "source": "synthetic",
                "backend": "recording",
                "detector_backend": "svm",
                "detector_model_path": "missing-profile.json",
            },
        )
        assert result.status_code == 200
        failed = wait_state(ws, owner["lease"], lambda s: s["capabilities"]["reset"])
        assert not opened
        assert failed["fault"]


def test_cleanup_blocks_new_session_and_old_events_cannot_mutate(service):
    client = service.connect()
    lease = service.claim(client)
    gate = threading.Event()
    service.session_thread = threading.Thread(target=lambda: gate.wait(2))
    service.session_thread.start()
    try:
        service.model.fail("closing")
        assert not service.snapshot()["capabilities"]["start"]
        assert not service.snapshot()["capabilities"]["reset"]
        with pytest.raises(ControlConflict):
            service.command(client, lease, 0, "reset")
        service.model.session_id = 9
        service._publish_event(RuntimeEvent("error", "old error", session_id=8))
        assert service.model.fault == "closing"
    finally:
        gate.set()
        service.session_thread.join(2)


@pytest.mark.parametrize("manual_only", [True, False])
def test_estop_takes_priority_over_queued_actions(manual_only):
    control = SessionControl()
    robot = RecordingRobot()
    control.request_action("nod")
    control.request_estop()
    if manual_only:
        result = run_manual_loop(robot, session=control)
    else:
        result = run_loop(
            SyntheticBallSource(),
            RedBallDetector(DetectorConfig()),
            VisualApproachController(ControllerConfig()),
            robot,
            armed=False,
            session=control,
        )
    assert result.termination == "estop"
    assert robot.actions == []


def test_lan_cli_requires_token(monkeypatch):
    monkeypatch.delenv("HCIROBOT_WEB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--host", "0.0.0.0"])
    assert exc.value.code == 2


def test_web_import_is_headless():
    # A fresh interpreter is required because GUI regression tests import Tk.
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", "import hcirobot.web, sys; assert 'tkinter' not in sys.modules"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_local_gamepad_owns_session_after_browser_disconnect(service):
    class LocalMonitor:
        def start(self):
            pass

        def stop(self):
            pass

        def status(self):
            return True, "Test local pad"

    service.gamepad_monitor = LocalMonitor()
    with TestClient(create_app(service=service)) as client:
        with client.websocket_connect("/ws/events") as ws:
            owner = acquire(client, ws)
            result = client.post(
                "/api/session/start",
                json={
                    **owner,
                    "source": "synthetic",
                    "backend": "recording",
                    "control_only": True,
                    "local_gamepad": True,
                },
            )
            assert result.status_code == 200
            wait_state(ws, None, lambda s: s["session_state"] == "running")
        assert service._worker_alive()
        assert service.snapshot()["owner"] == "local"
        with client.websocket_connect("/ws/events") as observer:
            client_id = observer.receive_json()["client_id"]
            assert (
                client.post("/api/control/claim", json={"client_id": client_id}).status_code == 409
            )
            assert client.post("/api/session/stop", json={}).status_code == 200
            wait_state(
                observer, None, lambda s: s["session_state"] == "stopped" and s["owner"] == "none"
            )
            assert (
                client.post("/api/control/claim", json={"client_id": client_id}).status_code == 200
            )


def test_delayed_jpeg_from_old_session_is_discarded(service, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_encode = cv2.imencode

    def slow_encode(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return real_encode(*args, **kwargs)

    monkeypatch.setattr(cv2, "imencode", slow_encode)
    service.open()
    service.model.begin_start()
    service.model.apply_event(RuntimeEvent("started", session_id=1))
    service._publish_event(RuntimeEvent("frame", frame=np.zeros((8, 8, 3), np.uint8), session_id=1))
    try:
        assert entered.wait(2)
        service.stop()
        release.set()
    finally:
        release.set()
        service.close()
    assert service.jpeg()[1] is None
