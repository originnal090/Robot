#!/usr/bin/env python3
"""Read-only Orange Pi deployment preflight for HCIRobot."""

from __future__ import annotations

import argparse
import platform
import socket
import sys
from pathlib import Path

import cv2
import numpy
from PIL import Image

from hcirobot.cli import build_source
from hcirobot.config import load_config
from hcirobot.robot import TcpRobotClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate an Orange Pi HCIRobot installation")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--probe-video", action="store_true", help="open the configured source and decode one frame")
    parser.add_argument(
        "--probe-robot",
        action="store_true",
        help="connect to the robot and send a stop frame; TonyPi may execute its stand action",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        print(f"python={platform.python_version()} executable={sys.executable}")
        print(f"platform={platform.platform()} machine={platform.machine()}")
        print(f"libc={platform.libc_ver()}")
        print(f"numpy={numpy.__version__} opencv={cv2.__version__} pillow={Image.__version__}")
        print(f"config={args.config} backend={config['robot']['backend']} source={config['video']['source']}")

        if args.probe_video:
            source = build_source(str(config["video"]["source"]), config["video"], realtime=False)
            try:
                frame = next(iter(source))
                print(f"video=ok shape={tuple(frame.shape)}")
            finally:
                source.close()

        if args.probe_robot:
            robot_config = config["robot"]
            if robot_config["backend"] != "tcp":
                raise ValueError("--probe-robot requires robot.backend='tcp'")
            robot = TcpRobotClient(
                str(robot_config["host"]),
                int(robot_config["port"]),
                float(robot_config["connect_timeout_seconds"]),
                0.0,
            )
            try:
                robot.connect()
                robot.close()
            finally:
                robot.cancel()
            print("robot=ok stop frame sent; remote stand action may have run")
        else:
            host = str(config["robot"]["host"])
            try:
                socket.getaddrinfo(host, int(config["robot"]["port"]), socket.AF_INET, socket.SOCK_STREAM)
            except OSError as exc:
                raise ValueError(f"robot host cannot be resolved: {host}: {exc}") from exc
            print("robot=not-probed (use --probe-robot only with the robot safely supported)")
    except Exception as exc:  # noqa: BLE001 - command boundary reports actionable failure.
        print(f"preflight failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("preflight=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
