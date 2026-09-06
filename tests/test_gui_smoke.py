from __future__ import annotations

import threading
import time
import tkinter as tk

import pytest

from hcirobot.gui import RobotControlApp
from hcirobot.gui_model import ZERO_COMMAND, SessionState


def test_tk_window_constructs_and_defaults_to_disarmed() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    app = RobotControlApp(root)
    root.update()
    assert app.root.title() == "TonyPi 视觉自治控制台"
    assert not app.model.armed
    assert str(app.arm_button["state"]) == "disabled"
    # Manual controls and tuning apply stay disabled while no session runs.
    assert all(str(button["state"]) == "disabled" for button in app.manual_buttons)
    assert str(app.apply_params_button["state"]) == "disabled"
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
        assert app.events.empty()
        # The recording backend and an off switch stay silent, as before.
        assert app._build_obstacle_policy({"obstacle": {"enabled": False}}, "recording", True) is None
        assert app._build_obstacle_policy({"obstacle": {"enabled": False}}, "tcp", False) is None
        assert app.events.empty()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
