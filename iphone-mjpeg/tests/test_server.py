from __future__ import annotations

import importlib.util
import io
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PYTHON_DIR = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PYTHON_DIR))
SPEC = importlib.util.spec_from_file_location("iphone_mjpeg_server", PYTHON_DIR / "server.py")
assert SPEC and SPEC.loader
server_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = server_module
SPEC.loader.exec_module(server_module)

FrameStore = server_module.FrameStore
MJPEGServer = server_module.MJPEGServer
NativeFrameReceiver = server_module.NativeFrameReceiver

JPEG_ONE = b"\xff\xd8one\xff\xd9"
JPEG_TWO = b"\xff\xd8two\xff\xd9"


def running_server():
    frames = FrameStore()
    server = MJPEGServer(("127.0.0.1", 0), frames)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return frames, server, thread


def request(server, path):
    host, port = server.server_address
    return urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=2)


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_health_and_snapshot_lifecycle():
    frames, server, thread = running_server()
    try:
        with request(server, "/health") as response:
            assert b'"camera":false' in response.read()
        try:
            request(server, "/snapshot.jpg")
        except urllib.error.HTTPError as error:
            assert error.code == 503
        else:
            raise AssertionError("snapshot should be unavailable before the first frame")

        frames.set_native_connected(True)
        frames.publish(JPEG_ONE, 1280, 720, 123)
        with request(server, "/snapshot.jpg") as response:
            assert response.headers["Content-Type"] == "image/jpeg"
            assert response.read() == JPEG_ONE
        with request(server, "/health") as response:
            body = response.read()
            assert b'"camera":true' in body
            assert b'"width":1280' in body
            assert b'"height":720' in body
    finally:
        stop_server(server, thread)


def test_frame_store_keeps_only_latest_frame():
    frames = FrameStore()
    first = frames.publish(JPEG_ONE, 640, 480, 1)
    second = frames.publish(JPEG_TWO, 640, 480, 2)
    assert second.sequence == first.sequence + 1
    assert frames.latest() == second
    assert frames.wait_for_new(first.sequence, 0.01) == second


def test_native_receiver_accepts_framed_jpeg(tmp_path):
    frames = FrameStore()
    socket_path = tmp_path / "frames.sock"
    receiver = NativeFrameReceiver(socket_path, frames)
    packet = struct.pack("!4sIIIQ", b"MJP1", len(JPEG_ONE), 1280, 720, 987654321)
    receiver._consume(io.BytesIO(packet + JPEG_ONE))
    received = frames.wait_for_new(0, 1.0)
    assert received is not None
    assert received.jpeg == JPEG_ONE
    assert received.width == 1280
    assert received.height == 720
    assert received.capture_timestamp_ns == 987654321


def test_mjpeg_multipart_headers_and_disconnect_do_not_stop_server():
    frames, server, thread = running_server()
    try:
        frames.set_native_connected(True)
        frames.publish(JPEG_ONE, 640, 480, 100)
        host, port = server.server_address
        client = socket.create_connection((host, port), timeout=2)
        client.sendall(b"GET /video HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n")
        data = b""
        deadline = time.monotonic() + 2
        while JPEG_ONE not in data and time.monotonic() < deadline:
            data += client.recv(4096)
        assert b"multipart/x-mixed-replace; boundary=frame" in data
        assert b"--frame\r\nContent-Type: image/jpeg\r\n" in data
        assert f"Content-Length: {len(JPEG_ONE)}\r\n".encode() in data
        assert JPEG_ONE in data
        client.close()

        frames.publish(JPEG_TWO, 640, 480, 200)
        with request(server, "/snapshot.jpg") as response:
            assert response.read() == JPEG_TWO
        assert thread.is_alive()
    finally:
        stop_server(server, thread)
