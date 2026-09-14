from __future__ import annotations

import sys
import time

import pytest

from hcirobot.app import RuntimeEvent, SessionControl, _safe_print, run_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.gamepad import (
    TOGGLE_BUTTON,
    GamepadMonitor,
    GamepadReading,
    GamepadTeleop,
    apply_deadzone,
    map_to_command,
    sanitize_axis,
)
from hcirobot.gui_model import GuiModel, SessionState
from hcirobot.model import RobotCommand
from hcirobot.robot import RecordingRobot
from hcirobot.video import SyntheticBallSource, SyntheticConfig


def wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class FakeBackend:
    """Scripted backend: queued probe results and readings, consumed in order."""

    def __init__(self) -> None:
        self.last_error = ""
        self._probe_results: list[str | None] = []
        self._readings: list[GamepadReading | Exception] = []
        self.closed = 0

    def queue_probe(self, *results: str | None) -> None:
        self._probe_results.extend(results)

    def queue_readings(self, *readings: GamepadReading | Exception) -> None:
        self._readings.extend(readings)

    def probe(self) -> str | None:
        if self._probe_results:
            result = self._probe_results.pop(0)
        else:
            result = None
        self.last_error = "" if result else "未检测到手柄"
        return result

    def read(self) -> GamepadReading:
        if not self._readings:
            return GamepadReading(True, "Fake Pad")
        item = self._readings.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed += 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def build_monitor(backend: FakeBackend, **kwargs) -> GamepadMonitor:
    kwargs.setdefault("poll_seconds", 0.005)
    kwargs.setdefault("probe_seconds", 0.01)
    kwargs.setdefault("maximum_probe_seconds", 0.05)
    monitor = GamepadMonitor(backend, **kwargs)
    monitor.start()
    return monitor


def press(backend: FakeBackend, buttons: frozenset[int] = frozenset()) -> None:
    backend.queue_readings(GamepadReading(True, "Fake Pad", 0.0, 0.0, buttons))


# ---------- pure mapping ----------


def test_sanitize_axis_rejects_garbage_and_clamps() -> None:
    assert sanitize_axis(float("nan")) == 0.0
    assert sanitize_axis(float("inf")) == 0.0
    assert sanitize_axis("not-a-number") == 0.0
    assert sanitize_axis(None) == 0.0
    assert sanitize_axis(5.0) == 1.0
    assert sanitize_axis(-5.0) == -1.0
    assert sanitize_axis(0.25) == pytest.approx(0.25)


def test_apply_deadzone_scales_outside_band() -> None:
    assert apply_deadzone(0.1, 0.20) == 0.0
    assert apply_deadzone(-0.2, 0.20) == 0.0
    assert apply_deadzone(0.6, 0.20) == pytest.approx(0.5)
    assert apply_deadzone(-0.6, 0.20) == pytest.approx(-0.5)
    assert apply_deadzone(1.0, 0.20) == pytest.approx(1.0)
    assert apply_deadzone(0.6, 0.0) == pytest.approx(0.6)


def test_map_to_command_follows_protocol_signs() -> None:
    # Axes arrive in protocol convention (+forward, +right) — the SDL "up is -1"
    # inversion happens in PygameGamepadBackend.read, not here.
    velocity, steer = map_to_command(1.0, 0.5, 0.20)
    assert velocity == pytest.approx(1.0)
    assert steer == pytest.approx(0.375)
    assert map_to_command(float("nan"), 0.0, 0.2) == (0.0, 0.0)


# ---------- monitor ----------


