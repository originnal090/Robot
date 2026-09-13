#!/usr/bin/env python3
"""One-click Unity<->Python visual demo: launch Unity Play Mode, then the annotated GUI.

Steps performed:
1. If the simulator endpoints are not up yet, start the Unity editor (windowed)
   on the demo project with an executeMethod that opens the Quick Start scene
   and enters Play Mode.
2. Wait until the MJPEG (8080) and TCP (5075) endpoints answer.
3. Start ``hcirobot-gui --config <config> --demo`` so the Python console shows
   the annotated Unity camera with ball detection, intent vs actual output and
   the obstacle distance, and auto-arms (loopback TCP only).
"""

from __future__ import annotations

import argparse
import math
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UNITY = Path(r"D:\Program Files\Unity\Hub\Editor\2022.3.62f3c1\Editor\Unity.exe")
# Tracked demo project with the full Quick Start scene; the launcher method
# itself lives in the simulator package so any project importing it works here.
DEFAULT_PROJECT = REPO_ROOT / "unity" / "demo-project"
LAUNCH_METHOD = "HciRobot.Simulator.Editor.DemoLauncher.LaunchDemo"
REBUILD_METHOD = "HciRobot.Simulator.Editor.DemoLauncher.RebuildAndPlay"


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_endpoints(host: str, tcp_port: int, http_port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # TCP accept is enough: Unity listens on both once Play Mode services boot.
        if port_open(host, tcp_port) and port_open(host, http_port):
            return True
        time.sleep(1.0)
    return False


def launch_unity(unity: Path, project: Path, rebuild_scene: bool) -> subprocess.Popen[bytes]:
    if not unity.is_file():
        raise SystemExit(f"Unity editor not found: {unity}")
    if not (project / "ProjectSettings" / "ProjectVersion.txt").is_file():
        raise SystemExit(
            f"Not a Unity project: {project} "
            "(the default is unity/demo-project; see unity/README.md to build your own)"
        )
    command = [
        str(unity),
        "-projectPath",
        str(project),
        "-executeMethod",
        REBUILD_METHOD if rebuild_scene else LAUNCH_METHOD,
    ]
    print(f"[demo] starting Unity: {' '.join(command)}")
    return subprocess.Popen(command)


def launch_gui(config: Path, auto_arm: bool) -> subprocess.Popen[bytes]:
    arguments = [sys.executable, "-m", "hcirobot.gui", "--config", str(config)]
    if auto_arm:
        arguments.append("--demo")
    print(f"[demo] starting GUI: {' '.join(arguments)}")
    return subprocess.Popen(arguments, cwd=str(REPO_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unity", type=Path, default=DEFAULT_UNITY, help="Unity.exe path")
    parser.add_argument(
        "--project", type=Path, default=DEFAULT_PROJECT, help="Unity project folder"
    )
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.unity.toml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--tcp-port", type=int, default=5075)
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument(
        "--rebuild-scene",
        action="store_true",
        help="discard the saved Quick Start scene and rebuild it from the current package "
        "(wipes manual obstacle edits in the demo project)",
    )
    parser.add_argument("--unity-only", action="store_true", help="only start Unity Play Mode")
    parser.add_argument("--gui-only", action="store_true", help="only start the Python GUI")
    parser.add_argument(
        "--no-autoarm",
        action="store_true",
        help="start the GUI in preview mode without --demo auto-arming",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not 1 <= args.tcp_port <= 65535 or not 1 <= args.http_port <= 65535:
        raise ValueError("ports must be in 1..65535")
    if not math.isfinite(args.startup_timeout) or args.startup_timeout <= 0:
        raise ValueError("--startup-timeout must be > 0")
    if not args.config.is_file():
        raise ValueError(f"config file not found: {args.config}")


def run(args: argparse.Namespace) -> int:
    try:
        validate_args(args)
    except ValueError as exc:
        print(f"[demo] invalid arguments: {exc}", file=sys.stderr)
        return 2

    if not args.gui_only and not port_open(args.host, args.tcp_port):
        if port_open(args.host, args.http_port):
            print("[demo] MJPEG is up but TCP is not; is another client holding 5075?")
            return 1
        launch_unity(args.unity, args.project, rebuild_scene=args.rebuild_scene)
        if not wait_for_endpoints(args.host, args.tcp_port, args.http_port, args.startup_timeout):
            print("[demo] Unity endpoints did not become ready in time", file=sys.stderr)
            return 1
        print(f"[demo] Unity endpoints ready on {args.host} ({args.tcp_port}/{args.http_port})")
    else:
        print("[demo] Unity simulator already running; skipping editor launch")

    if args.unity_only:
        return 0

    gui_process = launch_gui(args.config, auto_arm=not args.no_autoarm)
    try:
        return gui_process.wait()
    except KeyboardInterrupt:
        gui_process.terminate()
        return 130


def main() -> None:
    raise SystemExit(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
