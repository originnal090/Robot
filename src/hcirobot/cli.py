from __future__ import annotations

import argparse
import contextlib
import sys
import threading
import time
from pathlib import Path

from .app import EventSink, RuntimeEvent, SessionControl, run_loop
from .config import (
    controller_config,
    detector_config,
    load_config,
    unity_status_config,
    validate_robot_config,
    validate_video_config,
)
from .controller import VisualApproachController
from .detector import RedBallDetector
from .gamepad import GamepadMonitor, GamepadTeleop
from .navigation import ObstaclePolicy, obstacle_config
from .robot import (
    FanoutRobot,
    MirrorTcpRobot,
    RecordingRobot,
    TcpRobotClient,
    parse_endpoint,
    same_endpoint,
)
from .unity_udp import UnityStatusConfig, UnityStatusPublisher, fanout_event_sinks
from .video import MjpegHttpSource, OpenCvVideoSource, SyntheticBallSource, SyntheticConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TonyPi visual search-align-approach baseline")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and exit before opening runtime resources",
    )
    parser.add_argument("--source", help="synthetic, camera index, video path, or MJPEG URL")
    parser.add_argument(
        "--edge-model",
        type=Path,
        help="use an experimental .npz SVM or a versioned .json detector profile",
    )
    parser.add_argument("--backend", choices=("recording", "tcp"))
    parser.add_argument("--robot-host")
    parser.add_argument("--robot-port", type=int)
    parser.add_argument(
        "--robot-mirror",
        action="append",
        metavar="HOST:PORT",
        help=(
            "best-effort command mirror (repeatable), e.g. 127.0.0.1:5075 to drive "
            "the Unity twin while controlling the real robot"
        ),
    )
    parser.add_argument("--arm", action="store_true", help="allow autonomous movement")
    parser.add_argument(
        "--approach-mode",
        choices=("fast_then_slow", "normal", "normal_then_slow", "slow_realtime"),
        help="walk-burst approach strategy (overrides [controller] approach_mode)",
    )
    parser.add_argument(
        "--gamepad",
        action="store_true",
        help=(
            "enable gamepad teleoperation: LS drives/laterals, RS turns/tilts head; "
            "B preempts autonomy "
            "into manual mode and resumes it on the next press (needs pygame)"
        ),
    )
    parser.add_argument(
        "--no-obstacle", action="store_true", help="disable reactive obstacle avoidance"
    )
    unity_group = parser.add_mutually_exclusive_group()
    unity_group.add_argument(
        "--unity-status",
        dest="unity_status",
        action="store_true",
        default=None,
        help="publish autonomy status to Unity over UDP",
    )
    unity_group.add_argument(
        "--no-unity-status",
        dest="unity_status",
        action="store_false",
        help="disable Unity autonomy status publishing",
    )
    parser.add_argument("--unity-host")
    parser.add_argument("--unity-port", type=int)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--realtime", action="store_true", help="pace synthetic video at configured FPS"
    )
    return parser


def build_obstacle_policy(config: dict, backend: str, disabled: bool) -> ObstaclePolicy | None:
    if disabled or backend != "tcp":
        return None
    values = config.get("obstacle", {})
    if not values.get("enabled", True):
        return None
    return ObstaclePolicy(obstacle_config(values))


def build_source(value: str, video: dict, realtime: bool):
    timeout = float(video["frame_timeout_seconds"])
    if value == "synthetic":
        return SyntheticBallSource(
            SyntheticConfig(
                width=int(video["width"]),
                height=int(video["height"]),
                fps=float(video["fps"]),
                realtime=realtime,
            )
        )
    if value.startswith(("http://", "https://")):
        return MjpegHttpSource(value, timeout)
    if value.isdigit():
        return OpenCvVideoSource(int(value), timeout)
    return OpenCvVideoSource(value, timeout)


def _start_gamepad_teleop(
    unity_publisher,
    *,
    armed_at_start: bool,
) -> tuple[SessionControl, EventSink | None, threading.Thread, GamepadMonitor, threading.Event]:
    """Headless gamepad teleoperation: monitor + poller thread + event-tracking sink.

    The toggle decision mirrors the GUI: while armed, B requests a disarm
    (manual takeover); while disarmed and live, B re-arms autonomy.  The armed
    state is tracked from runtime events because the CLI has no view model.
    """
    session = SessionControl()
    monitor = GamepadMonitor(on_log=_gamepad_log)
    teleop = GamepadTeleop(monitor)
    runtime_state = {"armed": armed_at_start, "live": False}

    def tracking_sink(event: RuntimeEvent) -> None:
        if event.kind == "started":
            runtime_state["live"] = True
        elif event.kind == "armed":
            runtime_state["armed"] = True
        elif event.kind == "disarmed":
            runtime_state["armed"] = False
        elif event.kind in ("stopping", "estop", "finished"):
            runtime_state["live"] = False
            runtime_state["armed"] = False
        elif event.kind == "frame":
            runtime_state["armed"] = bool(event.armed)

    active = threading.Event()
    active.set()

    def poller() -> None:
        started_at = time.monotonic()
        reported = False
        while active.is_set() and not session.stopped:
            if not reported and time.monotonic() - started_at >= 0.5:
                reported = True
                connected, text = monitor.status()
                if not connected:
                    _gamepad_log(
                        f"手柄未就绪：{text}；可用后自动连接（B 抢断/恢复，LS 移动，RS 旋转/俯仰）"
                    )
            teleop.poll(
                session,
                armed=runtime_state["armed"],
                live=runtime_state["live"],
                can_arm=runtime_state["live"] and not runtime_state["armed"],
                can_manual=runtime_state["live"] and not runtime_state["armed"],
                log=_gamepad_log,
            )
            time.sleep(0.03)

    monitor.start()
    thread = threading.Thread(target=poller, name="gamepad-teleop", daemon=True)
    thread.start()
    sink = fanout_event_sinks(unity_publisher, tracking_sink)
    return session, sink, thread, monitor, active


