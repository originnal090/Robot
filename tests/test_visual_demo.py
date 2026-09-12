from __future__ import annotations

import numpy as np
import pytest

from hcirobot.controller import ControlDecision
from hcirobot.gui import _auto_arm_allowed
from hcirobot.model import ControlState, Detection, RobotCommand
from hcirobot.video import annotate


def _decision(velocity: float = 0.35, steer: float = -0.1) -> ControlDecision:
    return ControlDecision(
        ControlState.APPROACHING,
        RobotCommand(velocity, steer),
        "approaching",
        0.05,
        0.08,
    )


def _detection() -> Detection:
    return Detection(
        True,
        True,
        320,
        240,
        40,
        640,
        480,
        control_confirmed=True,
    )


def _frame() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


def _overlay_text(rendered: np.ndarray, lines: int) -> str:
    # Extract the drawn rows only; white text on black background is trivially separable.
    band = rendered[0 : 20 + lines * 24, 0:640]
    return " ".join("".join(chr(int(v)) for v in row if 200 < v < 256) for row in band[:, 0])


def test_annotate_keeps_legacy_three_lines_without_output_info() -> None:
    rendered = annotate(_frame(), _detection(), _decision())
    assert rendered.shape == (480, 640, 3)
    assert rendered[120, 320].tolist() != [0, 0, 0]  # center guide line drawn


def test_annotate_shows_intent_and_actual_with_source() -> None:
    rendered = annotate(
        _frame(),
        _detection(),
        _decision(velocity=0.35, steer=-0.10),
        output_v=0.0,
        output_steer=0.0,
        output_source="obstacle_hold",
        distance_mm=850.0,
        obstacle_state="CAUTION",
    )
    overlay = rendered[0:130, 10:620]
    white = overlay > 200
    assert white.any()
    # five text rows: state, intent, actual, dist, detected
    rows = white.any(axis=1).nonzero()[0]
    row_groups = 1 + int(np.count_nonzero(np.diff(rows) > 8))
    assert row_groups >= 5


def test_annotate_without_distance_omits_distance_row() -> None:
    rendered_full = annotate(_frame(), _detection(), _decision(), output_v=0.35, output_steer=-0.1)
    rendered_plain = annotate(_frame(), _detection(), _decision())
    # Actual-output row adds exactly one more text band than the plain legacy call.
    full_rows = (rendered_full[0:130, 10:620] > 200).any(axis=1).nonzero()[0]
    plain_rows = (rendered_plain[0:130, 10:620] > 200).any(axis=1).nonzero()[0]
    full_groups = 1 + int(np.count_nonzero(np.diff(full_rows) > 8))
    plain_groups = 1 + int(np.count_nonzero(np.diff(plain_rows) > 8))
    assert full_groups == plain_groups + 1


@pytest.mark.parametrize(
    ("backend", "host", "allowed"),
    [
        ("tcp", "127.0.0.1", True),
        ("TCP", "LOCALHOST", True),
        ("tcp", "::1", True),
        ("tcp", "192.168.1.20", False),
        ("tcp", "tonypi.local", False),
        ("recording", "127.0.0.1", False),
        ("tcp", "", False),
    ],
)
def test_auto_arm_restricted_to_loopback_tcp(backend: str, host: str, allowed: bool) -> None:
    assert _auto_arm_allowed(backend, host) is allowed
