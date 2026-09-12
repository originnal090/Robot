from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

from .controller import ControllerConfig
from .detector import DetectorConfig
from .unity_udp import UnityStatusConfig


def load_config(path: Path) -> dict:
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    for section in ("video", "detection", "controller", "robot"):
        if section not in config or not isinstance(config[section], dict):
            raise ValueError(f"missing configuration section: {section}")
    return config


def detector_config(values: dict) -> DetectorConfig:
    values = dict(values)
    values["lab_min"] = tuple(values["lab_min"])
    values["lab_max"] = tuple(values["lab_max"])
    return DetectorConfig(**values)


def controller_config(values: dict) -> ControllerConfig:
    return ControllerConfig(**values)


def unity_status_config(values: dict | None) -> UnityStatusConfig:
    values = values or {}
    if not isinstance(values, dict):
        raise TypeError("configuration section unity must be a table")
    known = {
        field.name: values[field.name]
        for field in dataclasses.fields(UnityStatusConfig)
        if field.name in values
    }
    return UnityStatusConfig(**known)