def test_monitor_publishes_reading_and_latches_toggle_edges() -> None:
    backend = FakeBackend()
    backend.queue_probe("Fake Pad")
    monitor = build_monitor(backend)
    try:
        assert wait_until(lambda: monitor.latest().connected)
        backend.queue_readings(
            GamepadReading(True, "Fake Pad", 0.5, -0.25, frozenset()),
            GamepadReading(True, "Fake Pad", 0.5, -0.25, frozenset({TOGGLE_BUTTON})),
            GamepadReading(True, "Fake Pad", 0.5, -0.25, frozenset({TOGGLE_BUTTON})),
        )
        assert wait_until(lambda: monitor.latest().drive_axis == 0.5)
        assert wait_until(monitor.consume_toggle)  # rising edge latched once...
        assert not monitor.consume_toggle()  # ...and holding the button adds nothing
        assert monitor.latest().steer_axis == -0.25

        press(backend)  # release
        press(backend, frozenset({TOGGLE_BUTTON}))  # press again
        assert wait_until(monitor.consume_toggle)
        assert not monitor.consume_toggle()
    finally:
        monitor.stop()
    assert not monitor.latest().connected


def test_monitor_survives_read_failure_and_reconnects() -> None:
    backend = FakeBackend()
    backend.queue_probe("Fake Pad")
    monitor = build_monitor(backend)
    try:
        assert wait_until(lambda: monitor.latest().connected)
        backend.queue_readings(RuntimeError("device unplugged"))
        assert wait_until(lambda: not monitor.latest().connected)
        assert backend.closed >= 1
        backend.queue_probe("Fake Pad")
        assert wait_until(lambda: monitor.latest().connected)
    finally:
        monitor.stop()


def test_monitor_reports_absent_device_without_dying() -> None:
    monitor = build_monitor(FakeBackend())
    try:
        assert wait_until(lambda: monitor.status() == (False, "未检测到手柄"))
    finally:
        monitor.stop()


def test_monitor_stale_reading_degrades_to_disconnected() -> None:
    clock = FakeClock()
    backend = FakeBackend()
    backend.queue_probe("Fake Pad")
    monitor = build_monitor(backend, clock=clock, stale_after_seconds=0.5)
    try:
        assert wait_until(lambda: monitor.latest().connected)
        backend.queue_readings(GamepadReading(True, "Fake Pad", 0.8, 0.0, frozenset()))
        assert wait_until(lambda: monitor.latest().drive_axis == 0.8)
        clock.now += 10.0
        reading = monitor.latest()
        assert not reading.connected
        assert reading.drive_axis == 0.0  # a stale stick must never stay latched
    finally:
        monitor.stop()


def test_monitor_probe_exception_is_contained() -> None:
    class ExplodingBackend(FakeBackend):
        def probe(self) -> str | None:
            raise RuntimeError("probe boom")

    monitor = build_monitor(ExplodingBackend())
    try:
        assert wait_until(lambda: not monitor.status()[0])
        assert monitor.latest() == GamepadReading()
    finally:
        monitor.stop()


# ---------- teleop switcher ----------


def teleop_setup(**kwargs):
    backend = FakeBackend()
    backend.queue_probe("Fake Pad")
    monitor = build_monitor(backend)
    teleop = GamepadTeleop(monitor, **kwargs)
    assert wait_until(lambda: monitor.latest().connected)
    return backend, monitor, teleop


def poll_until(teleop, session, expected: str, **flags) -> str:
    logs: list[str] = flags.pop("logs")
    deadline = time.monotonic() + 2.0
    outcome = "idle"
    while time.monotonic() < deadline:
        outcome = teleop.poll(session, log=logs.append, **flags)
        if outcome == expected:
            return outcome
        time.sleep(0.005)
    return outcome


def test_teleop_button_preempts_armed_autonomy() -> None:
    backend, monitor, teleop = teleop_setup()
    try:
        session = SessionControl()
        logs: list[str] = []
        press(backend, frozenset({TOGGLE_BUTTON}))
        outcome = poll_until(
            teleop,
            session,
            "disarm",
            logs=logs,
            armed=True,
            live=True,
            can_arm=False,
            can_manual=False,
        )
        assert outcome == "disarm"
        assert session.consume_disarm()
        assert session.consume_arm() is False
        assert any("抢断" in message for message in logs)
    finally:
        monitor.stop()


