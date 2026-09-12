from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_tool(name: str) -> ModuleType:
    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_tool("check_unity_simulator")
evaluator = _load_tool("eval_unity_trials")


JPEG_2X3 = bytes.fromhex("FFD8FFC00011080003000203011100021100031100FFD9")


class _SimulatorServer:
    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.lines: list[str] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.thread.join(timeout=2)
        self.listener.close()

    def _serve(self) -> None:
        conn, _ = self.listener.accept()
        with conn:
            conn.settimeout(0.1)
            stop = threading.Event()

            def telemetry() -> None:
                while not stop.wait(0.05):
                    try:
                        conn.sendall(b"DIST:432\n")
                    except OSError:
                        return

            sender = threading.Thread(target=telemetry, daemon=True)
            sender.start()
            buffer = b""
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    data = conn.recv(4096)
                except TimeoutError:
                    continue
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode()
                    self.lines.append(line)
                    if line.startswith("{"):
                        payload = json.loads(line)
                        if payload["steer"] > 0:
                            time.sleep(0.08)
                            conn.sendall(b"WATCHDOG:STAND\n")
            stop.set()
            sender.join(timeout=1)


class _MjpegHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG_2X3 + b"\r\n"
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _start_http_server() -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MjpegHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_checker_parses_jpeg_and_distance() -> None:
    stream = type("Stream", (), {"read": lambda self, _size: JPEG_2X3})()
    assert checker.extract_complete_jpeg(stream, max_bytes=1024) == JPEG_2X3
    assert checker.jpeg_dimensions(JPEG_2X3) == (2, 3)
    assert checker.parse_distance_line("DIST: 123 ") == 123.0
    assert checker.parse_distance_line("DIST:nan") is None
    assert checker.parse_distance_line("OTHER:1") is None


def test_checker_runs_against_fake_simulator_and_mjpeg() -> None:
    tcp = _SimulatorServer()
    tcp.start()
    http, http_thread = _start_http_server()
    args = argparse.Namespace(
        host="127.0.0.1",
        port=tcp.port,
        mjpeg_url=f"http://127.0.0.1:{http.server_port}/?action=stream",
        timeout=1.0,
        distance_timeout=0.4,
        watchdog_seconds=0.05,
        watchdog_margin=0.05,
        max_mjpeg_bytes=4096,
    )
    try:
        code, results = checker.run(args)
    finally:
        http.shutdown()
        http.server_close()
        http_thread.join(timeout=2)
        tcp.close()

    assert code == checker.ExitCode.OK
    assert all(result.ok for result in results)
    assert any(line.startswith("CMD:stand") for line in tcp.lines)
    payloads = [json.loads(line) for line in tcp.lines if line.startswith("{")]
    assert any(payload["steer"] == -0.35 for payload in payloads)
    assert any(payload["v"] == 0 and payload["steer"] == 0 for payload in payloads)


