from __future__ import annotations

import contextlib
import itertools
import json
import queue
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
from numpy.typing import NDArray

from .controller import ControlDecision, VisualApproachController
from .detector import RedBallDetector
from .model import ControlState, Detection, RobotCommand
from .navigation import ObstaclePolicy
from .robot import RobotBackend
from .video import annotate


@dataclass(frozen=True, slots=True)
class RunResult:
    frames: int
    final_state: ControlState
    states_seen: tuple[ControlState, ...]
    termination: str = "completed"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    kind: str
    message: str = ""
    frame: NDArray[Any] | None = field(default=None, repr=False, compare=False)
    detection: Detection | None = None
    decision: ControlDecision | None = None
    frame_count: int = 0
    seq: int = 0
    session_id: int = 0
    output_v: float = 0.0
    output_steer: float = 0.0
    output_grab: bool = False
    output_source: str = "shutdown"
    armed: bool = False
    obstacle_state: str | None = None
    distance_mm: float | None = None
    avoid_count: int | None = None


EventSink = Callable[[RuntimeEvent], None]

_ACTION_NAME = re.compile(r"[A-Za-z0-9_]{1,32}")
_OBSTACLE_EVENT_MIN_INTERVAL_S = 0.2  # CAUTION/CLEAR flicker near a threshold must not spam events.
_session_counter = itertools.count(1)


@dataclass(frozen=True, slots=True)
class ManualCommand:
    """Raw velocity pulse that overrides autonomy while active."""

    velocity: float
    steer: float
    until: float
    source: str = "manual"


_MANUAL_SOURCES = frozenset({"manual", "gamepad"})


