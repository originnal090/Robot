from __future__ import annotations

import json

from hcirobot.app import RuntimeEvent
from hcirobot.cli import build_parser, build_unity_status_config
from hcirobot.config import unity_status_config
from hcirobot.controller import ControlDecision
from hcirobot.model import ControlState, Detection, RobotCommand
from hcirobot.telemetry import AutonomyStatusProjector, encode_autonomy_status


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def frame_event(detection: Detection, *, output_v: float = 0.25) -> RuntimeEvent:
    decision = ControlDecision(
        ControlState.APPROACHING,
        RobotCommand(velocity=0.25),
        "approaching",
        horizontal_error=0.0,
        radius_ratio=0.1,
    )
    return RuntimeEvent(
        "frame",
        "approaching",
        detection=detection,
        decision=decision,
        frame_count=7,
        output_v=output_v,
        output_source="autonomy",
        armed=True,
        obstacle_state="CLEAR",
        distance_mm=812.0,
        avoid_count=1,
    )


def test_schema_v1_projection_has_stable_required_shape() -> None:
    clock = Clock()
    projector = AutonomyStatusProjector(
        "12345678-1234-5678-1234-567812345678",
        monotonic=clock,
        wall_clock_ms=lambda: 1_700_000_000_123,
    )
    detection = Detection(True, True, 320.0, 240.0, 48.0, 640, 480)
    status = projector.project(frame_event(detection), seq=9)

    assert status["type"] == "autonomy_status"
    assert status["schema_version"] == 1
    assert status["source"] == "hcirobot"
    assert status["session_id"] == "12345678-1234-5678-1234-567812345678"
    assert status["seq"] == 9
    assert status["sent_at_ms"] == 1_700_000_000_123
    assert status["mode"] == "autonomy"
    assert status["control_state"] == "APPROACHING"
    assert status["video_status"] == "streaming"
    assert status["target"] == {
        "candidate": True,
        "confirmed": True,
        "fresh": True,
        "age_ms": 0,
        "center_x": 320.0,
        "center_y": 240.0,
        "radius": 48.0,
        "frame_width": 640,
        "frame_height": 480,
    }
    assert status["output"] == {"v": 0.25, "steer": 0.0, "grab": False, "source": "autonomy"}
    assert status["obstacle"] == {"state": "CLEAR", "distance_mm": 812.0, "avoid_count": 1}
    assert status["fault"] is None
    assert status["termination"] is None


def test_target_freshness_expires_on_non_frame_status() -> None:
    clock = Clock()
    projector = AutonomyStatusProjector(target_freshness_ms=500, monotonic=clock)
    detection = Detection(True, True, 100.0, 80.0, 20.0, 320, 240)
    projector.project(frame_event(detection), seq=1, sent_at_ms=100)
    clock.value = 0.501

    status = projector.project(
        RuntimeEvent("state", "changed", output_source="autonomy", armed=True),
        seq=2,
        sent_at_ms=101,
    )
    assert status["target"]["confirmed"] is True
    assert status["target"]["fresh"] is False
    assert status["target"]["age_ms"] == 501


def test_fault_estop_and_termination_are_latched_into_status() -> None:
    projector = AutonomyStatusProjector()
    estop = projector.project(RuntimeEvent("estop", "operator", output_source="estop"), seq=1)
    assert estop["estop"] is True
    assert estop["control_state"] == "LOST_SAFE"

    fault = projector.project(RuntimeEvent("error", "connection lost"), seq=2)
    assert fault["fault"] == "connection lost"

    finished = projector.project(RuntimeEvent("finished", "robot_connection_lost"), seq=3)
    assert finished["termination"] == "robot_connection_lost"
    assert finished["video_status"] == "ended"

    arrived_projector = AutonomyStatusProjector()
    arrived_projector.project(
        RuntimeEvent(
            "frame",
            decision=ControlDecision(ControlState.ARRIVED, RobotCommand.stop(), "arrived"),
            output_source="autonomy",
            armed=True,
        ),
        seq=1,
    )
    arrived = arrived_projector.project(RuntimeEvent("finished", "arrived"), seq=2)
    assert arrived["control_state"] == "ARRIVED"


def test_missing_target_and_distance_use_unity_safe_numeric_sentinels() -> None:
    projector = AutonomyStatusProjector()
    status = projector.project(RuntimeEvent("started", "session started"), seq=1)

    assert status["target"] == {
        "candidate": False,
        "confirmed": False,
        "fresh": False,
        "age_ms": -1,
        "center_x": -1.0,
        "center_y": -1.0,
        "radius": -1.0,
        "frame_width": 0,
        "frame_height": 0,
    }
    assert status["obstacle"]["distance_mm"] == -1.0
    assert status["distance_mm"] == -1.0


def test_encoder_replaces_non_finite_values_and_forbids_nan_tokens() -> None:
    payload = {"type": "autonomy_status", "value": float("nan"), "nested": [float("inf"), 1.0]}
    encoded = encode_autonomy_status(payload)
    text = encoded.decode("utf-8")
    assert "NaN" not in text
    assert "Infinity" not in text
    assert json.loads(text) == {
        "nested": [None, 1.0],
        "type": "autonomy_status",
        "value": None,
    }


def test_unity_config_defaults_disabled_and_cli_overrides_are_explicit() -> None:
    assert unity_status_config(None).enabled is False
    parser = build_parser()
    config = {"unity": {"enabled": True, "host": "sim.local", "port": 7000}}

    inherited = build_unity_status_config(config, parser.parse_args([]))
    assert (inherited.enabled, inherited.host, inherited.port) == (True, "sim.local", 7000)

    overridden = build_unity_status_config(
        config,
        parser.parse_args(["--no-unity-status", "--unity-host", "127.0.0.2", "--unity-port", "6109"]),
    )
    assert (overridden.enabled, overridden.host, overridden.port) == (False, "127.0.0.2", 6109)

    enabled = build_unity_status_config({}, parser.parse_args(["--unity-status"]))
    assert enabled.enabled is True