def test_teleop_button_resumes_disarmed_autonomy() -> None:
    backend, monitor, teleop = teleop_setup()
    try:
        session = SessionControl()
        logs: list[str] = []
        press(backend, frozenset({TOGGLE_BUTTON}))
        outcome = poll_until(
            teleop,
            session,
            "arm",
            logs=logs,
            armed=False,
            live=True,
            can_arm=True,
            can_manual=True,
        )
        assert outcome == "arm"
        assert session.consume_arm()
        assert any("恢复自主" in message for message in logs)
    finally:
        monitor.stop()


def test_teleop_ignores_toggle_without_live_session() -> None:
    backend, monitor, teleop = teleop_setup()
    try:
        session = SessionControl()
        logs: list[str] = []
        press(backend, frozenset({TOGGLE_BUTTON}))
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline and not logs:
            assert (
                teleop.poll(
                    session, armed=False, live=False, can_arm=True, can_manual=True, log=logs.append
                )
                == "idle"
            )
            time.sleep(0.005)
        assert any("当前没有运行中的会话" in message for message in logs)
        assert not session.consume_arm()
        assert not session.consume_disarm()
        assert not monitor.consume_toggle()  # the stale press must not leak later
    finally:
        monitor.stop()


def test_teleop_sticks_drive_only_in_manual_mode() -> None:
    backend, monitor, teleop = teleop_setup()
    try:
        session = SessionControl()
        logs: list[str] = []
        backend.queue_readings(GamepadReading(True, "Fake Pad", 0.9, -0.9, frozenset()))
        assert wait_until(lambda: monitor.latest().drive_axis == 0.9)
        # Armed autonomy: sticks are ignored with a single hint.
        assert (
            teleop.poll(
                session, armed=True, live=True, can_arm=False, can_manual=False, log=logs.append
            )
            == "hint"
        )
        assert session.current_manual() is None
        assert any("忽略摇杆" in message for message in logs)
        # Manual mode: the stick becomes a gamepad-sourced manual pulse.
        assert (
            teleop.poll(
                session, armed=False, live=True, can_arm=True, can_manual=True, log=logs.append
            )
            == "manual"
        )
        manual = session.current_manual()
        assert manual is not None
        assert manual.source == "gamepad"
        assert manual.velocity == pytest.approx((0.9 - 0.2) / 0.8)
        assert manual.steer == pytest.approx(-(0.9 - 0.2) / 0.8)
    finally:
        monitor.stop()


def test_teleop_deadzone_keeps_idle_sticks_silent() -> None:
    backend, monitor, teleop = teleop_setup()
    try:
        session = SessionControl()
        backend.queue_readings(GamepadReading(True, "Fake Pad", 0.15, 0.1, frozenset()))
        assert wait_until(lambda: monitor.latest().drive_axis == 0.15)
        assert (
            teleop.poll(
                session, armed=False, live=True, can_arm=True, can_manual=True, log=lambda _m: None
            )
            == "idle"
        )
        assert session.current_manual() is None
    finally:
        monitor.stop()


def test_teleop_debounces_rapid_double_press() -> None:
    backend, monitor, teleop = teleop_setup()
    try:
        session = SessionControl()
        logs: list[str] = []
        press(backend, frozenset({TOGGLE_BUTTON}))
        outcome = poll_until(
            teleop,
            session,
            "disarm",
            logs=logs,
            armed=True,
            live=True,
            can_arm=False,
            can_manual=False,
        )
        assert outcome == "disarm"
        # The second press inside the debounce window is a no-op: the arm state
        # only lands one frame after the disarm event, so a double press must
        # not immediately undo itself.
        press(backend)
        press(backend, frozenset({TOGGLE_BUTTON}))
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            assert (
                teleop.poll(
                    session, armed=True, live=True, can_arm=False, can_manual=False, log=logs.append
                )
                == "idle"
            )
            time.sleep(0.005)
        assert not session.consume_arm()
        assert not any("恢复自主寻路" in message for message in logs)
    finally:
        monitor.stop()


def test_manual_pulse_rejects_unknown_source() -> None:
    session = SessionControl()
    with pytest.raises(ValueError):
        session.request_manual(0.1, 0.0, 0.2, source="wifi")


# ---------- run_loop integration ----------


