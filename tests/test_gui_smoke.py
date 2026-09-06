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
