from __future__ import annotations

import tomllib
from pathlib import Path

from .controller import ControllerConfig
from .detector import DetectorConfig


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
