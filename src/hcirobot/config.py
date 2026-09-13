from __future__ import annotations

import dataclasses
import math
import tomllib
from pathlib import Path
from typing import Any

from .controller import ControllerConfig
from .detector import DetectorConfig
from .navigation import obstacle_config
from .unity_udp import UnityStatusConfig

_REQUIRED_SECTIONS = ("video", "detection", "controller", "robot")
_OPTIONAL_SECTIONS = ("obstacle", "unity")
_VIDEO_KEYS = {"source", "width", "height", "fps", "frame_timeout_seconds"}
_ROBOT_KEYS = {
    "backend",
    "host",
    "port",
    "connect_timeout_seconds",
    "send_interval_seconds",
}


def load_config(path: Path) -> dict[str, Any]:
    """Load and validate a complete application configuration.

    Configuration mistakes must fail before a camera or robot connection is opened;
    in particular, an unknown robot backend must never fall back to recording mode.
    """
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    if not isinstance(config, dict):
        raise TypeError("configuration root must be a table")

    allowed_sections = set(_REQUIRED_SECTIONS + _OPTIONAL_SECTIONS)
    _reject_unknown(config, allowed_sections, "configuration")
    for section in _REQUIRED_SECTIONS:
        _table(config, section, required=True)
    for section in _OPTIONAL_SECTIONS:
        if section in config:
            _table(config, section, required=False)

    validate_video_config(config["video"])
    detector_config(config["detection"])
    controller_config(config["controller"])
    validate_robot_config(config["robot"])
    obstacle_config(config.get("obstacle", {}))
    unity_status_config(config.get("unity"))
    return config


def validate_video_config(values: dict[str, Any]) -> None:
    _reject_unknown(values, _VIDEO_KEYS, "video")
    missing = _VIDEO_KEYS - values.keys()
    if missing:
        raise ValueError(f"missing video configuration keys: {', '.join(sorted(missing))}")
    source = values["source"]
    if not isinstance(source, (str, int)) or isinstance(source, bool):
        raise TypeError("video source must be a string or camera index")
    if isinstance(source, str) and not source.strip():
        raise ValueError("video source must not be empty")
    _positive_int(values["width"], "video width")
    _positive_int(values["height"], "video height")
    _positive_number(values["fps"], "video fps")
    _positive_number(values["frame_timeout_seconds"], "video frame timeout")


def validate_robot_config(values: dict[str, Any]) -> None:
    _reject_unknown(values, _ROBOT_KEYS, "robot")
    missing = _ROBOT_KEYS - values.keys()
    if missing:
        raise ValueError(f"missing robot configuration keys: {', '.join(sorted(missing))}")
    backend = values["backend"]
    if backend not in {"recording", "tcp"}:
        raise ValueError("robot backend must be 'recording' or 'tcp'")
    host = values["host"]
    if not isinstance(host, str) or not host.strip():
        raise ValueError("robot host must not be empty")
    port = values["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("robot port must be an integer between 1 and 65535")
    _positive_number(values["connect_timeout_seconds"], "robot connect timeout")
    _non_negative_number(values["send_interval_seconds"], "robot send interval")


def detector_config(values: dict[str, Any]) -> DetectorConfig:
    values = _dataclass_values(values, DetectorConfig, "detection")
    if "lab_min" in values:
        values["lab_min"] = tuple(values["lab_min"])
    if "lab_max" in values:
        values["lab_max"] = tuple(values["lab_max"])
    return DetectorConfig(**values)


def controller_config(values: dict[str, Any]) -> ControllerConfig:
    return ControllerConfig(**_dataclass_values(values, ControllerConfig, "controller"))


def unity_status_config(values: dict[str, Any] | None) -> UnityStatusConfig:
    if values is None:
        return UnityStatusConfig()
    if not isinstance(values, dict):
        raise TypeError("configuration section unity must be a table")
    config = UnityStatusConfig(**_dataclass_values(values, UnityStatusConfig, "unity"))
    if not isinstance(config.enabled, bool):
        raise TypeError("unity enabled must be a boolean")
    return config


def _table(config: dict[str, Any], section: str, *, required: bool) -> dict[str, Any]:
    if section not in config:
        if required:
            raise ValueError(f"missing configuration section: {section}")
        return {}
    values = config[section]
    if not isinstance(values, dict):
        raise TypeError(f"configuration section {section} must be a table")
    return values


def _dataclass_values(values: dict[str, Any], config_type: type, section: str) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise TypeError(f"configuration section {section} must be a table")
    known = {field.name for field in dataclasses.fields(config_type)}
    _reject_unknown(values, known, section)
    return dict(values)


def _reject_unknown(values: dict[str, Any], known: set[str], section: str) -> None:
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"unknown {section} configuration keys: {', '.join(sorted(unknown))}")


def _positive_int(value: Any, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _positive_number(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(float(value)) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")


def _non_negative_number(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{name} must be non-negative and finite")
