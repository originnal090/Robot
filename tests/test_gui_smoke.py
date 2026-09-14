from __future__ import annotations

import contextlib
import queue
import threading
import time
import tkinter as tk
from pathlib import Path

import numpy as np
import pytest

from hcirobot.edge_detector import FEATURE_VERSION, EdgeBallDetector, FeatureExtractor
from hcirobot.gamepad import GamepadReading
from hcirobot.gui import RobotControlApp
from hcirobot.gui_model import ZERO_COMMAND, SessionState
from hcirobot.mock_server import MockRobotServer
from hcirobot.video import SyntheticBallSource, SyntheticConfig


class StubGamepadMonitor:
    """Keep GUI smoke tests isolated from the separately tested native monitor."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def status(self) -> tuple[bool, str]:
        return False, "未检测到手柄"

    def latest(self) -> GamepadReading:
        return GamepadReading(False, "")

    def consume_toggle(self) -> bool:
        return False

    def consume_actions(self) -> tuple[str, ...]:
        return ()


@pytest.fixture(autouse=True)
def isolate_gui_from_slow_realtime_devices(monkeypatch):
    """Use fast fakes; native gamepad and 10 FPS pacing have dedicated tests."""
    import hcirobot.gui as gui_module

    real_build_source = gui_module.build_source

    def fast_build_source(value: str, video: dict, realtime: bool):
        if value == "synthetic":
            return SyntheticBallSource(
                SyntheticConfig(
                    width=int(video["width"]),
                    height=int(video["height"]),
                    fps=100.0,
                    realtime=True,
                )
            )
        return real_build_source(value, video, realtime)

    monkeypatch.setattr(gui_module, "GamepadMonitor", StubGamepadMonitor)
    monkeypatch.setattr(gui_module, "build_source", fast_build_source)


def drain_non_gamepad_events(app: RobotControlApp) -> list:
    """Pop every queued event, tolerating gamepad monitor notes (kind='gamepad')."""
    others = []
    while True:
        try:
            event = app.events.get_nowait()
        except queue.Empty:
            return others
        if event.kind != "gamepad":
            others.append(event)


def test_tk_window_constructs_and_defaults_to_disarmed() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = RobotControlApp(root)
    try:
        root.update()
        assert app.root.title() == "TonyPi 视觉自治控制台"
        assert not app.model.armed
        assert str(app.arm_button["state"]) == "disabled"
        # Manual controls and tuning apply stay disabled while no session runs.
        assert all(str(button["state"]) == "disabled" for button in app.manual_buttons)
        assert str(app.apply_params_button["state"]) == "disabled"
        assert not app.edge_model_var.get()
        assert app.edge_model_checkbutton.instate(["!disabled"])
        assert app.edge_model_entry.instate(["disabled"])
        app.edge_model_var.set(True)
        app._refresh_detector_selector()
        assert app.edge_model_entry.instate(["!disabled"])
        assert app.edge_model_browse_button.instate(["!disabled"])
        # The top-bar gamepad cell must reflect the probed state (name, 未检测到
        # or 未装 pygame) instead of the initial placeholder.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and str(app.status_values["gamepad"]["text"]) in (
            "--",
            "未连接",
        ):
            root.update()
            time.sleep(0.02)
        assert str(app.status_values["gamepad"]["text"]) not in ("--", "未连接")
    finally:
        app.gamepad_monitor.stop()
        root.destroy()


def test_session_runs_manual_actions_and_stops_cleanly() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    baseline_threads = threading.active_count()
    app = RobotControlApp(root)
    root.update()
    app.source_var.set("synthetic")
    app.backend_var.set("recording")
    assert app.model.can_start
    app._start_session()

    def pump_until(condition, timeout: float, message: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            root.update()
            if condition():
                return
            time.sleep(0.01)
        raise AssertionError(message)

    try:
        pump_until(lambda: app.model.frame_count > 0, 10.0, "synthetic session produced no frames")
        assert app.model.session_state is SessionState.RUNNING
        assert app.model.can_manual
        assert str(app.manual_buttons[0]["state"]) == "normal"
        app._send_action("nod")
        app._send_manual(0.35, 0.0, 0.35)
        # Invalid input is rejected into the log instead of crashing.
        app._send_action("not a valid action!")
        assert any("被拒绝" in line for line in app.model.logs)
        app._stop()
        assert app.model.session_state in (SessionState.STOPPING, SessionState.STOPPED)
        pump_until(
            lambda: app.model.session_state is SessionState.STOPPED,
            10.0,
            "session did not confirm the stop request",
        )
        assert app.model.command == ZERO_COMMAND
        assert app.model.control_state == "IDLE"
        assert app.latest_frame is None
    finally:
        app._closing = True
        if app.session_thread is not None and app.session_thread.is_alive():
            app.session_thread.join(timeout=5.0)
        app.gamepad_monitor.stop()
    assert app.session_thread is not None
    assert not app.session_thread.is_alive()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and threading.active_count() > baseline_threads:
        time.sleep(0.05)
    assert threading.active_count() == baseline_threads, "session threads leaked"
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_control_only_session_never_opens_video_and_allows_manual(monkeypatch) -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = RobotControlApp(root)
    root.update()

    def fail_if_video_opens(*_args, **_kwargs):
        raise AssertionError("control-only session opened a video source")

    monkeypatch.setattr("hcirobot.gui.build_source", fail_if_video_opens)
    app.control_only_var.set(True)
    app.backend_var.set("recording")
    app.source_var.set("this-source-must-not-be-opened")
    app._refresh_control_mode()
    app._start_session()

    deadline = time.monotonic() + 3.0
    try:
        while time.monotonic() < deadline and app.model.session_state is not SessionState.RUNNING:
            root.update()
            time.sleep(0.01)
        assert app.model.session_state is SessionState.RUNNING
        assert app.model.video_status == "已禁用"
        assert app.model.can_manual
        assert not app.model.can_arm
        assert app.active_detector is None
        assert app.active_controller is None
        assert app.edge_model_checkbutton.instate(["disabled"])
        assert app.obstacle_checkbutton.instate(["disabled"])
        assert str(app.capture_button["state"]) == "disabled"

        app._send_manual(0.35, 0.0, 0.2)
        while time.monotonic() < deadline and app.model.output_source != "manual":
            root.update()
            time.sleep(0.01)
        assert app.model.output_source == "manual"
        app._stop()
        while time.monotonic() < deadline and app.model.session_state is not SessionState.STOPPED:
            root.update()
            time.sleep(0.01)
        assert app.model.session_state is SessionState.STOPPED
        assert app.model.video_status == "已禁用"
    finally:
        app._closing = True
        if app.session_thread is not None and app.session_thread.is_alive():
            app.session_control.request_stop()
            app.session_thread.join(timeout=3.0)
        app.gamepad_monitor.stop()
        with contextlib.suppress(tk.TclError):
            root.destroy()


def test_gui_tcp_session_connects_with_command_mirror() -> None:
    """Regression: a configured mirror used to hide connect() from the GUI."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    servers = [MockRobotServer(port=0), MockRobotServer(port=0)]
    server_threads: list[threading.Thread] = []
    for server in servers:
        ready = threading.Event()
        thread = threading.Thread(target=server.serve_forever, args=(ready,), daemon=True)
        thread.start()
        assert ready.wait(2.0)
        server_threads.append(thread)

    primary, mirror = servers
    app = RobotControlApp(root)
    app.source_var.set("synthetic")
    app.backend_var.set("tcp")
    app.host_var.set("127.0.0.1")
    app.port_var.set(str(primary.bound_port))
    app.mirror_var.set(f"127.0.0.1:{mirror.bound_port}")
    app.obstacle_var.set(False)
    app._start_session()

    deadline = time.monotonic() + 5.0
    try:
        while time.monotonic() < deadline:
            root.update()
            if (
                app.model.session_state is SessionState.RUNNING
                and primary.commands
                and mirror.commands
            ):
                break
            time.sleep(0.01)
        assert app.model.session_state is SessionState.RUNNING
        assert primary.commands
        assert mirror.commands
        assert any(
            f"机器人 TCP 已连接：127.0.0.1:{primary.bound_port}" in line for line in app.model.logs
        )
        app._stop()
        while time.monotonic() < deadline and app.model.session_state is not SessionState.STOPPED:
            root.update()
            time.sleep(0.01)
        assert app.model.session_state is SessionState.STOPPED
    finally:
        app._closing = True
        if app.session_control is not None:
            app.session_control.request_stop()
        if app.session_thread is not None:
            app.session_thread.join(timeout=3.0)
        app.gamepad_monitor.stop()
        for server in servers:
            server.stop()
        for thread in server_threads:
            thread.join(timeout=2.0)
        root.destroy()


