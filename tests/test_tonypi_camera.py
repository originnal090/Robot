from __future__ import annotations

import ast
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "robot_side"))

import tonypi_camera as camera


def test_single_file_syntax_is_python_38_compatible() -> None:
    source = Path(camera.__file__).read_text(encoding="utf-8")
    ast.parse(source, filename=str(camera.__file__), feature_version=(3, 8))


def test_load_config_parses_device_and_rejects_bad_limits(monkeypatch) -> None:
    monkeypatch.setattr(camera, "_vendor_flip", lambda _path: -1)
    config = camera.load_config({"TONYPI_CAMERA_DEVICE": "/dev/video2"})
    assert config.device == "/dev/video2"
    assert config.flip == -1
    assert camera.load_config({"TONYPI_CAMERA_DEVICE": "0"}).device == 0
    with pytest.raises(camera.ConfigError, match="JPEG_QUALITY"):
        camera.load_config({"TONYPI_CAMERA_JPEG_QUALITY": "101"})
    with pytest.raises(camera.ConfigError, match="FLIP"):
        camera.load_config({"TONYPI_CAMERA_FLIP": "2"})


class FakeJPEG:
    def tobytes(self) -> bytes:
        return b"jpeg"


class FakeCapture:
    def __init__(self, outcomes, *, opened: bool = True) -> None:
        self.outcomes = iter(outcomes)
        self.opened = opened
        self.released = False

    def isOpened(self) -> bool:
        return self.opened

    def set(self, *_args) -> bool:
        return True

    def read(self):
        try:
            return next(self.outcomes)
        except StopIteration:
            threading.Event().wait(0.01)
            return True, object()

    def release(self) -> None:
        self.released = True


def test_worker_releases_failed_handle_before_reopen(monkeypatch) -> None:
    first = FakeCapture([(False, None)])
    second = FakeCapture([(True, object())])
    captures = [first, second]
    opened = []

    def open_capture(_device):
        if opened:
            assert opened[-1].released
        item = captures[len(opened)]
        opened.append(item)
        return item

    monkeypatch.setattr(camera.cv2, "VideoCapture", open_capture)
    monkeypatch.setattr(camera.cv2, "imencode", lambda *_args: (True, FakeJPEG()))
    state = camera.FrameState()
    stop = threading.Event()
    config = camera.CameraConfig(
        read_failure_limit=1,
        max_reopen_attempts=2,
        initial_backoff_s=0.001,
        max_backoff_s=0.001,
    )
    worker = camera.CameraWorker(config, state, stop)
    worker.start()
    try:
        _, sequence = state.wait_after(0, 1.0)
        assert sequence > 0
    finally:
        stop.set()
        worker.join(1.0)
    assert not worker.is_alive()
    assert first.released
    assert second.released


def test_worker_exits_after_finite_open_failures(monkeypatch) -> None:
    captures = []

    def open_capture(_device):
        item = FakeCapture([], opened=False)
        captures.append(item)
        return item

    monkeypatch.setattr(camera.cv2, "VideoCapture", open_capture)
    state = camera.FrameState()
    worker = camera.CameraWorker(
        camera.CameraConfig(
            max_reopen_attempts=2,
            initial_backoff_s=0.001,
            max_backoff_s=0.001,
        ),
        state,
        threading.Event(),
    )
    worker.start()
    worker.join(1.0)
    assert not worker.is_alive()
    assert len(captures) == 3
    assert all(item.released for item in captures)
    assert state.snapshot()[-1] == "camera reopen limit reached (2)"


def test_reconnect_clears_stale_jpeg() -> None:
    state = camera.FrameState()
    state.publish(b"old", 1.0)
    state.set_unavailable("reconnecting")
    jpeg, sequence, last_frame_at, status, fault = state.snapshot()
    assert jpeg is None
    assert sequence == 1
    assert last_frame_at == 1.0
    assert status == "reconnecting"
    assert fault is None


def test_http_health_snapshot_and_vendor_stream_url() -> None:
    state = camera.FrameState()
    stop = threading.Event()
    server = camera.ThreadedHTTPServer(("127.0.0.1", 0), camera.make_handler(state, stop))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as unavailable:
            urllib.request.urlopen(base + "/healthz", timeout=1.0)
        assert unavailable.value.code == 503

        state.publish(b"first-jpeg", 1.0)
        with urllib.request.urlopen(base + "/healthz", timeout=1.0) as response:
            health = json.loads(response.read())
        assert health["healthy"] is True
        assert health["sequence"] == 1

        with urllib.request.urlopen(base + "/?action=snapshot", timeout=1.0) as response:
            assert response.read() == b"first-jpeg"

        with urllib.request.urlopen(
            base + "/?action=stream?dummy=param.mjpg", timeout=1.0
        ) as response:
            state.publish(b"second-jpeg", 2.0)
            assert response.readline() == b"--frame\r\n"
            assert response.readline() == b"Content-Type: image/jpeg\r\n"
            length = int(response.readline().partition(b":")[2])
            assert response.readline() == b"\r\n"
            assert response.read(length) == b"second-jpeg"
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(1.0)