def test_checker_returns_failure_for_unreachable_endpoints() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    args = argparse.Namespace(
        host="127.0.0.1",
        port=port,
        mjpeg_url=f"http://127.0.0.1:{port}/?action=stream",
        timeout=0.1,
        distance_timeout=0.1,
        watchdog_seconds=0.05,
        watchdog_margin=0.05,
        max_mjpeg_bytes=4096,
    )
    code, results = checker.run(args)
    assert code == checker.ExitCode.CHECK_FAILED
    assert any(not result.ok for result in results)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_evaluator_summarizes_truth_metrics_and_csv(tmp_path: Path) -> None:
    log = tmp_path / "trials.jsonl"
    _write_jsonl(
        log,
        [
            {
                "trial_id": "clear-left",
                "scenario": "clear",
                "elapsed_s": 0.0,
                "robot_position": [0, 0, 0],
                "target_position": [0, 0, 2],
                "robot_yaw_deg": 0,
                "clearance_m": 1.2,
                "avoid_count": 0,
                "state": "SEARCHING",
            },
            {
                "trial_id": "clear-left",
                "scenario": "clear",
                "elapsed_s": 5.0,
                "robot_position": [0, 0, 1.8],
                "target_position": [0, 0, 2],
                "robot_yaw_deg": 0,
                "clearance_m": 0.8,
                "avoid_count": 1,
                "state": "ARRIVED",
                "success": True,
            },
            {
                "trial_id": "off-center",
                "scenario": "arrival-boundary",
                "elapsed_s": 0.0,
                "robot_position": [0, 0, 0],
                "target_position": [1, 0, 1],
                "robot_yaw_deg": 0,
                "clearance_m": 0.5,
                "state": "SEARCHING",
            },
            {
                "trial_id": "off-center",
                "scenario": "arrival-boundary",
                "elapsed_s": 2.0,
                "robot_position": [0.8, 0, 0.8],
                "target_position": [1, 0, 1],
                "robot_yaw_deg": 0,
                "clearance_m": 0.2,
                "collision": True,
                "state": "ARRIVED",
            },
        ],
    )

    report = evaluator.evaluate_inputs(log, thresholds=evaluator.Thresholds())
    aggregate = report["aggregate"]
    assert aggregate["trial_count"] == 2
    assert aggregate["success_rate"] == 0.5
    assert aggregate["collision_rate"] == 0.5
    assert aggregate["false_arrival_rate"] == 0.5
    assert aggregate["total_avoid_count"] == 1
    assert aggregate["minimum_clearance_m"] == 0.2

    first = report["trials"][0]
    assert first["final_distance_m"] == pytest.approx(0.2)
    assert first["final_heading_error_deg"] == pytest.approx(0)
    assert first["duration_s"] == pytest.approx(5)
    assert first["path_length_m"] == pytest.approx(1.8)
    assert first["straight_line_efficiency"] == 1.0

    csv_path = tmp_path / "report.csv"
    evaluator.write_csv(csv_path, report["trials"])
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["trial_id"] for row in rows] == ["clear-left", "off-center"]


def test_evaluator_accepts_directory_and_explicit_summary_fields(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_jsonl(
        logs / "one.jsonl",
        [
            {
                "trial_id": "one",
                "scenario": "obstacle",
                "duration_s": 8.0,
                "path_length_m": 2.5,
                "true_distance_mm": 250,
                "heading_error_deg": 10,
                "minimum_clearance_mm": 180,
                "collision_count": 0,
                "avoid_count": 2,
                "controller_state": "ARRIVED",
            }
        ],
    )
    report = evaluator.evaluate_inputs(logs, thresholds=evaluator.Thresholds())
    trial = report["trials"][0]
    assert trial["final_distance_m"] == 0.25
    assert trial["minimum_clearance_m"] == 0.18
    assert trial["avoid_count"] == 2
    assert trial["success"] is True


def test_evaluator_rejects_bad_json_and_missing_truth(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{bad json}\n", encoding="utf-8")
    with pytest.raises(evaluator.EvaluationError, match="bad.jsonl:1"):
        evaluator.evaluate_inputs(bad, thresholds=evaluator.Thresholds())

    missing = tmp_path / "missing.jsonl"
    _write_jsonl(missing, [{"trial_id": "x", "elapsed_s": 0, "state": "ARRIVED"}])
    with pytest.raises(evaluator.EvaluationError, match="final true target distance"):
        evaluator.evaluate_inputs(missing, thresholds=evaluator.Thresholds())


def test_evaluator_allow_invalid_reports_skips(tmp_path: Path) -> None:
    log = tmp_path / "mixed.jsonl"
    log.write_text(
        "not json\n"
        + json.dumps(
            {
                "trial_id": "valid",
                "duration_s": 1,
                "path_length_m": 0.5,
                "true_distance_m": 0.2,
                "heading_error_deg": 0,
                "state": "ARRIVED",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = evaluator.evaluate_inputs(
        log,
        thresholds=evaluator.Thresholds(),
        allow_invalid=True,
    )
    assert report["aggregate"]["trial_count"] == 1
    assert report["input"]["invalid_records"]