def build_runtime(robot: RecordingRobot, session: SessionControl, sink, **kwargs):
    return run_loop(
        SyntheticBallSource(SyntheticConfig(fps=100.0, realtime=True)),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig(approach_mode="slow_realtime")),
        robot,
        armed=False,
        session=session,
        event_sink=sink,
        **kwargs,
    )


def test_run_loop_disarm_then_rearm_via_session_requests() -> None:
    session = SessionControl()
    robot = RecordingRobot()
    events: list[RuntimeEvent] = []
    state = {"disarmed": False, "rearmed": False}

    def sink(event: RuntimeEvent) -> None:
        events.append(event)
        if event.kind == "started":
            session.request_arm()
        elif event.kind == "disarmed":
            session.request_manual(0.5, 0.0, 0.3, source="gamepad")
        elif event.kind == "frame":
            if event.frame_count == 8 and not state["disarmed"]:
                state["disarmed"] = True
                session.request_disarm()
            elif event.frame_count == 14 and state["disarmed"] and not state["rearmed"]:
                state["rearmed"] = True
                session.request_arm()
            elif event.frame_count >= 20:
                session.request_stop()

    result = build_runtime(robot, session, sink)
    kinds = [event.kind for event in events]
    assert result.termination == "stop_requested"
    assert kinds.index("disarmed") > kinds.index("armed")
    assert "armed" in kinds[kinds.index("disarmed") :]  # autonomy resumed afterwards
    disarmed_event = next(event for event in events if event.kind == "disarmed")
    assert disarmed_event.output_source == "disarmed"
    assert not disarmed_event.armed
    assert any(event.output_source == "gamepad" for event in events if event.kind == "frame")
    assert robot.raw_payloads and any(
        payload["v"] == pytest.approx(0.5) for payload in robot.raw_payloads
    )
    assert robot.commands[-1] == RobotCommand.stop()
    resumed = [event for event in events if event.kind == "frame" and event.frame_count > 15]
    assert resumed and all(event.armed for event in resumed)


def test_run_loop_disarm_from_idle_session_is_harmless() -> None:
    session = SessionControl()
    robot = RecordingRobot()
    events: list[RuntimeEvent] = []

    def sink(event: RuntimeEvent) -> None:
        events.append(event)
        if event.kind == "started":
            session.request_disarm()  # never armed: no-op, must not disturb the loop
        if event.kind == "frame" and event.frame_count >= 3:
            session.request_stop()

    result = build_runtime(robot, session, sink)
    assert result.termination == "stop_requested"
    assert not any(event.kind == "disarmed" for event in events)
    assert all(command == RobotCommand.stop() for command in robot.commands)


# ---------- robustness fixes ----------


def test_safe_print_tolerates_missing_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hcirobot.app.sys.stdout", None, raising=False)
    _safe_print("state line")  # must not raise (pythonw has no stdout)


def test_frame_write_failure_degrades_instead_of_killing_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls = {"count": 0}

    def broken_imwrite(_path, _image):
        calls["count"] += 1
        raise OSError("disk full")

    monkeypatch.setattr("hcirobot.app.cv2.imwrite", broken_imwrite)
    session = SessionControl()
    events: list[RuntimeEvent] = []

    def sink(event: RuntimeEvent) -> None:
        events.append(event)
        if event.kind == "frame" and event.frame_count >= 3:
            session.request_stop()

    result = build_runtime(RecordingRobot(), session, sink, output_dir=tmp_path)
    assert result.termination == "stop_requested"
    assert calls["count"] == 1  # saving disabled after the first failure
    assert any(event.kind == "warning" for event in events)


# ---------- GUI model ----------


def test_gui_model_handles_disarmed_gamepad_and_warning_events() -> None:
    model = GuiModel()
    model.session_state = SessionState.RUNNING
    model.session_id = 7
    model.armed = True

    model.apply_event(RuntimeEvent("disarmed", "manual takeover", session_id=7))
    assert not model.armed
    assert any("抢断" in message for message in model.logs)

    model.apply_event(RuntimeEvent("gamepad", "手柄已连接：Fake Pad"))
    assert any("Fake Pad" in message for message in model.logs)

    model.apply_event(RuntimeEvent("warning", "帧保存失败", session_id=7))
    assert any("帧保存失败" in message for message in model.logs)