@pytest.mark.parametrize('use_profile', [False, True])
def test_gui_selects_edge_model_and_locks_choice_during_session(tmp_path: Path,
                                                               use_profile: bool) -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = RobotControlApp(root)
    size = FeatureExtractor().extract(np.zeros((32, 32, 3), np.uint8), (0, 0, 32, 32)).size
    model_path = tmp_path / "gui-edge-model.npz"
    np.savez(
        model_path,
        weights=np.zeros(size, np.float32),
        bias=np.float32(1),
        threshold=np.float32(0),
        feature_version=np.int32(FEATURE_VERSION),
    )
    app.edge_model_var.set(True)
    if use_profile:
        import hashlib
        import json
        from dataclasses import asdict

        from hcirobot.detector import DetectorConfig
        from hcirobot.hybrid_detector import HybridBallDetector

        profile_path = tmp_path / 'gui-version.json'
        profile_path.write_text(json.dumps({
            'schema_version': 1, 'id': 'gui-hybrid-test', 'model': model_path.name,
            'model_sha256': hashlib.sha256(model_path.read_bytes()).hexdigest(),
            'strategy': 'hybrid', 'options': {'fast_backend': True},
            'detector_config': asdict(DetectorConfig(confirmation_frames=4)),
        }), encoding='utf-8')
        model_path = profile_path
    app.edge_model_path_var.set(str(model_path))
    app.source_var.set("synthetic")
    app.backend_var.set("recording")
    app._start_session()
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline and app.model.frame_count == 0:
            root.update()
            time.sleep(0.01)
        assert isinstance(app.active_detector, EdgeBallDetector)
        if use_profile:
            assert isinstance(app.active_detector, HybridBallDetector)
            assert app.active_detector.config.confirmation_frames == 4
            assert app.active_detector.fast_backend
            assert app.tuning_vars['confirmation_frames'].get() == '4'
        assert app.edge_model_checkbutton.instate(["disabled"])
        assert app.edge_model_entry.instate(["disabled"])
        assert any("识别器：端侧模型" in line for line in app.model.logs)
        app._stop()
        while time.monotonic() < deadline and app.model.session_state is not SessionState.STOPPED:
            root.update()
            time.sleep(0.01)
        assert app.edge_model_checkbutton.instate(["!disabled"])
        assert app.edge_model_entry.instate(["!disabled"])
    finally:
        app._closing = True
        if app.session_thread is not None and app.session_thread.is_alive():
            app.session_control.request_stop()
            app.session_thread.join(timeout=5)
        app.gamepad_monitor.stop()
        root.destroy()


