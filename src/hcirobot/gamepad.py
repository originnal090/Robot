"""Gamepad teleoperation: stick-to-TCP driving with an autonomy takeover button.

The course baseline reads the controller inside Unity and forwards it to the
robot over TCP 5075.  This module moves that role to the Python client: a
background monitor polls the gamepad, one designated button preempts autonomy
(``抢断`` -> manual) and pressing it again resumes autonomous pathfinding.  The
left stick drives/steps laterally, the right stick rotates the body, and the
D-Pad selects one of three persistent camera pitch positions.  pygame is an
optional dependency; without it the monitor reports the reason and never
disturbs the control loop.
"""

from __future__ import annotations

import contextlib
import ctypes
import math
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

# XInput-style button indices (0=A, 1=B, 2=X, 3=Y, ...).  B is the takeover
# button: press once to preempt autonomy, press again to resume it.
TOGGLE_BUTTON = 1
TOGGLE_HINT = "B 抢断/恢复，LS 前后/横移，RS 左右旋转，十字键上下俯仰，LT/RT 爬起，RB 灭火"
DEADZONE = 0.20  # matches the TonyPi TCP_connect stick deadzone
_DRIVE_AXIS = 1  # left stick Y: -1 when pushed up
_LATERAL_AXIS = 0  # left stick X: +1 when pushed right
_STEER_AXIS = 2  # right stick X: +1 when pushed right

# Triggers are analog on XInput (SDL axes 4/5, XInput bytes); backends surface
# them as synthetic buttons past the 11 SDL XInput buttons.
TRIGGER_LEFT_BUTTON = 11
TRIGGER_RIGHT_BUTTON = 12
_TRIGGER_AXIS_PRESSED = 0.5  # normalized SDL trigger axis threshold
_TRIGGER_BYTE_PRESSED = 64  # XInput byte threshold out of 255

# Gamepad buttons -> CMD action names in the course button namespace, so both
# the course TCP_connect.py and robot_side/tonypi_server.py accept them:
# left_trigger/right_trigger -> stand_up_front/back, right_grip -> outfire.
ACTION_BUTTONS: dict[int, str] = {
    TRIGGER_LEFT_BUTTON: "left_trigger",
    TRIGGER_RIGHT_BUTTON: "right_trigger",
    5: "right_grip",  # RB, the gamepad counterpart of the PICO right grip
}

Log = Callable[[str], None]