# ---------- CLI wiring ----------


def test_cli_gamepad_teleop_lifecycle() -> None:
    from hcirobot.cli import _start_gamepad_teleop

    session, sink, thread, monitor, active = _start_gamepad_teleop(None, armed_at_start=False)
    try:
        assert sink is not None
        sink(RuntimeEvent("started", session_id=session.session_id))
        sink(RuntimeEvent("frame", armed=True, session_id=session.session_id))
        assert thread.is_alive()
        assert not session.stopped
    finally:
        active.clear()
        thread.join(timeout=1.0)
        monitor.stop()
        session.request_stop()
    assert not thread.is_alive()


# ---------- XInput backend (native Windows path) ----------


def make_xinput_state(buttons: int, lx: int, ly: int):
    from hcirobot.gamepad import _XInputState

    state = _XInputState()
    state.Gamepad.wButtons = buttons
    state.Gamepad.sThumbLX = lx
    state.Gamepad.sThumbLY = ly
    return state


def test_xinput_state_mapping_matches_sdl_button_order() -> None:
    from hcirobot.gamepad import XInputGamepadBackend

    # A(0x1000) + B(0x2000) + Start(0x0010); stick pushed fully up-right.
    state = make_xinput_state(0x1000 | 0x2000 | 0x0010, 32767, 32767)
    reading = XInputGamepadBackend._reading_from_state("XInput 手柄 #0", state)
    assert reading.connected
    assert reading.buttons == frozenset({0, 1, 7})  # A, B(toggle), Start
    assert reading.drive_axis == pytest.approx(32767 / 32768)  # XInput: +Y is up = forward
    assert reading.steer_axis == pytest.approx(32767 / 32768)


def test_xinput_backend_reports_missing_library_as_none() -> None:
    from hcirobot.gamepad import XInputGamepadBackend

    backend = XInputGamepadBackend(library=False)
    assert backend.probe() is None
    assert "XInput" in backend.last_error
    with pytest.raises(RuntimeError):
        backend.read()


def test_composite_backend_prefers_first_candidate_that_opens() -> None:
    from hcirobot.gamepad import CompositeGamepadBackend

    failing = FakeBackend()  # no queued probe -> None, 未检测到手柄
    working = FakeBackend()
    working.queue_probe("Working Pad")
    working.queue_readings(GamepadReading(True, "Working Pad", 0.4, 0.0, frozenset()))

    composite = CompositeGamepadBackend([failing, working])
    assert composite.probe() == "Working Pad"
    assert wait_until(lambda: composite.read().drive_axis == 0.4)
    composite.close()

    both_failing = CompositeGamepadBackend([FakeBackend(), FakeBackend()])
    assert both_failing.probe() is None
    assert "未检测到手柄" in both_failing.last_error


def test_default_backend_on_windows_leads_with_xinput() -> None:
    from hcirobot.gamepad import CompositeGamepadBackend, XInputGamepadBackend, default_backend

    backend = default_backend()
    if sys.platform == "win32":
        assert isinstance(backend, CompositeGamepadBackend)
        assert isinstance(backend._candidates[0], XInputGamepadBackend)
    else:
        from hcirobot.gamepad import PygameGamepadBackend

        assert isinstance(backend, PygameGamepadBackend)


# ---------- action buttons (triggers / RB) ----------


def test_xinput_triggers_surface_as_synthetic_buttons() -> None:
    from hcirobot.gamepad import TRIGGER_LEFT_BUTTON, TRIGGER_RIGHT_BUTTON, XInputGamepadBackend

    state = make_xinput_state(0, 0, 0)
    state.Gamepad.bLeftTrigger = 255  # LT held past threshold
    state.Gamepad.bRightTrigger = 10  # resting, below threshold
    reading = XInputGamepadBackend._reading_from_state("XInput 手柄 #0", state)
    assert TRIGGER_LEFT_BUTTON in reading.buttons
    assert TRIGGER_RIGHT_BUTTON not in reading.buttons

    state.Gamepad.bLeftTrigger = 0
    state.Gamepad.bRightTrigger = 255
    reading = XInputGamepadBackend._reading_from_state("XInput 手柄 #0", state)
    assert TRIGGER_LEFT_BUTTON not in reading.buttons
    assert TRIGGER_RIGHT_BUTTON in reading.buttons