class SessionControl:
    """Thread-safe requests consumed by the control worker."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._arm = threading.Event()
        self._disarm = threading.Event()
        self._estop = threading.Event()
        self._lock = threading.Lock()
        self._manual: ManualCommand | None = None
        self._action: str | None = None
        self._robot: RobotBackend | None = None
        self.session_id = next(_session_counter)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def attach_robot(self, robot: RobotBackend) -> None:
        """Register the backend so stop/estop can cancel connect or a stuck send."""
        with self._lock:
            self._robot = robot
            stopped = self._stop.is_set()
        if stopped:
            self._cancel_robot()

    def request_arm(self) -> None:
        if not self._estop.is_set():
            self._arm.set()

    def request_disarm(self) -> None:
        """Preempt autonomy without stopping the session (gamepad takeover)."""
        self._disarm.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._cancel_robot()

    def request_estop(self) -> None:
        self._estop.set()
        self._stop.set()
        self._cancel_robot()

    def request_manual(self, velocity: float, steer: float, seconds: float, *, source: str = "manual") -> None:
        if not -1.0 <= velocity <= 1.0:
            raise ValueError("manual velocity must be between -1 and 1")
        if not -1.0 <= steer <= 1.0:
            raise ValueError("manual steer must be between -1 and 1")
        if not 0.0 < seconds <= 5.0:
            raise ValueError("manual duration must be between 0 and 5 seconds")
        if source not in _MANUAL_SOURCES:
            raise ValueError("manual source must be one of: " + ", ".join(sorted(_MANUAL_SOURCES)))
        with self._lock:
            self._manual = ManualCommand(velocity, steer, time.monotonic() + seconds, source)

    def current_manual(self) -> ManualCommand | None:
        with self._lock:
            if self._manual is None:
                return None
            if time.monotonic() >= self._manual.until:
                self._manual = None
                return None
            return self._manual

    def request_action(self, name: str) -> None:
        if not _ACTION_NAME.fullmatch(name):
            raise ValueError("action name must be 1-32 alphanumeric or underscore characters")
        with self._lock:
            self._action = name

    def consume_action(self) -> str | None:
        with self._lock:
            action, self._action = self._action, None
            return action

    def consume_arm(self) -> bool:
        if self._arm.is_set():
            self._arm.clear()
            return True
        return False

    def consume_disarm(self) -> bool:
        if self._disarm.is_set():
            self._disarm.clear()
            return True
        return False

    def consume_estop(self) -> bool:
        if self._estop.is_set():
            self._estop.clear()
            return True
        return False

    def _cancel_robot(self) -> None:
        with self._lock:
            robot = self._robot
        cancel = getattr(robot, "cancel", None)
        if cancel is None:
            return
        with contextlib.suppress(Exception):  # cancel is best-effort unblocking.
            cancel()


_END = object()


def _safe_print(message: str) -> None:
    """Print state lines without dying under pythonw (stdout is None) or a closed pipe."""
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(message, flush=True, file=stream)
    except (OSError, ValueError):
        pass


def _publish(output: queue.Queue, stop: threading.Event, item: object) -> bool:
    while not stop.is_set():
        try:
            output.put(item, timeout=0.05)
            return True
        except queue.Full:
            continue
    return False


def _publish_latest(output: queue.Queue, stop: threading.Event, frame) -> bool:
    """Enqueue a video frame with latest-wins semantics.

    A blocking put would backpressure the capture thread all the way to the
    socket: whenever detection+control run slower than the camera, frames pile
    up in kernel buffers and the loop reacts to ever-staler images (growing
    latency, align oscillation). Dropping the queued frame instead bounds the
    latency to one frame plus one processing step.
    """
    while not stop.is_set():
        try:
            output.put_nowait(frame)
            return True
        except queue.Full:
            try:
                output.get_nowait()  # discard the stale frame
            except queue.Empty:
                pass
    return False


def _capture_frames(source, output: queue.Queue, stop: threading.Event, frame_recorder=None) -> None:
    try:
        for frame in source:
            if frame_recorder is not None:
                frame_recorder.offer(frame)  # tap before latest-wins: record every frame
            if not _publish_latest(output, stop, frame):
                return
        _publish(output, stop, _END)
    except Exception as exc:  # noqa: BLE001 - transport exception crosses worker boundary.
        _publish(output, stop, exc)
    finally:
        # A session that ends mid-capture must not leave a recording running.
        if frame_recorder is not None:
            with contextlib.suppress(Exception):
                frame_recorder.stop_session()


def _emit(sink: EventSink | None, event: RuntimeEvent) -> None:
    if sink is not None:
        sink(event)


def run_loop(
    source,
    detector: RedBallDetector,
    controller: VisualApproachController,
    robot: RobotBackend,
    *,
    armed: bool,
    max_frames: int = 0,
    frame_timeout_seconds: float = 0.75,
    output_dir: Path | None = None,
    clock=time.monotonic,
    session: SessionControl | None = None,
    event_sink: EventSink | None = None,
    obstacle_policy: ObstaclePolicy | None = None,
    frame_recorder=None,
) -> RunResult:
    if frame_timeout_seconds <= 0:
        raise ValueError("frame timeout must be positive")
    if obstacle_policy is not None and getattr(robot, "latest_distance", None) is None:
        raise ValueError("obstacle policy requires a robot backend with distance telemetry")
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
    session = session or SessionControl()
    session_id = session.session_id
    event_seq = itertools.count(1)
    is_armed = armed
    output_v = 0.0
    output_steer = 0.0
    output_grab = False
    output_source = "shutdown"

    def emit(kind: str, message: str = "", **kwargs: Any) -> None:
        event_values = {
            "output_v": output_v,
            "output_steer": output_steer,
            "output_grab": output_grab,
            "output_source": output_source,
            "armed": is_armed,
            "obstacle_state": obstacle_policy.state.value if obstacle_policy is not None else None,
            "distance_mm": obstacle_policy.last_distance_mm if obstacle_policy is not None else None,
            "avoid_count": obstacle_policy.avoid_count if obstacle_policy is not None else None,
        }
        event_values.update(kwargs)
        _emit(
            event_sink,
            RuntimeEvent(kind, message, seq=next(event_seq), session_id=session_id, **event_values),
        )

    def send_command(command: RobotCommand, source_name: str) -> None:
        nonlocal output_v, output_steer, output_grab, output_source
        robot.send(command)
        output_v = command.velocity
        output_steer = command.steer
        output_grab = command.grab
        output_source = source_name

    def send_vector(velocity: float, steer: float, source_name: str) -> None:
        nonlocal output_v, output_steer, output_grab, output_source
        payload = {
            "v": round(velocity, 4),
            "steer": round(steer, 4),
            "grab": False,
            "t": datetime.now(UTC).isoformat(),
        }
        robot.send_raw(payload)
        output_v = float(payload["v"])
        output_steer = float(payload["steer"])
        output_grab = False
        output_source = source_name

    states_seen: list[ControlState] = []
    frame_count = 0
    termination = "completed"
    last_obstacle_key: tuple[str, str, str] | None = None
    last_obstacle_emitted = float("-inf")
    manual_was_active = False
    save_frames = True  # flips off permanently once writing a frame fails
    if armed:
        controller.arm(clock())

    frames: queue.Queue = queue.Queue(maxsize=1)
    capture_stop = threading.Event()
    capture_thread = threading.Thread(
        target=_capture_frames,
        args=(source, frames, capture_stop, frame_recorder),
        name="video-capture",
        daemon=True,
    )
    capture_thread.start()
    emit("started", "session started")
    cleanup_error: Exception | None = None
    last_frame_received = time.monotonic()

    def finish_safe(reason: str) -> None:
        controller.fail_safe(reason)
        send_command(RobotCommand.stop(), "shutdown")

    try:
        while True:
            action = session.consume_action()
            if action is not None:
                robot.send_action(action)
                emit("action", action)
            if session.consume_estop():
                controller.estop("operator_estop")
                is_armed = False
                send_command(RobotCommand.stop(), "estop")
                termination = "estop"
                emit("estop", "operator emergency stop")
                break
            if session.stopped:
                is_armed = False
                send_command(RobotCommand.stop(), "shutdown")
                termination = "stop_requested"
                emit("stopping", "stop requested")
                break
            # consume_disarm() runs first so a stray request is cleared even
            # when the session is already disarmed.
            if session.consume_disarm() and is_armed:
                is_armed = False
                controller.reset()
                send_command(RobotCommand.stop(), "disarmed")
                emit("disarmed", "manual takeover: autonomy disarmed")
            if session.consume_arm():
                is_armed = True
                if controller.state is ControlState.IDLE:
                    controller.arm(clock())
                    emit("armed", "autonomy armed")

            try:
                item = frames.get(timeout=min(frame_timeout_seconds, 0.05))
            except queue.Empty:
                if time.monotonic() - last_frame_received < frame_timeout_seconds:
                    continue
                if controller.state is ControlState.IDLE:
                    termination = "preview_timeout"
                    break
                finish_safe("video_timeout")
                termination = "video_timeout"
                states_seen.append(ControlState.LOST_SAFE)
                break
            last_frame_received = time.monotonic()
            if item is _END:
                if controller.state not in (ControlState.ARRIVED, ControlState.IDLE):
                    finish_safe("video_ended")
                    termination = "video_ended"
                    if not states_seen or states_seen[-1] is not ControlState.LOST_SAFE:
                        states_seen.append(ControlState.LOST_SAFE)
                break
            if isinstance(item, Exception):
                raise item

            detection = detector.process(item)
            decision = controller.update(detection, clock())
            manual = session.current_manual()
            obstacle = None
            vision_blocked = None
            if obstacle_policy is not None:
                vision_blocked = obstacle_policy.observe_frame(item)
                sonar = robot.latest_distance()
                obstacle = obstacle_policy.update(sonar, vision_blocked, clock())
                if obstacle_policy.latched_blocked:
                    controller.fail_safe("obstacle_blocked")
                    is_armed = False
                    send_command(RobotCommand.stop(), "shutdown")
                    termination = "obstacle_blocked"
                    if not states_seen or states_seen[-1] is not ControlState.LOST_SAFE:
                        states_seen.append(ControlState.LOST_SAFE)
                    emit(
                        "obstacle",
                        json.dumps(
                            {
                                "state": obstacle.state.value,
                                "reason": obstacle.reason,
                                "action": obstacle.action,
                                "distance": obstacle_policy.last_distance_mm,
                                "vision_blocked": vision_blocked,
                                "avoid_count": obstacle_policy.avoid_count,
                            },
                            ensure_ascii=False,
                        ),
                        frame_count=frame_count,
                    )
                    break
            if manual is not None and not manual_was_active and obstacle_policy is not None:
                # First frame of a manual pulse: abort any in-flight avoidance
                # phase so no residual backup/turn vector bursts out after it.
                obstacle_policy.cancel_maneuver()
            manual_was_active = manual is not None
            maneuver_suppressed = obstacle is not None and obstacle.action == "maneuver" and not is_armed
            if manual is not None:
                send_vector(manual.velocity, manual.steer, manual.source)
            elif maneuver_suppressed:
                # An un-armed session must never self-activate an avoidance
                # motion: the maneuver degrades to a plain hold.
                send_command(RobotCommand.stop(), "unarmed_hold")
            elif obstacle is not None and obstacle.action == "maneuver":
                send_vector(obstacle.velocity, obstacle.steer, "obstacle_maneuver")
            elif obstacle is not None and obstacle.action == "hold":
                send_command(RobotCommand.stop(), "obstacle_hold")
            else:
                source_name = "autonomy" if is_armed else "unarmed_hold"
                send_command(decision.command, source_name)
            frame_count += 1
            rendered = annotate(
                item,
                detection,
                decision,
                output_v=output_v,
                output_steer=output_steer,
                output_source=output_source,
                distance_mm=obstacle_policy.last_distance_mm if obstacle_policy is not None else None,
                obstacle_state=(
                    obstacle_policy.state.value
                    if obstacle_policy is not None and obstacle_policy.last_distance_mm is not None
                    else None
                ),
            )
            emit(
                "frame",
                decision.reason,
                frame=rendered,
                detection=detection,
                decision=decision,
                frame_count=frame_count,
            )
            if obstacle is not None:
                action_effective = "hold" if maneuver_suppressed else obstacle.action
                obstacle_key = (obstacle.state.value, action_effective, obstacle.reason)
                now_s = clock()
                if (
                    obstacle_key != last_obstacle_key
                    and now_s - last_obstacle_emitted >= _OBSTACLE_EVENT_MIN_INTERVAL_S
                ):
                    last_obstacle_key = obstacle_key
                    last_obstacle_emitted = now_s
                    payload = {
                        "state": obstacle.state.value,
                        "reason": obstacle.reason,
                        "action": obstacle.action,
                        "distance": obstacle_policy.last_distance_mm if obstacle_policy else None,
                        "vision_blocked": vision_blocked,
                        "avoid_count": obstacle_policy.avoid_count if obstacle_policy else None,
                    }
                    if maneuver_suppressed:
                        payload["suppressed"] = True
                    emit(
                        "obstacle",
                        json.dumps(payload, ensure_ascii=False),
                        frame_count=frame_count,
                    )
            if not states_seen or states_seen[-1] is not decision.state:
                states_seen.append(decision.state)
                payload = {
                    "frame": frame_count,
                    "state": decision.state.value,
                    "reason": decision.reason,
                    "v": round(output_v, 3),
                    "steer": round(output_steer, 3),
                    "source": output_source,
                }
                message = json.dumps(payload, ensure_ascii=False)
                _safe_print(message)
                emit("state", message, frame_count=frame_count)
            if output_dir is not None and save_frames:
                # Disk-full or an unwritable path must degrade to "stop saving",
                # never kill the control loop (frame writing is a side output).
                try:
                    if not cv2.imwrite(str(output_dir / f"frame-{frame_count:04d}.png"), rendered):
                        raise OSError("cv2.imwrite returned false")
                except Exception as exc:  # noqa: BLE001 - side output only.
                    save_frames = False
                    emit("warning", f"帧保存失败，本次会话不再保存：{type(exc).__name__}: {exc}")
            if decision.state in (ControlState.ARRIVED, ControlState.LOST_SAFE):
                termination = decision.state.value.lower()
                if decision.state is ControlState.ARRIVED and controller.config.arrival_action:
                    # One-shot arrival action (course button namespace, e.g.
                    # right_grip = crouch-and-extinguish).  A send failure must
                    # not mask the arrival: warn and still end the session.
                    try:
                        robot.send_action(controller.config.arrival_action)
                        emit("action", controller.config.arrival_action, frame_count=frame_count)
                    except (ConnectionError, OSError, ValueError) as exc:
                        emit("warning", f"到达动作下发失败：{exc}", frame_count=frame_count)
                break
            if max_frames > 0 and frame_count >= max_frames:
                termination = "max_frames"
                break
    except ConnectionError as exc:
        is_armed = False
        output_v = 0.0
        output_steer = 0.0
        output_grab = False
        if session.consume_estop():
            controller.estop("operator_estop")
            output_source = "estop"
            termination = "estop"
            emit("estop", "operator emergency stop")
        elif session.stopped:
            output_source = "shutdown"
            termination = "stop_requested"
            emit("stopping", "stop requested")
        else:
            controller.fail_safe("robot_connection_lost")
            output_source = "shutdown"
            termination = "robot_connection_lost"
            emit("error", f"robot connection lost: {exc}")
        if not states_seen or states_seen[-1] is not ControlState.LOST_SAFE:
            states_seen.append(ControlState.LOST_SAFE)
    except Exception as exc:
        controller.fail_safe("runtime_error")
        is_armed = False
        termination = "runtime_error"
        try:
            send_command(RobotCommand.stop(), "shutdown")
        except (ConnectionError, OSError):
            output_v = 0.0
            output_steer = 0.0
            output_grab = False
            output_source = "shutdown"
        if not states_seen or states_seen[-1] is not ControlState.LOST_SAFE:
            states_seen.append(ControlState.LOST_SAFE)
        emit("error", f"runtime error: {type(exc).__name__}: {exc}", frame_count=frame_count)
        raise
    finally:
        capture_stop.set()
        try:
            robot.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must continue.
            cleanup_error = exc
        is_armed = False
        output_v = 0.0
        output_steer = 0.0
        output_grab = False
        output_source = "estop" if termination == "estop" else "shutdown"
        try:
            source.close()
        except Exception as exc:  # noqa: BLE001 - robot cleanup already ran.
            cleanup_error = cleanup_error or exc
        capture_thread.join(timeout=0.5)
        if capture_thread.is_alive():
            cleanup_error = cleanup_error or RuntimeError("video capture thread did not stop")

    result = RunResult(frame_count, controller.state, tuple(states_seen), termination)
    if cleanup_error is not None:
        emit(
            "error",
            f"cleanup error: {type(cleanup_error).__name__}: {cleanup_error}",
            frame_count=frame_count,
        )
    emit("finished", termination, frame_count=frame_count)
    if cleanup_error is not None:
        raise cleanup_error
    return result