def test_gui_rejects_empty_edge_model_path_without_starting() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = RobotControlApp(root)
    try:
        app.edge_model_var.set(True)
        app.edge_model_path_var.set("")
        app._start_session()
        assert app.session_thread is None
        assert app.model.session_state is SessionState.STOPPED
        assert any("请先选择模型文件" in line for line in app.model.logs)
    finally:
        app.gamepad_monitor.stop()
        root.destroy()


def test_gui_rejects_invalid_tcp_endpoint_before_starting() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = RobotControlApp(root)
    try:
        app.backend_var.set("tcp")
        app.host_var.set("192.0.2.10")
        app.port_var.set("5075 extra")

        app._start_session()

        assert app.session_thread is None
        assert app.model.session_state is SessionState.STOPPED
        assert any("端口必须是整数" in line for line in app.model.logs)
    finally:
        app.gamepad_monitor.stop()
        root.destroy()


def test_gui_rejects_missing_edge_model_before_opening_video(tmp_path: Path, monkeypatch) -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = RobotControlApp(root)
    video_opened = False

    def unexpected_video_open(*_args, **_kwargs):
        nonlocal video_opened
        video_opened = True
        raise AssertionError("video must not open before model validation")

    monkeypatch.setattr("hcirobot.gui.build_source", unexpected_video_open)
    app.edge_model_var.set(True)
    app.edge_model_path_var.set(str(tmp_path / "missing.npz"))
    app.source_var.set("synthetic")
    app.backend_var.set("recording")
    app._start_session()
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline and app.model.session_state is not SessionState.FAILED:
            root.update()
            time.sleep(0.01)
        assert app.model.session_state is SessionState.FAILED
        assert not video_opened
        assert "edge model does not exist" in app.model.fault
    finally:
        app._closing = True
        if app.session_thread is not None and app.session_thread.is_alive():
            app.session_thread.join(timeout=5)
        app.gamepad_monitor.stop()
        root.destroy()


