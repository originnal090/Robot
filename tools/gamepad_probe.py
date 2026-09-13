#!/usr/bin/env python3
"""Live gamepad diagnostic: print axes, buttons and the mapped v/steer command.

Wiggle the sticks and press buttons while this runs; the numbers are exactly
what the teleop stack sees (same backend, same deadzone mapping).  Use it to
separate "hardware/driver gives no input" from "the app ignores input":

    uv run python tools/gamepad_probe.py            # watch 20 s
    uv run python tools/gamepad_probe.py --list     # just enumerate devices

If raw axes stay 0.000 while you move the stick, the problem is below this
program (driver/mode); if they move here but the robot does not, the problem
is in the app-side wiring (mode gating, session state).
"""

from __future__ import annotations

import argparse
import sys
import time

from hcirobot.gamepad import (
    DEADZONE,
    TOGGLE_BUTTON,
    CompositeGamepadBackend,
    PygameGamepadBackend,
    XInputGamepadBackend,
    map_to_command,
)


def list_devices() -> int:
    import pygame

    pygame.joystick.init()
    count = pygame.joystick.get_count()
    if count == 0:
        print("no gamepad detected (is the dongle paired/powered on?)")
        return 1
    for index in range(count):
        stick = pygame.joystick.Joystick(index)
        stick.init()
        print(f"[{index}] {stick.get_name()}  axes={stick.get_numaxes()} buttons={stick.get_numbuttons()}")
    return 0


def build_backend(choice: str):
    if choice == "xinput":
        return XInputGamepadBackend()
    if choice == "pygame":
        return PygameGamepadBackend()
    if sys.platform == "win32":
        return CompositeGamepadBackend([XInputGamepadBackend(), PygameGamepadBackend()])
    return PygameGamepadBackend()


def probe(seconds: float, deadzone: float, backend_choice: str) -> int:
    backend = build_backend(backend_choice)
    print(f"backend: {backend_choice}")
    name = backend.probe()
    if name is None:
        print(f"no gamepad: {backend.last_error}")
        return 1
    print(f"connected: {name}")
    print(f"deadzone={deadzone:.2f}  B(toggle)=button {TOGGLE_BUTTON}")
    print("wiggle sticks / press buttons... (Ctrl+C exits early)")
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            reading = backend.read()
            velocity, steer = map_to_command(reading.drive_axis, reading.steer_axis, deadzone)
            buttons = " ".join(
                f"{index}{'*' if index == TOGGLE_BUTTON else ''}" for index in sorted(reading.buttons)
            )
            print(
                f"\rraw={reading.drive_axis:+.3f}/{reading.steer_axis:+.3f} "
                f"-> v={velocity:+.3f} steer={steer:+.3f}  buttons=[{buttons or '-'}]   ",
                end="",
                flush=True,
            )
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=20.0, help="how long to watch (default 20)")
    parser.add_argument("--deadzone", type=float, default=DEADZONE)
    parser.add_argument(
        "--backend",
        choices=("auto", "xinput", "pygame"),
        default="auto",
        help="input path to test (auto = XInput then pygame on Windows)",
    )
    parser.add_argument("--list", action="store_true", help="enumerate devices and exit")
    args = parser.parse_args(argv)
    if args.list:
        return list_devices()
    return probe(args.seconds, args.deadzone, args.backend)


if __name__ == "__main__":
    raise SystemExit(main())
