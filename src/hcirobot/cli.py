from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .app import run_loop
from .config import controller_config, detector_config, load_config
from .controller import VisualApproachController
from .detector import RedBallDetector
from .navigation import ObstaclePolicy, obstacle_config
from .robot import RecordingRobot, TcpRobotClient
from .video import MjpegHttpSource, OpenCvVideoSource, SyntheticBallSource, SyntheticConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TonyPi visual search-align-approach baseline")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--source", help="synthetic, camera index, video path, or MJPEG URL")
    parser.add_argument("--backend", choices=("recording", "tcp"))
    parser.add_argument("--robot-host")
    parser.add_argument("--robot-port", type=int)
    parser.add_argument("--arm", action="store_true", help="allow autonomous movement")
    parser.add_argument("--no-obstacle", action="store_true", help="disable reactive obstacle avoidance")
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--realtime", action="store_true", help="pace synthetic video at configured FPS")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = None
    try:
        config = load_config(args.config)
        source_value = args.source or str(config["video"]["source"])
        backend_name = args.backend or str(config["robot"]["backend"])
        source = build_source(source_value, config["video"], args.realtime)
        try:
            if backend_name == "tcp":
                robot = TcpRobotClient(
                    args.robot_host or str(config["robot"]["host"]),
                    args.robot_port or int(config["robot"]["port"]),
                    float(config["robot"]["connect_timeout_seconds"]),
                    float(config["robot"]["send_interval_seconds"]),
                )
                robot.connect()
            else:
                robot = RecordingRobot()
        except BaseException:
            source.close()
            raise
        result = run_loop(
            source,
            RedBallDetector(detector_config(config["detection"])),
            VisualApproachController(controller_config(config["controller"])),
            robot,
            armed=args.arm,
            max_frames=args.max_frames,
            frame_timeout_seconds=float(config["video"]["frame_timeout_seconds"]),
            obstacle_policy=build_obstacle_policy(config, backend_name, args.no_obstacle),
            output_dir=args.output_dir,
        )
        states = ",".join(state.value for state in result.states_seen)
        print(f"frames={result.frames} final_state={result.final_state.value} states={states}")
        return 0 if result.final_state in (result.final_state.IDLE, result.final_state.ARRIVED) else 2
    except KeyboardInterrupt:
        print("interrupted: cleanup sent best-effort stop commands", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports user-facing failures.
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