def test_obstacle_panel_tracks_telemetry_and_recording_skips_policy() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    baseline_threads = threading.active_count()
    app = RobotControlApp(root)
    root.update()
    # The obstacle panel exists and the switch is live while stopped and disarmed.
    assert app.obstacle_panel.winfo_exists()
    assert not app.obstacle_checkbutton.instate(["disabled"])
    app.source_var.set("synthetic")
    app.backend_var.set("recording")
    switch_on = bool(app.obstacle_var.get())
    assert app.model.can_start
    app._start_session()

    def pump_until(condition, timeout: float, message: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            root.update()
            if condition():
                return
            time.sleep(0.01)
        raise AssertionError(message)

    try:
        pump_until(lambda: app.model.frame_count > 0, 10.0, "synthetic session produced no frames")
        assert app.model.session_state is SessionState.RUNNING
        # The start snapshot mirrors the switch, which freezes while the session runs.
        assert app.model.obstacle_enabled == switch_on
        assert app.obstacle_checkbutton.instate(["disabled"])
        # A recording backend never builds an obstacle policy, so no obstacle
        # telemetry may appear while frames stream.
        root.update()
        assert app.model.obstacle_state == "无数据"
        assert app.obstacle_values["state"]["text"] == app.model.obstacle_state
        assert app.obstacle_values["distance"]["text"] == app.model.obstacle_distance
        assert app.obstacle_values["avoids"]["text"] == str(app.model.avoid_count)
        app._stop()
        assert app.model.session_state in (SessionState.STOPPING, SessionState.STOPPED)
        pump_until(
            lambda: app.model.session_state is SessionState.STOPPED,
            10.0,
            "session did not confirm the stop request",
        )
        # Session end reset the panel display and released the switch.
        assert app.model.obstacle_state == "无数据"
        assert app.obstacle_values["state"]["text"] == "无数据"
        assert app.model.obstacle_distance == "--"
        assert not app.model.latched_blocked
        assert app.obstacle_checkbutton.instate(["!disabled"])
    finally:
        app._closing = True
        if app.session_thread is not None and app.session_thread.is_alive():
            app.session_thread.join(timeout=5.0)
        app.gamepad_monitor.stop()
    assert app.session_thread is not None
    assert not app.session_thread.is_alive()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and threading.active_count() > baseline_threads:
        time.sleep(0.05)
    assert threading.active_count() == baseline_threads, "session threads leaked"
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_obstacle_policy_builder_logs_config_disabled_and_defaults_to_enabled() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = RobotControlApp(root)
    root.update()
    try:
        # An explicit enabled=false in config wins over the switch and is logged,
        # never silently skipped.
        assert app._build_obstacle_policy({"obstacle": {"enabled": False}}, "tcp", True) is None
        event = app.events.get_nowait()
        assert event.kind == "state"
        assert "避障已被配置文件禁用" in event.message
        # A missing section/key defaults to enabled, matching the CLI.
        assert app._build_obstacle_policy({}, "tcp", True) is not None
        assert not drain_non_gamepad_events(app)
        # The recording backend and an off switch stay silent, as before.
        assert (
            app._build_obstacle_policy({"obstacle": {"enabled": False}}, "recording", True) is None
        )
        assert app._build_obstacle_policy({"obstacle": {"enabled": False}}, "tcp", False) is None
        assert not drain_non_gamepad_events(app)
    finally:
        app.gamepad_monitor.stop()
        try:
            root.destroy()
        except tk.TclError:
            pass