def sanitize_axis(value: object) -> float:
    """Coerce a raw axis value into a finite float clamped to [-1, 1]."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(-1.0, min(1.0, number))


def apply_deadzone(value: object, deadzone: float) -> float:
    """Scaled deadzone: values inside the band map to 0, the rest rescale to [-1, 1]."""
    axis = sanitize_axis(value)
    band = min(max(deadzone, 0.0), 1.0)
    if band <= 0.0:
        return axis
    if abs(axis) <= band:
        return 0.0
    return math.copysign((abs(axis) - band) / (1.0 - band), axis)


def map_to_command(drive: object, steer: object, deadzone: float) -> tuple[float, float]:
    """Map raw stick readings to a ``(v, steer)`` command in protocol sign convention."""
    return apply_deadzone(drive, deadzone), apply_deadzone(steer, deadzone)


def map_to_motion(reading: GamepadReading, deadzone: float) -> tuple[float, float, float]:
    """Map LS-Y, RS-X and LS-X to forward, turn and lateral protocol axes."""
    return (
        apply_deadzone(reading.drive_axis, deadzone),
        apply_deadzone(reading.steer_axis, deadzone),
        apply_deadzone(reading.lateral_axis, deadzone),
    )


def _head_axis_from_dpad(up: object, down: object) -> float:
    """Map D-Pad buttons to pitch direction; opposing presses cancel."""
    return float(bool(up)) - float(bool(down))


@dataclass(frozen=True, slots=True)
class GamepadReading:
    """One polled gamepad snapshot; axes use the protocol convention (+forward, +right)."""

    connected: bool = False
    name: str = ""
    drive_axis: float = 0.0
    steer_axis: float = 0.0
    buttons: frozenset[int] = frozenset()
    lateral_axis: float = 0.0
    head_axis: float = 0.0


_DISCONNECTED = GamepadReading()


class GamepadBackend(Protocol):
    """Polling surface implemented by the pygame backend and by test fakes."""

    last_error: str

    def probe(self) -> str | None:
        """Open device 0 and return its name, or None when unavailable."""
        ...

    def read(self) -> GamepadReading:
        """Return the current reading; raise to signal a lost device."""
        ...

    def close(self) -> None: ...


class PygameGamepadBackend:
    """pygame/SDL joystick backend; every failure degrades to 'no device'."""

    def __init__(self) -> None:
        self.last_error = ""
        self._pygame = None
        self._joystick = None

    def probe(self) -> str | None:
        try:
            import pygame  # lazy: optional dependency, import failure is not fatal
        except Exception as exc:  # noqa: BLE001 - report any import-level failure.
            self.last_error = f'未安装 pygame（pip install "hcirobot[gamepad]"）：{exc}'
            return None
        self._close_joystick()
        try:
            pygame.joystick.init()
            if pygame.joystick.get_count() == 0:
                self.last_error = "未检测到手柄"
                return None
            joystick = pygame.joystick.Joystick(0)
            joystick.init()
            if joystick.get_numaxes() < 2:
                self.last_error = f"手柄 {joystick.get_name()} 摇杆轴数不足"
                return None
            self._pygame = pygame
            self._joystick = joystick
            self.last_error = ""
            return joystick.get_name()
        except Exception as exc:  # noqa: BLE001 - a broken device must not kill the thread.
            self.last_error = f"手柄初始化失败：{exc}"
            self._close_joystick()
            return None

    def read(self) -> GamepadReading:
        pygame, joystick = self._pygame, self._joystick
        if pygame is None or joystick is None:
            raise RuntimeError("gamepad is not open")
        try:
            # Joystick axis/button state refreshes from the SDL event queue,
            # and pygame's queue silently DROPS new input once it fills up
            # (pump() alone does not consume anything).  Draining it every poll
            # is the canonical pattern; there is no window to feed events to.
            with contextlib.suppress(Exception):
                pygame.event.get()
            buttons = frozenset(
                index for index in range(joystick.get_numbuttons()) if joystick.get_button(index)
            )
            if joystick.get_numaxes() >= 6:
                # SDL maps XInput triggers to axes 4/5 (0..1); surface them as
                # synthetic buttons so ACTION_BUTTONS works uniformly.
                buttons |= frozenset(
                    synthetic
                    for synthetic, axis in ((TRIGGER_LEFT_BUTTON, 4), (TRIGGER_RIGHT_BUTTON, 5))
                    if sanitize_axis(joystick.get_axis(axis)) > _TRIGGER_AXIS_PRESSED
                )
            hat_y = joystick.get_hat(0)[1] if joystick.get_numhats() > 0 else 0
            return GamepadReading(
                connected=True,
                name=joystick.get_name(),
                drive_axis=-sanitize_axis(joystick.get_axis(_DRIVE_AXIS)),
                lateral_axis=sanitize_axis(joystick.get_axis(_LATERAL_AXIS)),
                steer_axis=(
                    sanitize_axis(joystick.get_axis(_STEER_AXIS))
                    if joystick.get_numaxes() > _STEER_AXIS
                    else 0.0
                ),
                head_axis=_head_axis_from_dpad(hat_y > 0, hat_y < 0),
                buttons=buttons,
            )
        except Exception as exc:  # device unplugged mid-read: reconnect path
            raise RuntimeError(f"读取手柄失败：{exc}") from exc

    def close(self) -> None:
        self._close_joystick()
        pygame = self._pygame
        self._pygame = None
        if pygame is not None:
            try:
                pygame.joystick.quit()
            except Exception:  # noqa: BLE001, S110 - best-effort module teardown.
                pass

    def _close_joystick(self) -> None:
        joystick, self._joystick = self._joystick, None
        if joystick is None:
            return
        try:
            joystick.quit()
        except Exception:  # noqa: BLE001, S110 - best-effort device teardown.
            pass


class _XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XInputState(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", _XInputGamepad),
    ]


# XINPUT_GAMEPAD_* bits mapped to the SDL/XInput standard button order
# (0=A, 1=B, 2=X, 3=Y, 4=LB, 5=RB, 6=Back, 7=Start, 8=Guide, 9=LS, 10=RS),
# matching what PygameGamepadBackend reports so TOGGLE_BUTTON stays "B".
_XINPUT_BUTTON_BITS = (
    (0x1000, 0),  # A
    (0x2000, 1),  # B
    (0x4000, 2),  # X
    (0x8000, 3),  # Y
    (0x0100, 4),  # left shoulder
    (0x0200, 5),  # right shoulder
    (0x0020, 6),  # back
    (0x0010, 7),  # start
    (0x0400, 8),  # guide
    (0x0040, 9),  # left stick press
    (0x0080, 10),  # right stick press
)


class XInputGamepadBackend:
    """Native XInput polling via ctypes — the same data path joy.cpl uses.

    pygame/SDL on Windows sometimes enumerates a dongle's DirectInput side
    but never delivers its events in a headless (windowless) process, while
    the XInput side keeps working system-wide.  Polling XInputGetState
    directly sidesteps the SDL event queue entirely.  State is read per call,
    so nothing can go stale; a disconnected device raises for the monitor to
    re-probe.
    """

    _DEVICE_NOT_CONNECTED = 1164
    _THUMB_SCALE = 32768.0

    def __init__(self, library: ctypes.CDLL | bool | None = None) -> None:
        # library=False forces "no XInput" (tests, explicit opt-out); None loads it.
        self.last_error = ""
        if library is None:
            library = self._load_library()
        self._library = library if library else None
        self._user_index: int | None = None
        self._name = ""
        if self._library is None:
            self.last_error = "XInput 库不可用（xinput1_4/xinput1_3/xinput9_1_0）"

    @staticmethod
    def _load_library() -> ctypes.CDLL | None:
        if sys.platform != "win32":
            return None
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                return ctypes.WinDLL(name)
            except OSError:
                continue
        return None

    def probe(self) -> str | None:
        if self._library is None:
            return None  # last_error set in __init__
        state = _XInputState()
        for index in range(4):
            try:
                result = self._library.XInputGetState(index, ctypes.byref(state))
            except Exception as exc:  # noqa: BLE001 - a broken DLL must not kill the thread.
                self.last_error = f"XInput 调用失败：{exc}"
                return None
            if result == 0:
                self._user_index = index
                self._name = f"XInput 手柄 #{index}"
                self.last_error = ""
                return self._name
        self._user_index = None
        self.last_error = "未检测到 XInput 手柄"
        return None

    def read(self) -> GamepadReading:
        if self._library is None or self._user_index is None:
            raise RuntimeError("XInput gamepad is not open")
        state = _XInputState()
        result = self._library.XInputGetState(self._user_index, ctypes.byref(state))
        if result == self._DEVICE_NOT_CONNECTED:
            raise RuntimeError("XInput 手柄已断开")
        if result != 0:
            raise RuntimeError(f"XInputGetState failed: {result}")
        return self._reading_from_state(self._name, state)

    @staticmethod
    def _reading_from_state(name: str, state: _XInputState) -> GamepadReading:
        gamepad = state.Gamepad
        buttons = frozenset(index for bit, index in _XINPUT_BUTTON_BITS if gamepad.wButtons & bit)
        buttons |= frozenset(
            synthetic
            for synthetic, value in (
                (TRIGGER_LEFT_BUTTON, gamepad.bLeftTrigger),
                (TRIGGER_RIGHT_BUTTON, gamepad.bRightTrigger),
            )
            if value > _TRIGGER_BYTE_PRESSED
        )
        return GamepadReading(
            connected=True,
            name=name,
            drive_axis=sanitize_axis(gamepad.sThumbLY / XInputGamepadBackend._THUMB_SCALE),
            lateral_axis=sanitize_axis(gamepad.sThumbLX / XInputGamepadBackend._THUMB_SCALE),
            steer_axis=sanitize_axis(gamepad.sThumbRX / XInputGamepadBackend._THUMB_SCALE),
            head_axis=_head_axis_from_dpad(
                gamepad.wButtons & 0x0001,  # XINPUT_GAMEPAD_DPAD_UP
                gamepad.wButtons & 0x0002,  # XINPUT_GAMEPAD_DPAD_DOWN
            ),
            buttons=buttons,
        )

    def close(self) -> None:
        self._user_index = None


class CompositeGamepadBackend:
    """Probe candidates in order and delegate to the first one that opens."""

    def __init__(self, candidates: tuple[GamepadBackend, ...] | list[GamepadBackend]) -> None:
        self._candidates = tuple(candidates)
        self._active: GamepadBackend | None = None
        self.last_error = ""

    def probe(self) -> str | None:
        errors: list[str] = []
        for backend in self._candidates:
            try:
                name = backend.probe()
            except Exception as exc:  # noqa: BLE001 - candidate failures fall through.
                name = None
                errors.append(f"{type(backend).__name__}: {exc}")
            if name:
                self._active = backend
                self.last_error = ""
                return name
            if backend.last_error:
                errors.append(backend.last_error)
        self._active = None
        self.last_error = "；".join(dict.fromkeys(errors)) or "未检测到手柄"
        return None

    def read(self) -> GamepadReading:
        active = self._active
        if active is None:
            raise RuntimeError("no active gamepad backend")
        return active.read()

    def close(self) -> None:
        active, self._active = self._active, None
        if active is None:
            return
        with contextlib.suppress(Exception):
            active.close()


def default_backend() -> GamepadBackend:
    """XInput first on Windows (pygame's SDL path can stay silent headless); pygame elsewhere."""
    if sys.platform == "win32":
        return CompositeGamepadBackend([XInputGamepadBackend(), PygameGamepadBackend()])
    return PygameGamepadBackend()


class GamepadMonitor:
    """Daemon thread that keeps the latest reading fresh and latches toggle edges.

    Hotplug is handled by probing with a growing backoff; a read failure drops
    the device and resumes probing.  Nothing outside the lock ever blocks and
    no exception escapes the thread: a dead monitor simply leaves ``latest()``
    reporting a stale, zeroed reading, which stops the robot.
    """

    def __init__(
        self,
        backend: GamepadBackend | None = None,
        *,
        poll_seconds: float = 0.05,
        probe_seconds: float = 1.0,
        maximum_probe_seconds: float = 5.0,
        stale_after_seconds: float = 0.5,
        on_log: Log | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend or default_backend()
        self._poll_seconds = max(0.005, poll_seconds)
        self._probe_seconds = max(0.01, probe_seconds)
        self._maximum_probe_seconds = max(self._probe_seconds, maximum_probe_seconds)
        self._stale_after_seconds = stale_after_seconds
        self._on_log = on_log
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._latest = _DISCONNECTED
        self._latest_ts = 0.0
        self._toggle_pending = False
        self._action_pending: list[str] = []
        self._connected = False
        self._status_text = "手柄未启动"
        self._thread: threading.Thread | None = None

    @property
    def backend(self) -> GamepadBackend:
        return self._backend

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="gamepad-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        with self._lock:
            self._latest = _DISCONNECTED
            self._connected = False
            self._status_text = "手柄已停止"

    def latest(self) -> GamepadReading:
        with self._lock:
            reading, timestamp = self._latest, self._latest_ts
        if not reading.connected:
            return _DISCONNECTED
        if self._clock() - timestamp > self._stale_after_seconds:
            return _DISCONNECTED
        return reading

    def consume_toggle(self) -> bool:
        with self._lock:
            pending, self._toggle_pending = self._toggle_pending, False
            return pending

    def consume_actions(self) -> tuple[str, ...]:
        """Drain action-button presses latched since the last call."""
        with self._lock:
            actions = tuple(self._action_pending)
            self._action_pending.clear()
            return actions

    def status(self) -> tuple[bool, str]:
        with self._lock:
            return self._connected, self._status_text

    def _run(self) -> None:
        try:
            self._run_loop()
        except Exception as exc:  # noqa: BLE001 - the monitor thread must never die silently.
            self._log(f"手柄监视线程异常退出：{type(exc).__name__}: {exc}")
            with self._lock:
                self._connected = False
                self._status_text = "手柄监视异常"

    def _run_loop(self) -> None:
        backoff = self._probe_seconds
        while not self._stop.is_set():
            name = None
            try:
                name = self._backend.probe()
            except Exception as exc:  # noqa: BLE001 - probe() itself must not kill the loop.
                self._backend.last_error = f"手柄探测异常：{exc}"
            if name is None:
                self._store(_DISCONNECTED)
                with self._lock:
                    self._connected = False
                    self._status_text = self._backend.last_error or "未检测到手柄"
                self._stop.wait(backoff)
                backoff = min(backoff * 1.5, self._maximum_probe_seconds)
                continue
            backoff = self._probe_seconds
            with self._lock:
                self._connected = True
                self._status_text = name
            self._log(f"手柄已连接：{name}")
            self._poll_device()

    def _poll_device(self) -> None:
        previous_buttons = frozenset()
        try:
            while not self._stop.wait(self._poll_seconds):
                reading = self._backend.read()
                if reading.buttons != previous_buttons:
                    self._latch_edges(previous_buttons, reading.buttons)
                    previous_buttons = reading.buttons
                self._store(reading)
        except Exception as exc:  # noqa: BLE001 - unplugged/broken device: reconnect.
            self._log(f"手柄断开，将自动重连：{exc}")
        finally:
            with contextlib.suppress(Exception):
                self._backend.close()
            self._store(_DISCONNECTED)
            with self._lock:
                self._connected = False
                self._status_text = "手柄已断开，正在重新检测"

    def _latch_edges(self, previous_buttons: frozenset[int], pressed: frozenset[int]) -> None:
        """Latch rising edges: B toggles autonomy, action buttons queue their CMD."""
        with self._lock:
            for index in pressed - previous_buttons:
                if index == TOGGLE_BUTTON:
                    self._toggle_pending = True
                elif index in ACTION_BUTTONS:
                    self._action_pending.append(ACTION_BUTTONS[index])

    def _store(self, reading: GamepadReading) -> None:
        with self._lock:
            self._latest = reading
            self._latest_ts = self._clock()

    def _log(self, message: str) -> None:
        callback = self._on_log
        if callback is None:
            return
        try:
            callback(message)
        except Exception:  # noqa: BLE001, S110 - logging must never break the monitor.
            pass


class GamepadTeleop:
    """Bridges monitor state into SessionControl requests.

    The takeover button toggles autonomy: armed -> ``request_disarm`` (manual),
    disarmed -> ``request_arm`` (resume autonomous pathfinding).  Action buttons
    (triggers, RB) become one-shot CMD requests in the course button namespace,
    manual mode only.  Sticks only drive while the session allows manual
    control, so an accidental nudge can never preempt a running autonomy.
    ``poll`` never raises.
    """

    def __init__(
        self,
        monitor: GamepadMonitor,
        *,
        deadzone: float = DEADZONE,
        manual_seconds: float = 0.30,
        toggle_min_interval_seconds: float = 0.25,
        hint_cooldown_seconds: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._monitor = monitor
        self._deadzone = deadzone
        self._manual_seconds = manual_seconds
        self._toggle_min_interval_seconds = toggle_min_interval_seconds
        self._hint_cooldown_seconds = hint_cooldown_seconds
        self._clock = clock
        self._last_toggle_at = float("-inf")
        self._last_hint_at = float("-inf")
        self._head_level = 0
        self._head_axis_direction = 0

    @property
    def deadzone(self) -> float:
        return self._deadzone

    def poll(
        self,
        session,
        *,
        armed: bool,
        live: bool,
        can_arm: bool,
        can_manual: bool,
        log: Log,
    ) -> str:
        """Apply one polling step; returns one of idle/disarm/arm/manual/action/hint."""
        outcome = "idle"
        if self._monitor.consume_toggle():
            outcome = self._apply_toggle(session, armed=armed, live=live, can_arm=can_arm, log=log)
        outcome = self._apply_actions(session, live=live, can_manual=can_manual, log=log) or outcome
        reading = self._monitor.latest()
        head = apply_deadzone(reading.head_axis, self._deadzone)
        outcome = (
            self._apply_head_axis(session, head, live=live, can_manual=can_manual, log=log)
            or outcome
        )
        velocity, steer, lateral = map_to_motion(reading, self._deadzone)
        if velocity == 0.0 and steer == 0.0 and lateral == 0.0:
            return outcome
        if live and can_manual and session is not None:
            try:
                session.request_manual(
                    velocity, steer, self._manual_seconds, lateral=lateral, source="gamepad"
                )
                outcome = "manual"
            except ValueError:
                pass  # sanitized values cannot trip this; guard anyway
        elif live and armed and self._clock() - self._last_hint_at >= self._hint_cooldown_seconds:
            self._last_hint_at = self._clock()
            log("手柄：自主寻路运行中忽略摇杆输入，按 B 键抢断切入手动控制")
            outcome = "hint"
        return outcome

    def _apply_head_axis(
        self, session, value: float, *, live: bool, can_manual: bool, log: Log
    ) -> str:
        """Move one pitch level per neutral-to-up/down D-Pad press."""
        direction = 1 if value > 0 else -1 if value < 0 else 0
        if direction == 0:
            self._head_axis_direction = 0
            return ""
        if direction == self._head_axis_direction:
            return ""
        self._head_axis_direction = direction
        next_level = max(-1, min(1, self._head_level + direction))
        if next_level == self._head_level:
            return ""
        if not live or session is None:
            return ""
        if not can_manual:
            log("手柄：自主模式下忽略头部俯仰，按 B 抢断后再试")
            return ""
        action = {-1: "head_down", 0: "head_center", 1: "head_up"}[next_level]
        try:
            session.request_action(action)
        except ValueError as exc:
            log(f"手柄头部动作被拒绝：{exc}")
            return ""
        self._head_level = next_level
        log(f"手柄头部档位：{action}")
        return "action"

    def _apply_actions(self, session, *, live: bool, can_manual: bool, log: Log) -> str:
        """Forward latched action-button presses as one-shot CMD requests."""
        actions = self._monitor.consume_actions()
        if not actions:
            return ""
        if not live or session is None:
            return ""  # dropped, not queued: stale presses must not fire later
        if not can_manual:
            log("手柄：动作键在自主模式下忽略，按 B 抢断后再试")
            return ""
        for action in actions:
            try:
                session.request_action(action)
                log(f"手柄动作已下发：{action}")
            except ValueError as exc:  # names are fixed valid; guard anyway
                log(f"手柄动作被拒绝：{exc}")
        return "action"

    def _apply_toggle(self, session, *, armed: bool, live: bool, can_arm: bool, log: Log) -> str:
        now = self._clock()
        if now - self._last_toggle_at < self._toggle_min_interval_seconds:
            return "idle"  # debounce: event-driven arm state lags one frame
        if not live or session is None:
            log("手柄：当前没有运行中的会话，按键已忽略")
            return "idle"
        self._last_toggle_at = now
        if armed:
            session.request_disarm()
            log("手柄：抢断自主寻路，切入手动控制（LS 移动、RS 左右旋转、十字键上下俯仰，再按 B 恢复自主）")
            return "disarm"
        if can_arm:
            session.request_arm()
            log("手柄：恢复自主寻路")
            return "arm"
        log("手柄：无法恢复自主（急停或故障未复位），请先在界面上复位")
        return "idle"