def test_monitor_latches_action_button_edges() -> None:
    from hcirobot.gamepad import ACTION_BUTTONS

    rb = next(index for index, name in ACTION_BUTTONS.items() if name == "right_grip")
    backend = FakeBackend()
    backend.queue_probe("Fake Pad")
    monitor = build_monitor(backend)
    try:
        assert wait_until(lambda: monitor.latest().connected)
        press(backend, frozenset({rb}))
        press(backend, frozenset({rb}))  # held: still one edge
        press(backend)  # release
        assert wait_until(lambda: monitor.consume_actions() == ("right_grip",))
        assert monitor.consume_actions() == ()
    finally:
        monitor.stop()


def test_teleop_forwards_action_buttons_in_manual_mode() -> None:
    from hcirobot.gamepad import ACTION_BUTTONS

    rb = next(index for index, name in ACTION_BUTTONS.items() if name == "right_grip")
    backend, monitor, teleop = teleop_setup()
    try:
        session = SessionControl()
        logs: list[str] = []
        press(backend, frozenset({rb}))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and session.consume_action() is None:
            teleop.poll(
                session, armed=False, live=True, can_arm=True, can_manual=True, log=logs.append
            )
            time.sleep(0.005)
        assert session.consume_action() is None  # consumed exactly once by the loop
        assert any("right_grip" in message for message in logs)
    finally:
        monitor.stop()


def test_teleop_ignores_action_buttons_while_autonomy_armed() -> None:
    from hcirobot.gamepad import ACTION_BUTTONS

    rb = next(index for index, name in ACTION_BUTTONS.items() if name == "right_grip")
    backend, monitor, teleop = teleop_setup()
    try:
        session = SessionControl()
        logs: list[str] = []
        press(backend, frozenset({rb}))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            teleop.poll(
                session, armed=True, live=True, can_arm=False, can_manual=False, log=logs.append
            )
            time.sleep(0.005)
            if any("忽略" in message for message in logs):
                break
        assert session.consume_action() is None
        assert not monitor.consume_actions()  # dropped, not left pending
        assert any("自主模式" in message for message in logs)
    finally:
        monitor.stop()


# ---------- arrival action ----------


def test_arrival_action_config_validation() -> None:
    assert ControllerConfig().arrival_action == "right_grip"
    assert ControllerConfig(arrival_action="").arrival_action == ""
    with pytest.raises(ValueError, match="arrival action"):
        ControllerConfig(arrival_action="bad name!")


def _run_arrival_session(robot: RecordingRobot, controller: VisualApproachController):
    class StepClock:
        def __init__(self, step: float = 0.1) -> None:
            self.value = -step
            self.step = step

        def __call__(self) -> float:
            self.value += self.step
            return self.value

    return run_loop(
        SyntheticBallSource(SyntheticConfig(fps=100.0, realtime=True)),
        RedBallDetector(DetectorConfig()),
        controller,
        robot,
        armed=True,
        clock=StepClock(),
        event_sink=lambda _event: None,
    )


def test_arrived_session_fires_arrival_action_once() -> None:
    robot = RecordingRobot()
    result = _run_arrival_session(
        robot, VisualApproachController(ControllerConfig(approach_mode="slow_realtime"))
    )
    assert result.termination == "arrived"
    assert robot.actions.count("right_grip") == 1
    assert robot.commands[-1] == RobotCommand.stop()


def test_arrived_session_without_arrival_action_sends_none() -> None:
    robot = RecordingRobot()
    result = _run_arrival_session(
        robot, VisualApproachController(ControllerConfig(arrival_action=""))
    )
    assert result.termination == "arrived"
    assert robot.actions == []