def _gamepad_log(message: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        print(f"[gamepad] {message}", file=sys.stderr)


def _mirror_log(message: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        print(f"[mirror] {message}", file=sys.stderr)


def build_unity_status_config(config: dict, args: argparse.Namespace) -> UnityStatusConfig:
    base = unity_status_config(config.get("unity"))
    enabled = base.enabled if args.unity_status is None else bool(args.unity_status)
    return UnityStatusConfig(
        enabled=enabled,
        host=args.unity_host or base.host,
        port=base.port if args.unity_port is None else args.unity_port,
        frame_rate_hz=base.frame_rate_hz,
        target_freshness_ms=base.target_freshness_ms,
        source=base.source,
        maximum_datagram_bytes=base.maximum_datagram_bytes,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = None
    unity_publisher = None
    session: SessionControl | None = None
    gamepad_monitor: GamepadMonitor | None = None
    gamepad_active = threading.Event()
    gamepad_thread: threading.Thread | None = None
    try:
        config = load_config(args.config)
        source_value = str(config["video"]["source"]) if args.source is None else args.source
        backend_name = str(config["robot"]["backend"]) if args.backend is None else args.backend
        robot_host = str(config["robot"]["host"]) if args.robot_host is None else args.robot_host
        robot_port = int(config["robot"]["port"]) if args.robot_port is None else args.robot_port
        effective_video = dict(config["video"], source=source_value)
        effective_robot = dict(
            config["robot"],
            backend=backend_name,
            host=robot_host,
            port=robot_port,
        )
        validate_video_config(effective_video)
        validate_robot_config(effective_robot)
        if args.approach_mode is not None:
            config["controller"] = dict(config["controller"], approach_mode=args.approach_mode)
        effective_unity = build_unity_status_config(config, args)
        detection_config = detector_config(config["detection"])
        if args.edge_model is not None:
            from .detector_profile import load_detector

            detector = load_detector(args.edge_model, detection_config)
        else:
            detector = RedBallDetector(detection_config)
        if args.check_config:
            print("config ok")
            return 0
        unity_publisher = UnityStatusPublisher(effective_unity)
        source = build_source(source_value, effective_video, args.realtime)
        try:
            if backend_name == "tcp":
                robot = TcpRobotClient(
                    robot_host,
                    robot_port,
                    float(effective_robot["connect_timeout_seconds"]),
                    float(effective_robot["send_interval_seconds"]),
                )
                robot.connect()
                if args.robot_mirror:
                    mirrors = []
                    for endpoint in args.robot_mirror:
                        mirror_host, mirror_port = parse_endpoint(endpoint)
                        if same_endpoint(robot_host, robot_port, (mirror_host, mirror_port)):
                            print("[mirror] 镜像地址与机器人相同，已忽略", file=sys.stderr)
                            continue
                        mirrors.append(
                            MirrorTcpRobot(
                                mirror_host, mirror_port, connect_timeout=1.0, on_status=_mirror_log
                            )
                        )
                    if mirrors:
                        robot = FanoutRobot(robot, tuple(mirrors))
            else:
                robot = RecordingRobot()
        except BaseException:
            source.close()
            raise
        event_sink = unity_publisher
        if args.gamepad:
            session, event_sink, gamepad_thread, gamepad_monitor, gamepad_active = (
                _start_gamepad_teleop(
                    unity_publisher,
                    armed_at_start=bool(args.arm),
                )
            )
        result = run_loop(
            source,
            detector,
            VisualApproachController(controller_config(config["controller"])),
            robot,
            armed=args.arm,
            max_frames=args.max_frames,
            frame_timeout_seconds=float(effective_video["frame_timeout_seconds"]),
            obstacle_policy=build_obstacle_policy(config, backend_name, args.no_obstacle),
            event_sink=event_sink,
            output_dir=args.output_dir,
            session=session,
        )
        states = ",".join(state.value for state in result.states_seen)
        print(
            f"frames={result.frames} final_state={result.final_state.value} "
            f"states={states} termination={result.termination}"
        )
        return (
            0 if result.final_state in (result.final_state.IDLE, result.final_state.ARRIVED) else 2
        )
    except KeyboardInterrupt:
        print("interrupted: cleanup sent best-effort stop commands", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports user-facing failures.
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        gamepad_active.clear()
        if gamepad_thread is not None:
            gamepad_thread.join(timeout=1.0)
        if gamepad_monitor is not None:
            gamepad_monitor.stop()
        if session is not None:
            session.request_stop()
        if unity_publisher is not None:
            unity_publisher.close()


if __name__ == "__main__":
    raise SystemExit(main())
