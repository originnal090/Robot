#!/usr/bin/env python3
# ruff: noqa: BLE001, UP006, UP031, UP035, UP041, UP045, TRY004
"""TonyPi robot-side TCP service compatible with the course JSONL/CMD protocol.

Sonar/DIST telemetry is deliberately disabled for this deployment. Runtime mode
is explicit: hardware mode fails closed when actuator SDKs/APIs are unavailable;
dry-run mode only prints actuator operations.
"""

import argparse
import json
import math
import os
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# Defaults. Environment and CLI overrides are parsed once by load_config().
HOST = "0.0.0.0"
PORT = 5075
UDP_COLOR_HOST = "127.0.0.1"
UDP_COLOR_PORT = 6001
DIST_INTERVAL_S = 0.30

DEADZONE = 0.20
WATCHDOG_S = 0.60
STEP_INTERVAL_S = 0.30
ON_STAND_ONCE = True
CMD_COOLDOWN_S = 0.50
PRINT_FWD = True

ACTION_FORWARD = "go_forward"
ACTION_BACK = "back"
ACTION_TURN_R = "turn_right"
ACTION_TURN_L = "turn_left"
ACTION_STAND = "stand"

# Speed tiers (|v| / |steer|): deadzone < |x| <= TIER_SLOW_MAX -> *_slow groups,
# <= TIER_FAST_MIN -> normal groups, above -> *_fast groups.  The tiered group
# names ship in the vendor ActionGroups library (TonyPi/ActionGroups/*.d6a).
TIER_SLOW_MAX = 0.45
TIER_FAST_MIN = 0.75
ACTION_FORWARD_SLOW = "go_forward_one_step"
ACTION_FORWARD_FAST = "go_forward_fast"
ACTION_BACK_SLOW = "back_one_step"
ACTION_BACK_FAST = "back_fast"
ACTION_TURN_R_SLOW = "turn_right_small_step"
ACTION_TURN_R_FAST = "turn_right_fast"
ACTION_TURN_L_SLOW = "turn_left_small_step"
ACTION_TURN_L_FAST = "turn_left_fast"
ACTION_LATERAL_R = "right_move"
ACTION_LATERAL_L = "left_move"
ACTION_LATERAL_R_SLOW = "right_move_10"
ACTION_LATERAL_L_SLOW = "left_move_10"
ACTION_LATERAL_R_FAST = "right_move_fast"
ACTION_LATERAL_L_FAST = "left_move_fast"

CMD_MAP = {
    "right_grip": "outfire",
    "right_trigger": "stand_up_back",
    "left_trigger": "stand_up_front",
}

HEAD_PITCH_ID = 1
HEAD_YAW_ID = 2
BOARD_PULSE_MIN = 500
BOARD_PULSE_MAX = 2500
PULSE_MIN = 500
PULSE_MAX = 2500
PITCH_CENTER = 1500
PITCH_MIN = 1000
PITCH_MAX = 2000
YAW_CENTER = 1500
YAW_MIN = 500
YAW_MAX = 2500
NOD_AMPLITUDE = 150
SHAKE_AMPLITUDE = 200
HEAD_STEP_MS = 250
BOARD_USE_TIME_MIN_MS = 20
BOARD_USE_TIME_MAX_MS = 30000

SONAR_MAX_VALID_MM = 5000
SONAR_DISCONNECTED_SENTINEL = 99999
SONAR_FAILURE_LIMIT = 3
SONAR_WARN_INTERVAL_S = 5.0

MODE_HARDWARE = "hardware"
MODE_DRY_RUN = "dry-run"
_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# Kept as injectable compatibility points for focused tests and direct imports.
BOARD = None  # type: Optional[ModuleType]
AGC = None  # type: Optional[ModuleType]
SONAR_MODULE = None  # type: Optional[ModuleType]


class ConfigError(ValueError):
    """Raised when startup configuration is invalid."""


class StartupError(RuntimeError):
    """Raised when required runtime dependencies are unavailable."""


class ActuatorError(RuntimeError):
    """Raised when a hardware actuator operation fails."""


@dataclass(frozen=True)
class ServiceConfig:
    mode: str = MODE_HARDWARE
    host: str = HOST
    port: int = PORT
    udp_color_host: str = UDP_COLOR_HOST
    udp_color_port: int = UDP_COLOR_PORT
    dist_interval_s: float = DIST_INTERVAL_S
    deadzone: float = DEADZONE
    watchdog_s: float = WATCHDOG_S
    step_interval_s: float = STEP_INTERVAL_S
    cmd_cooldown_s: float = CMD_COOLDOWN_S
    on_stand_once: bool = ON_STAND_ONCE
    print_fwd: bool = PRINT_FWD
    allow_cmd_while_moving: bool = False
    action_group_dir: str = ""
    action_forward: str = ACTION_FORWARD
    action_back: str = ACTION_BACK
    action_turn_r: str = ACTION_TURN_R
    action_turn_l: str = ACTION_TURN_L
    tier_slow_max: float = TIER_SLOW_MAX
    tier_fast_min: float = TIER_FAST_MIN
    action_forward_slow: str = ACTION_FORWARD_SLOW
    action_forward_fast: str = ACTION_FORWARD_FAST
    action_back_slow: str = ACTION_BACK_SLOW
    action_back_fast: str = ACTION_BACK_FAST
    action_turn_r_slow: str = ACTION_TURN_R_SLOW
    action_turn_r_fast: str = ACTION_TURN_R_FAST
    action_turn_l_slow: str = ACTION_TURN_L_SLOW
    action_turn_l_fast: str = ACTION_TURN_L_FAST
    action_stand: str = ACTION_STAND
    action_lateral_r: str = ACTION_LATERAL_R
    action_lateral_l: str = ACTION_LATERAL_L
    action_lateral_r_slow: str = ACTION_LATERAL_R_SLOW
    action_lateral_l_slow: str = ACTION_LATERAL_L_SLOW
    action_lateral_r_fast: str = ACTION_LATERAL_R_FAST
    action_lateral_l_fast: str = ACTION_LATERAL_L_FAST
    head_pitch_id: int = HEAD_PITCH_ID
    head_yaw_id: int = HEAD_YAW_ID
    pulse_min: int = PULSE_MIN
    pulse_max: int = PULSE_MAX
    pitch_center: int = PITCH_CENTER
    pitch_min: int = PITCH_MIN
    pitch_max: int = PITCH_MAX
    yaw_center: int = YAW_CENTER
    yaw_min: int = YAW_MIN
    yaw_max: int = YAW_MAX
    nod_amplitude: int = NOD_AMPLITUDE
    shake_amplitude: int = SHAKE_AMPLITUDE
    head_step_ms: int = HEAD_STEP_MS
    sonar_sim: str = ""
    require_sonar: bool = False
    allow_sonar_sim_in_hardware: bool = False
    sonar_failure_limit: int = SONAR_FAILURE_LIMIT
    sonar_warn_interval_s: float = SONAR_WARN_INTERVAL_S
    sonar_max_valid_mm: int = SONAR_MAX_VALID_MM
    sonar_disconnected_sentinel: int = SONAR_DISCONNECTED_SENTINEL


@dataclass
class RuntimeHardware:
    board: Optional[ModuleType] = None
    agc: Optional[ModuleType] = None
    sonar: Optional[Any] = None


def _parse_bool(name: str, value: str) -> bool:
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ConfigError("%s must be one of 1/0, true/false, yes/no, on/off" % name)


def _env_value(
    environ: Mapping[str, str],
    name: str,
    default: Any,
    cast: Callable[[str], Any],
) -> Any:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("invalid %s=%r: %s" % (name, raw, exc))


def _option(namespace: Optional[argparse.Namespace], name: str, fallback: Any) -> Any:
    if namespace is None:
        return fallback
    value = getattr(namespace, name, None)
    return fallback if value is None else value


def _env_alias(
    environ: Mapping[str, str],
    primary: str,
    legacy: str,
    default: Any,
    cast: Callable[[str], Any],
) -> Any:
    primary_raw = environ.get(primary)
    legacy_raw = environ.get(legacy)
    if primary_raw is not None and legacy_raw is not None:
        primary_value = _env_value(environ, primary, default, cast)
        legacy_value = _env_value(environ, legacy, default, cast)
        if primary_value != legacy_value:
            raise ConfigError("%s conflicts with legacy %s" % (primary, legacy))
        return primary_value
    if primary_raw is not None:
        return _env_value(environ, primary, default, cast)
    if legacy_raw is not None:
        return _env_value(environ, legacy, default, cast)
    return default


def _resolve_mode(namespace: Optional[argparse.Namespace], environ: Mapping[str, str]) -> str:
    cli_mode = getattr(namespace, "mode", None) if namespace is not None else None
    env_mode = environ.get("TONYPI_MODE", "").strip().lower()
    legacy_raw = environ.get("TONYPI_DRY_RUN")
    legacy_mode = None
    if legacy_raw is not None:
        legacy_mode = MODE_DRY_RUN if _parse_bool("TONYPI_DRY_RUN", legacy_raw) else MODE_HARDWARE

    if cli_mode is not None:
        return cli_mode
    if env_mode:
        if env_mode not in (MODE_HARDWARE, MODE_DRY_RUN):
            raise ConfigError("TONYPI_MODE must be hardware or dry-run")
        if legacy_mode is not None and legacy_mode != env_mode:
            raise ConfigError("TONYPI_MODE conflicts with legacy TONYPI_DRY_RUN")
        return env_mode
    if legacy_mode is not None:
        return legacy_mode
    return MODE_HARDWARE


def load_config(
    namespace: Optional[argparse.Namespace] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ServiceConfig:
    """Load all robot-side settings once, then validate ranges and combinations."""
    env = os.environ if environ is None else environ

    def value(attr: str, env_name: str, default: Any, cast: Callable[[str], Any]) -> Any:
        return _option(namespace, attr, _env_value(env, env_name, default, cast))

    def boolean(attr: str, env_name: str, default: bool) -> bool:
        return value(attr, env_name, default, lambda raw: _parse_bool(env_name, raw))

    config = ServiceConfig(
        mode=_resolve_mode(namespace, env),
        host=value("host", "TONYPI_HOST", HOST, str),
        port=value("port", "TONYPI_PORT", PORT, int),
        udp_color_host=value("udp_color_host", "TONYPI_UDP_COLOR_HOST", UDP_COLOR_HOST, str),
        udp_color_port=value("udp_color_port", "TONYPI_UDP_COLOR_PORT", UDP_COLOR_PORT, int),
        dist_interval_s=value("dist_interval_s", "TONYPI_DIST_INTERVAL", DIST_INTERVAL_S, float),
        deadzone=value("deadzone", "TONYPI_DEADZONE", DEADZONE, float),
        watchdog_s=value("watchdog_s", "TONYPI_WATCHDOG", WATCHDOG_S, float),
        step_interval_s=value("step_interval_s", "TONYPI_STEP_INTERVAL", STEP_INTERVAL_S, float),
        cmd_cooldown_s=value("cmd_cooldown_s", "TONYPI_CMD_COOLDOWN", CMD_COOLDOWN_S, float),
        on_stand_once=boolean("on_stand_once", "TONYPI_ON_STAND_ONCE", ON_STAND_ONCE),
        print_fwd=boolean("print_fwd", "TONYPI_PRINT_FWD", PRINT_FWD),
        allow_cmd_while_moving=boolean(
            "allow_cmd_while_moving", "TONYPI_ALLOW_CMD_WHILE_MOVING", False
        ),
        action_group_dir=_option(
            namespace,
            "action_group_dir",
            _env_alias(
                env,
                "TONYPI_ACTION_GROUP_CHECK_DIR",
                "TONYPI_ACTION_GROUP_DIR",
                "",
                str,
            ),
        ).strip(),
        action_forward=value("action_forward", "TONYPI_ACTION_FORWARD", ACTION_FORWARD, str),
        action_back=value("action_back", "TONYPI_ACTION_BACK", ACTION_BACK, str),
        action_turn_r=_option(
            namespace,
            "action_turn_r",
            _env_alias(
                env,
                "TONYPI_ACTION_TURN_RIGHT",
                "TONYPI_ACTION_TURN_R",
                ACTION_TURN_R,
                str,
            ),
        ),
        action_turn_l=_option(
            namespace,
            "action_turn_l",
            _env_alias(
                env,
                "TONYPI_ACTION_TURN_LEFT",
                "TONYPI_ACTION_TURN_L",
                ACTION_TURN_L,
                str,
            ),
        ),
        tier_slow_max=value("tier_slow_max", "TONYPI_TIER_SLOW_MAX", TIER_SLOW_MAX, float),
        tier_fast_min=value("tier_fast_min", "TONYPI_TIER_FAST_MIN", TIER_FAST_MIN, float),
        action_forward_slow=value(
            "action_forward_slow", "TONYPI_ACTION_FORWARD_SLOW", ACTION_FORWARD_SLOW, str
        ),
        action_forward_fast=value(
            "action_forward_fast", "TONYPI_ACTION_FORWARD_FAST", ACTION_FORWARD_FAST, str
        ),
        action_back_slow=value(
            "action_back_slow", "TONYPI_ACTION_BACK_SLOW", ACTION_BACK_SLOW, str
        ),
        action_back_fast=value(
            "action_back_fast", "TONYPI_ACTION_BACK_FAST", ACTION_BACK_FAST, str
        ),
        action_turn_r_slow=value(
            "action_turn_r_slow", "TONYPI_ACTION_TURN_R_SLOW", ACTION_TURN_R_SLOW, str
        ),
        action_turn_r_fast=value(
            "action_turn_r_fast", "TONYPI_ACTION_TURN_R_FAST", ACTION_TURN_R_FAST, str
        ),
        action_turn_l_slow=value(
            "action_turn_l_slow", "TONYPI_ACTION_TURN_L_SLOW", ACTION_TURN_L_SLOW, str
        ),
        action_turn_l_fast=value(
            "action_turn_l_fast", "TONYPI_ACTION_TURN_L_FAST", ACTION_TURN_L_FAST, str
        ),
        action_stand=value("action_stand", "TONYPI_ACTION_STAND", ACTION_STAND, str),
        action_lateral_r=value(
            "action_lateral_r", "TONYPI_ACTION_LATERAL_R", ACTION_LATERAL_R, str
        ),
        action_lateral_l=value(
            "action_lateral_l", "TONYPI_ACTION_LATERAL_L", ACTION_LATERAL_L, str
        ),
        action_lateral_r_slow=value(
            "action_lateral_r_slow", "TONYPI_ACTION_LATERAL_R_SLOW", ACTION_LATERAL_R_SLOW, str
        ),
        action_lateral_l_slow=value(
            "action_lateral_l_slow", "TONYPI_ACTION_LATERAL_L_SLOW", ACTION_LATERAL_L_SLOW, str
        ),
        action_lateral_r_fast=value(
            "action_lateral_r_fast", "TONYPI_ACTION_LATERAL_R_FAST", ACTION_LATERAL_R_FAST, str
        ),
        action_lateral_l_fast=value(
            "action_lateral_l_fast", "TONYPI_ACTION_LATERAL_L_FAST", ACTION_LATERAL_L_FAST, str
        ),
        head_pitch_id=value("head_pitch_id", "TONYPI_HEAD_PITCH_ID", HEAD_PITCH_ID, int),
        head_yaw_id=value("head_yaw_id", "TONYPI_HEAD_YAW_ID", HEAD_YAW_ID, int),
        pulse_min=value("pulse_min", "TONYPI_PULSE_MIN", PULSE_MIN, int),
        pulse_max=value("pulse_max", "TONYPI_PULSE_MAX", PULSE_MAX, int),
        pitch_center=value("pitch_center", "TONYPI_PITCH_CENTER", PITCH_CENTER, int),
        pitch_min=value("pitch_min", "TONYPI_PITCH_MIN", PITCH_MIN, int),
        pitch_max=value("pitch_max", "TONYPI_PITCH_MAX", PITCH_MAX, int),
        yaw_center=value("yaw_center", "TONYPI_YAW_CENTER", YAW_CENTER, int),
        yaw_min=value("yaw_min", "TONYPI_YAW_MIN", YAW_MIN, int),
        yaw_max=value("yaw_max", "TONYPI_YAW_MAX", YAW_MAX, int),
        nod_amplitude=value("nod_amplitude", "TONYPI_NOD_AMPLITUDE", NOD_AMPLITUDE, int),
        shake_amplitude=value("shake_amplitude", "TONYPI_SHAKE_AMPLITUDE", SHAKE_AMPLITUDE, int),
        head_step_ms=value("head_step_ms", "TONYPI_HEAD_STEP_MS", HEAD_STEP_MS, int),
        sonar_sim=value("sonar_sim", "TONYPI_SONAR_SIM", "", str).strip(),
        require_sonar=boolean("require_sonar", "TONYPI_REQUIRE_SONAR", False),
        allow_sonar_sim_in_hardware=_option(
            namespace,
            "allow_sonar_sim_in_hardware",
            _env_alias(
                env,
                "TONYPI_ALLOW_SIM_WITH_HARDWARE",
                "TONYPI_ALLOW_SONAR_SIM_IN_HARDWARE",
                False,
                lambda raw: _parse_bool("TONYPI_ALLOW_SIM_WITH_HARDWARE", raw),
            ),
        ),
        sonar_failure_limit=value(
            "sonar_failure_limit", "TONYPI_SONAR_FAILURE_LIMIT", SONAR_FAILURE_LIMIT, int
        ),
        sonar_warn_interval_s=value(
            "sonar_warn_interval_s", "TONYPI_SONAR_WARN_INTERVAL", SONAR_WARN_INTERVAL_S, float
        ),
        sonar_max_valid_mm=value(
            "sonar_max_valid_mm", "TONYPI_SONAR_MAX_VALID_MM", SONAR_MAX_VALID_MM, int
        ),
        sonar_disconnected_sentinel=value(
            "sonar_disconnected_sentinel",
            "TONYPI_SONAR_DISCONNECTED_SENTINEL",
            SONAR_DISCONNECTED_SENTINEL,
            int,
        ),
    )
    validate_config(config)
    return config


def validate_config(config: ServiceConfig) -> None:
    errors = []  # type: List[str]
    if config.mode not in (MODE_HARDWARE, MODE_DRY_RUN):
        errors.append("mode must be hardware or dry-run")
    if not config.host.strip():
        errors.append("host must not be empty")
    if not config.udp_color_host.strip():
        errors.append("udp_color_host must not be empty")
    if not 1 <= config.port <= 65535:
        errors.append("port must be in [1, 65535]")
    if not 1 <= config.udp_color_port <= 65535:
        errors.append("udp_color_port must be in [1, 65535]")
    if not 0.0 <= config.deadzone < 1.0:
        errors.append("deadzone must be in [0, 1)")
    if not 0.0 <= config.watchdog_s <= 60.0:
        errors.append("watchdog_s must be in [0, 60]")
    if not 0.02 <= config.step_interval_s <= 10.0:
        errors.append("step_interval_s must be in [0.02, 10]")
    if not 0.02 <= config.dist_interval_s <= 60.0:
        errors.append("dist_interval_s must be in [0.02, 60]")
    if not 0.0 <= config.cmd_cooldown_s <= 60.0:
        errors.append("cmd_cooldown_s must be in [0, 60]")
    if not 1 <= config.sonar_failure_limit <= 1000:
        errors.append("sonar_failure_limit must be in [1, 1000]")
    if not 0.0 <= config.sonar_warn_interval_s <= 3600.0:
        errors.append("sonar_warn_interval_s must be in [0, 3600]")
    if not 1 <= config.sonar_max_valid_mm < config.sonar_disconnected_sentinel:
        errors.append("sonar max must be positive and below the disconnected sentinel")

    if not config.deadzone < config.tier_slow_max < config.tier_fast_min <= 1.0:
        errors.append("speed tiers must satisfy deadzone < tier_slow_max < tier_fast_min <= 1")

    actions = (
        config.action_forward,
        config.action_back,
        config.action_turn_r,
        config.action_turn_l,
        config.action_stand,
        config.action_forward_slow,
        config.action_forward_fast,
        config.action_back_slow,
        config.action_back_fast,
        config.action_turn_r_slow,
        config.action_turn_r_fast,
        config.action_turn_l_slow,
        config.action_turn_l_fast,
        config.action_lateral_r,
        config.action_lateral_l,
        config.action_lateral_r_slow,
        config.action_lateral_l_slow,
        config.action_lateral_r_fast,
        config.action_lateral_l_fast,
    )
    if any(_ACTION_RE.fullmatch(name or "") is None for name in actions):
        errors.append("action group names must match [A-Za-z0-9_.-]{1,128}")

    if config.head_pitch_id not in (1, 2) or config.head_yaw_id not in (1, 2):
        errors.append("head servo IDs must be 1 or 2")
    if config.head_pitch_id == config.head_yaw_id:
        errors.append("pitch and yaw servo IDs must differ")
    if not BOARD_PULSE_MIN <= config.pulse_min < config.pulse_max <= BOARD_PULSE_MAX:
        errors.append("pulse range must be ordered within [500, 2500]")

    axes = (
        ("pitch", config.pitch_min, config.pitch_center, config.pitch_max, config.nod_amplitude),
        ("yaw", config.yaw_min, config.yaw_center, config.yaw_max, config.shake_amplitude),
    )
    for name, lo, center, hi, amplitude in axes:
        if not config.pulse_min <= lo < center < hi <= config.pulse_max:
            errors.append("%s min/center/max must be ordered inside pulse range" % name)
        if amplitude <= 0 or center - amplitude < lo or center + amplitude > hi:
            errors.append("%s amplitude must be positive and remain inside axis limits" % name)
    if not BOARD_USE_TIME_MIN_MS <= config.head_step_ms <= BOARD_USE_TIME_MAX_MS:
        errors.append("head_step_ms must be in [20, 30000]")

    if config.mode == MODE_HARDWARE and config.sonar_sim and not config.allow_sonar_sim_in_hardware:
        errors.append(
            "TONYPI_SONAR_SIM is forbidden in hardware mode unless TONYPI_ALLOW_SIM_WITH_HARDWARE=1"
        )
    if config.mode == MODE_DRY_RUN and config.require_sonar and not config.sonar_sim:
        errors.append("dry-run with required Sonar needs TONYPI_SONAR_SIM")
    if config.sonar_sim:
        try:
            parse_sonar_sim(
                config.sonar_sim,
                config.sonar_max_valid_mm,
                config.sonar_disconnected_sentinel,
            )
        except ValueError as exc:
            errors.append("invalid sonar simulation: %s" % exc)
    if config.mode == MODE_HARDWARE and config.action_group_dir:
        group_dir = Path(config.action_group_dir)
        if not group_dir.is_dir():
            errors.append("action_group_dir does not exist or is not a directory")
        else:
            missing = [name for name in actions if not (group_dir / (name + ".d6a")).is_file()]
            if missing:
                errors.append("missing action group files: %s" % ", ".join(missing))

    if errors:
        raise ConfigError("; ".join(errors))


def dry_run_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Compatibility helper for old callers using TONYPI_DRY_RUN."""
    return _resolve_mode(None, os.environ if environ is None else environ) == MODE_DRY_RUN


def _load_hardware(mode: str) -> Tuple[Optional[ModuleType], Optional[ModuleType]]:
    if mode == MODE_DRY_RUN:
        return None, None
    try:
        from hiwonder import ActionGroupControl, Board
    except Exception as exc:
        raise StartupError("hiwonder actuator SDK unavailable: %s" % exc)
    if not callable(getattr(Board, "setPWMServoPulse", None)):
        raise StartupError("hiwonder.Board.setPWMServoPulse API unavailable")
    if not (
        callable(getattr(ActionGroupControl, "runActionGroup", None))
        or callable(getattr(ActionGroupControl, "runAction", None))
    ):
        raise StartupError("hiwonder ActionGroupControl runActionGroup/runAction API unavailable")
    return Board, ActionGroupControl


def _load_sonar(mode: str, required: bool) -> Optional[ModuleType]:
    if mode == MODE_DRY_RUN:
        return None
    try:
        from hiwonder import Sonar
    except Exception as exc:
        if required:
            raise StartupError("required hiwonder Sonar SDK unavailable: %s" % exc)
        print("[WARN] hiwonder Sonar unavailable; DIST telemetry disabled:", exc)
        return None
    if not callable(getattr(Sonar, "Sonar", None)):
        if required:
            raise StartupError("required hiwonder.Sonar.Sonar API unavailable")
        print("[WARN] hiwonder.Sonar.Sonar API unavailable; DIST telemetry disabled")
        return None
    return Sonar


def prepare_runtime(config: ServiceConfig) -> RuntimeHardware:
    """Validate and load runtime dependencies without moving any actuator."""
    board, agc = _load_hardware(config.mode)
    # Do not initialize or read the Sonar SDK.  The compatibility helpers below
    # remain importable for old callers, but the running service never uses them.
    return RuntimeHardware(board=board, agc=agc)


# Pure protocol/planning logic.
def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def vector_to_mode(
    v: float,
    steer: float,
    deadzone: float = DEADZONE,
    slow_max: float = TIER_SLOW_MAX,
    fast_min: float = TIER_FAST_MIN,
    lateral: float = 0.0,
) -> str:
    """Exclusive gait priority: turn > lateral > forward/backward."""

    def tier(value: float) -> str:
        magnitude = abs(value)
        if magnitude <= slow_max:
            return "_slow"
        if magnitude <= fast_min:
            return ""
        return "_fast"

    if abs(steer) > deadzone:
        return ("turn_r" if steer > 0 else "turn_l") + tier(steer)
    if abs(lateral) > deadzone:
        return ("lateral_r" if lateral > 0 else "lateral_l") + tier(lateral)
    if abs(v) > deadzone:
        return ("forward" if v > 0 else "back") + tier(v)
    return "stand"


def head_swing(
    servo_id: int,
    center: int,
    amplitude: int,
    limits: Tuple[int, int],
    step_ms: int = HEAD_STEP_MS,
) -> List[Tuple[Any, ...]]:
    lo, hi = limits
    up = clamp(center + amplitude, lo, hi)
    down = clamp(center - amplitude, lo, hi)
    return [
        ("head", servo_id, up, step_ms),
        ("head", servo_id, down, step_ms),
        ("head", servo_id, clamp(center, lo, hi), step_ms),
    ]


def nod_plan(config: Optional[ServiceConfig] = None) -> List[Tuple[Any, ...]]:
    if config is None:
        return head_swing(HEAD_PITCH_ID, PITCH_CENTER, NOD_AMPLITUDE, (PITCH_MIN, PITCH_MAX))
    return head_swing(
        config.head_pitch_id,
        config.pitch_center,
        config.nod_amplitude,
        (config.pitch_min, config.pitch_max),
        config.head_step_ms,
    )


def shake_plan(config: Optional[ServiceConfig] = None) -> List[Tuple[Any, ...]]:
    if config is None:
        return head_swing(HEAD_YAW_ID, YAW_CENTER, SHAKE_AMPLITUDE, (YAW_MIN, YAW_MAX))
    return head_swing(
        config.head_yaw_id,
        config.yaw_center,
        config.shake_amplitude,
        (config.yaw_min, config.yaw_max),
        config.head_step_ms,
    )


@dataclass
class RobotSession:
    now: Callable[[], float] = field(default=time.time)
    mode: str = "stand"
    last_rx_ts: float = 0.0
    unknown_cmds: int = 0
    last_cmd_ts: Dict[str, float] = field(default_factory=dict)
    deadzone: float = DEADZONE
    watchdog_s: float = WATCHDOG_S
    cmd_cooldown_s: float = CMD_COOLDOWN_S
    on_stand_once: bool = ON_STAND_ONCE
    action_forward: str = ACTION_FORWARD
    action_back: str = ACTION_BACK
    action_turn_r: str = ACTION_TURN_R
    action_turn_l: str = ACTION_TURN_L
    action_stand: str = ACTION_STAND
    tier_slow_max: float = TIER_SLOW_MAX
    tier_fast_min: float = TIER_FAST_MIN
    action_forward_slow: str = ACTION_FORWARD_SLOW
    action_forward_fast: str = ACTION_FORWARD_FAST
    action_back_slow: str = ACTION_BACK_SLOW
    action_back_fast: str = ACTION_BACK_FAST
    action_turn_r_slow: str = ACTION_TURN_R_SLOW
    action_turn_r_fast: str = ACTION_TURN_R_FAST
    action_turn_l_slow: str = ACTION_TURN_L_SLOW
    action_turn_l_fast: str = ACTION_TURN_L_FAST
    allow_cmd_while_moving: bool = True
    action_lateral_r: str = ACTION_LATERAL_R
    action_lateral_l: str = ACTION_LATERAL_L
    action_lateral_r_slow: str = ACTION_LATERAL_R_SLOW
    action_lateral_l_slow: str = ACTION_LATERAL_L_SLOW
    action_lateral_r_fast: str = ACTION_LATERAL_R_FAST
    action_lateral_l_fast: str = ACTION_LATERAL_L_FAST
    nod: Callable[[], List[Tuple[Any, ...]]] = field(default=nod_plan)
    shake: Callable[[], List[Tuple[Any, ...]]] = field(default=shake_plan)

    @classmethod
    def from_config(cls, config: ServiceConfig) -> "RobotSession":
        return cls(
            deadzone=config.deadzone,
            watchdog_s=config.watchdog_s,
            cmd_cooldown_s=config.cmd_cooldown_s,
            on_stand_once=config.on_stand_once,
            action_forward=config.action_forward,
            action_back=config.action_back,
            action_turn_r=config.action_turn_r,
            action_turn_l=config.action_turn_l,
            action_stand=config.action_stand,
            tier_slow_max=config.tier_slow_max,
            tier_fast_min=config.tier_fast_min,
            action_forward_slow=config.action_forward_slow,
            action_forward_fast=config.action_forward_fast,
            action_back_slow=config.action_back_slow,
            action_back_fast=config.action_back_fast,
            action_turn_r_slow=config.action_turn_r_slow,
            action_turn_r_fast=config.action_turn_r_fast,
            action_turn_l_slow=config.action_turn_l_slow,
            action_turn_l_fast=config.action_turn_l_fast,
            allow_cmd_while_moving=config.allow_cmd_while_moving,
            action_lateral_r=config.action_lateral_r,
            action_lateral_l=config.action_lateral_l,
            action_lateral_r_slow=config.action_lateral_r_slow,
            action_lateral_l_slow=config.action_lateral_l_slow,
            action_lateral_r_fast=config.action_lateral_r_fast,
            action_lateral_l_fast=config.action_lateral_l_fast,
            nod=lambda: nod_plan(config),
            shake=lambda: shake_plan(config),
        )

    def handle_line(self, line: str) -> List[Tuple[Any, ...]]:
        text = (line or "").strip()
        if not text:
            return []
        if text.startswith("CMD:"):
            ts = self.now()
            self.last_rx_ts = ts
            plan = self.handle_cmd(text[4:], ts)
            print("[RX] CMD:%s -> %s" % (text[4:].strip(), plan or "ignored"))
            return plan
        try:
            msg = json.loads(text)
            if not isinstance(msg, dict):
                raise TypeError("payload is not a JSON object")
            lateral = msg.get("lateral", 0.0)
            if (
                isinstance(lateral, bool)
                or not isinstance(lateral, (int, float))
                or not math.isfinite(lateral)
                or not -1.0 <= lateral <= 1.0
            ):
                return []  # Invalid extensions must not refresh the watchdog.
            v = float(msg.get("v", 0.0))
            steer = float(msg.get("steer", 0.0))
        except (TypeError, ValueError):
            return []
        self.last_rx_ts = self.now()
        mode = vector_to_mode(
            v, steer, self.deadzone, self.tier_slow_max, self.tier_fast_min, lateral
        )
        plan = self._set_mode(mode)
        action = self.tick_group()
        print(
            "[RX] v=%+.3f steer=%+.3f lateral=%+.3f -> %s%s"
            % (v, steer, lateral, mode, " (%s)" % action if action else "")
        )
        return plan

    def handle_cmd(self, raw_cmd: str, ts: Optional[float] = None) -> List[Tuple[Any, ...]]:
        cmd = (raw_cmd or "").strip().lower()
        if not cmd:
            return []
        if cmd != "stand" and self.mode != "stand" and not self.allow_cmd_while_moving:
            return []
        timestamp = self.now() if ts is None else ts
        if timestamp - self.last_cmd_ts.get(cmd, float("-inf")) < self.cmd_cooldown_s:
            return []
        self.last_cmd_ts[cmd] = timestamp
        if cmd == "nod":
            return self.nod()
        if cmd == "shake":
            return self.shake()
        if cmd == "stand":
            return self._set_mode("stand", force=True)
        action = CMD_MAP.get(cmd)
        if action is None:
            self.unknown_cmds += 1
            return [("unknown", cmd)]
        return [("group", action)]

    def watchdog_plan(self, ts: Optional[float] = None) -> List[Tuple[Any, ...]]:
        timestamp = self.now() if ts is None else ts
        if (
            self.watchdog_s > 0
            and self.last_rx_ts > 0
            and (timestamp - self.last_rx_ts) > self.watchdog_s
            and self.mode != "stand"
        ):
            return self._set_mode("stand")
        return []

    def tick_group(self) -> Optional[str]:
        return {
            "forward": self.action_forward,
            "forward_slow": self.action_forward_slow,
            "forward_fast": self.action_forward_fast,
            "back": self.action_back,
            "back_slow": self.action_back_slow,
            "back_fast": self.action_back_fast,
            "turn_r": self.action_turn_r,
            "turn_r_slow": self.action_turn_r_slow,
            "turn_r_fast": self.action_turn_r_fast,
            "turn_l": self.action_turn_l,
            "turn_l_slow": self.action_turn_l_slow,
            "turn_l_fast": self.action_turn_l_fast,
            "lateral_r": self.action_lateral_r,
            "lateral_l": self.action_lateral_l,
            "lateral_r_slow": self.action_lateral_r_slow,
            "lateral_l_slow": self.action_lateral_l_slow,
            "lateral_r_fast": self.action_lateral_r_fast,
            "lateral_l_fast": self.action_lateral_l_fast,
        }.get(self.mode)

    def force_stand(self) -> List[Tuple[Any, ...]]:
        return self._set_mode("stand", force=True)

    def settle_stand(self) -> List[Tuple[Any, ...]]:
        return self._set_mode("stand")

    def _set_mode(self, new_mode: str, force: bool = False) -> List[Tuple[Any, ...]]:
        if new_mode == self.mode and not force:
            return []
        self.mode = new_mode
        plan = [("mode", new_mode)]  # type: List[Tuple[Any, ...]]
        if new_mode == "stand" and self.action_stand and (self.on_stand_once or force):
            plan.append(("group", self.action_stand))
        return plan


# Actuator primitives. Hardware failures propagate to TonyPiService for latching.
def run_group(
    name: str,
    agc: Optional[ModuleType] = None,
    dry_run: Optional[bool] = None,
) -> None:
    if not name:
        return
    is_dry_run = dry_run_enabled() if dry_run is None else dry_run
    target = AGC if agc is None else agc
    if is_dry_run:
        print("[DRY] run action group:", name)
        return
    if target is None:
        raise ActuatorError("action-group SDK unavailable")
    try:
        if callable(getattr(target, "runActionGroup", None)):
            target.runActionGroup(name)
        elif callable(getattr(target, "runAction", None)):
            target.runAction(name)
        else:
            raise AttributeError("runActionGroup/runAction unavailable")
        print("[ACT]", name)
    except Exception as exc:
        raise ActuatorError("action group %s failed: %s" % (name, exc))


def move_head(
    servo_id: int,
    pulse: int,
    use_time_ms: int,
    board: Optional[ModuleType] = None,
    dry_run: Optional[bool] = None,
) -> None:
    if servo_id not in (1, 2):
        raise ValueError("servo_id must be 1 or 2")
    if not BOARD_PULSE_MIN <= pulse <= BOARD_PULSE_MAX:
        raise ValueError("pulse must be in [500, 2500]")
    if not BOARD_USE_TIME_MIN_MS <= use_time_ms <= BOARD_USE_TIME_MAX_MS:
        raise ValueError("use_time_ms must be in [20, 30000]")
    is_dry_run = dry_run_enabled() if dry_run is None else dry_run
    target = BOARD if board is None else board
    if is_dry_run:
        print("[DRY] head servo %d -> pulse %d in %dms" % (servo_id, pulse, use_time_ms))
        return
    if target is None:
        raise ActuatorError("Board SDK unavailable")
    try:
        target.setPWMServoPulse(servo_id, pulse, use_time_ms)
    except Exception as exc:
        raise ActuatorError("head servo %d failed: %s" % (servo_id, exc))
    time.sleep(use_time_ms / 1000.0 + 0.02)


def execute(
    plan: Sequence[Tuple[Any, ...]],
    board: Optional[ModuleType] = None,
    agc: Optional[ModuleType] = None,
    dry_run: Optional[bool] = None,
) -> None:
    for item in plan:
        kind = item[0]
        if kind == "group":
            run_group(item[1], agc=agc, dry_run=dry_run)
        elif kind == "head":
            move_head(item[1], item[2], item[3], board=board, dry_run=dry_run)
        elif kind == "mode":
            print("[MODE]", item[1])
        elif kind == "unknown":
            print("[CMD] %s (no mapping, ignored)" % item[1])


# Sonar simulation and source selection.
def _validate_sim_distance(value: int, max_valid_mm: int, sentinel: int) -> int:
    if value == sentinel or 0 <= value <= max_valid_mm:
        return value
    raise ValueError("distance must be in [0, %d] or sentinel %d" % (max_valid_mm, sentinel))


def parse_sonar_sim(
    spec: str,
    max_valid_mm: int = SONAR_MAX_VALID_MM,
    sentinel: int = SONAR_DISCONNECTED_SENTINEL,
) -> Callable[[float], int]:
    text = (spec or "").strip()
    parts = text.split(":")
    kind = parts[0].strip().lower()
    if kind == "flat" and len(parts) == 2 and parts[1].strip():
        base = _validate_sim_distance(int(float(parts[1])), max_valid_mm, sentinel)
        return lambda _t: base
    if kind == "sweep" and len(parts) == 4:
        lo = _validate_sim_distance(int(float(parts[1])), max_valid_mm, sentinel)
        hi = _validate_sim_distance(int(float(parts[2])), max_valid_mm, sentinel)
        period = float(parts[3])
        if lo == sentinel or hi == sentinel:
            raise ValueError("sweep endpoints cannot use the disconnected sentinel")
        if period <= 0:
            raise ValueError("sweep period_s must be > 0")
        if hi < lo:
            raise ValueError("sweep needs min <= max")
        span = hi - lo

        def sweep(t: float) -> int:
            phase = (t % period) / period
            tri = 1.0 - abs(2.0 * phase - 1.0)
            return round(lo + span * tri)

        return sweep
    if kind == "steps" and len(parts) >= 2:
        marks = []  # type: List[Tuple[int, float]]
        for item in ":".join(parts[1:]).split(","):
            value_text, separator, at_text = item.partition("@")
            if not separator or not value_text.strip() or not at_text.strip():
                raise ValueError("steps needs v1@t1[,v2@t2,...]")
            value = _validate_sim_distance(int(float(value_text)), max_valid_mm, sentinel)
            at = float(at_text)
            if at <= 0:
                raise ValueError("steps times must be > 0")
            marks.append((value, at))
        if not marks:
            raise ValueError("steps needs v1@t1[,v2@t2,...]")
        for index in range(1, len(marks)):
            if marks[index][1] <= marks[index - 1][1]:
                raise ValueError("steps times must be strictly increasing")

        def steps(t: float) -> int:
            value = marks[0][0]
            for index in range(len(marks) - 1):
                if t >= marks[index][1]:
                    value = marks[index + 1][0]
                else:
                    break
            return value

        return steps
    raise ValueError("unsupported TONYPI_SONAR_SIM spec: %r" % spec)


def create_sonar(
    module: Optional[ModuleType] = None,
    mode: Optional[str] = None,
) -> Optional[Any]:
    selected_mode = MODE_DRY_RUN if dry_run_enabled() else MODE_HARDWARE if mode is None else mode
    if selected_mode == MODE_DRY_RUN:
        return None
    target = SONAR_MODULE if module is None else module
    if target is None:
        return None
    try:
        sonar = target.Sonar()
    except Exception as exc:
        print("[WARN] Sonar init failed; DIST telemetry disabled:", exc)
        return None
    if not callable(getattr(sonar, "getDistance", None)):
        print("[WARN] Sonar.getDistance API unavailable; DIST telemetry disabled")
        return None
    return sonar


def _compat_config() -> ServiceConfig:
    mode = MODE_DRY_RUN if dry_run_enabled() else MODE_HARDWARE
    # Legacy direct-import callers historically changed module constants at runtime.
    # Do not apply strict startup validation here; main()/load_config() remain strict.
    return ServiceConfig(
        mode=mode,
        dist_interval_s=DIST_INTERVAL_S if DIST_INTERVAL_S > 0 else 0.3,
        sonar_sim=os.environ.get("TONYPI_SONAR_SIM", "").strip(),
        allow_sonar_sim_in_hardware=True,
    )


def build_distance_source(
    config: Optional[ServiceConfig] = None,
    sonar: Optional[Any] = None,
) -> Optional[Callable[[], Optional[int]]]:
    selected = _compat_config() if config is None else config
    if selected.sonar_sim:
        try:
            sample = parse_sonar_sim(
                selected.sonar_sim,
                selected.sonar_max_valid_mm,
                selected.sonar_disconnected_sentinel,
            )
        except ValueError:
            if config is not None:
                raise
            print("[WARN] bad TONYPI_SONAR_SIM; DIST telemetry disabled")
            return None
        t0 = time.monotonic()
        return lambda: sample(time.monotonic() - t0)
    target = sonar
    if target is None and config is None:
        target = create_sonar()
    if target is not None:
        return lambda: int(target.getDistance())
    return None


class TonyPiService:
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        session: Optional[RobotSession] = None,
        config: Optional[ServiceConfig] = None,
        runtime: Optional[RuntimeHardware] = None,
    ) -> None:
        selected = _compat_config() if config is None else config
        if host is not None or port is not None:
            values = dict(selected.__dict__)
            if host is not None:
                values["host"] = host
            if port is not None:
                values["port"] = port
            selected = ServiceConfig(**values)
            validate_config(selected)
        self.config = selected
        self.host = selected.host
        self.port = selected.port
        self.session = session or RobotSession.from_config(selected)
        self.runtime = runtime or RuntimeHardware(board=BOARD, agc=AGC)
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._actuator_lock = threading.Lock()
        self._stop = threading.Event()
        self._conn = None  # type: Optional[socket.socket]
        self._server_socket = None  # type: Optional[socket.socket]
        self._udp_socket = None  # type: Optional[socket.socket]
        self._fault = None  # type: Optional[str]
        self._last_sonar_warning = float("-inf")
        self._step = None
        self._step_history = {}
        self._step_wakeup = threading.Event()
        self.action_directory = Path("/home/pi/TonyPi/ActionGroups")

    @property
    def fault(self) -> Optional[str]:
        with self._lock:
            return self._fault

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._cancel_step_locked()
            sockets = (self._conn, self._server_socket, self._udp_socket)
        self._step_wakeup.set()
        for sock in sockets:
            if sock is None:
                continue
            try:
                shutdown = getattr(sock, "shutdown", None)
                if callable(shutdown):
                    shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                close = getattr(sock, "close", None)
                if callable(close):
                    close()
            except OSError:
                pass

    def _latch_fault(self, message: str) -> None:
        first = False
        with self._lock:
            if self._fault is None:
                self._fault = message
                self.session.mode = "stand"
                first = True
        if first:
            print("[FAULT]", message)
        self.stop()

    def _execute_plan(self, plan: Sequence[Tuple[Any, ...]], fatal: bool = True) -> bool:
        if not plan:
            return True
        try:
            with self._actuator_lock:
                execute(
                    plan,
                    board=self.runtime.board,
                    agc=self.runtime.agc,
                    dry_run=self.config.mode == MODE_DRY_RUN,
                )
        except (ActuatorError, ValueError) as exc:
            if fatal:
                self._latch_fault("actuator failure: %s" % exc)
            else:
                print("[ERR] final stand failed:", exc)
            return False
        return True

    def _run_current_group(self) -> bool:
        """Execute only the gait that is current after acquiring the actuator."""
        try:
            with self._actuator_lock:
                with self._lock:
                    if self._stop.is_set():
                        return True
                    group = self.session.tick_group()
                if group is None:
                    return True
                run_group(
                    group,
                    agc=self.runtime.agc,
                    dry_run=self.config.mode == MODE_DRY_RUN,
                )
        except (ActuatorError, ValueError) as exc:
            self._latch_fault("actuator failure: %s" % exc)
            return False
        return True

    def _cancel_step_locked(self) -> None:
        if self._step is not None:
            self._step["cancel"].set()
            self._step_wakeup.set()

    def _step_reply(self, conn, request) -> None:
        payload = {key: request[key] for key in ("id", "status", "detail")}
        self._send_line(conn, ("STEP_STATUS:" + json.dumps(payload) + "\n").encode(), "STEP")

    def _handle_step_line(self, conn, text: str) -> bool:
        """Handle the isolated extension; never fall through to legacy JSON."""
        if text.startswith("STEP_PING:"):
            with self._lock:
                if self._step is not None and self._step["id"] == text[10:]:
                    self._step["heartbeat"] = time.monotonic()
            return True
        if not text.startswith("STEP:"):
            return False
        try:
            payload = json.loads(text[5:])
            ident = payload["id"]
            if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", ident):
                return True
            steer, lateral = payload.get("steer", 0.0), payload.get("lateral", 0.0)
            valid = set(payload) <= {"id", "steer", "lateral"}
            valid = valid and all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and -1 <= v <= 1 for v in (steer, lateral)
            )
            valid = valid and bool(steer) != bool(lateral)
        except (ValueError, TypeError, KeyError):
            return True
        with self._lock:
            previous = self._step_history.get(ident)
            if previous is not None:
                # Every ID is immutable, including rejected/cancelled requests.
                reply = dict(previous)
            else:
                reply = {"id": ident, "status": "error", "detail": "invalid_step"}
                if len(self._step_history) >= 1024:
                    reply["detail"] = "session_step_limit"
                else:
                    mode = vector_to_mode(0, steer, self.session.deadzone,
                                          self.session.tier_slow_max,
                                          self.session.tier_fast_min, lateral) if valid else "stand"
                    if mode == "stand":
                        pass
                    elif self._step is not None or self.session.mode != "stand":
                        reply["detail"] = "busy"
                    elif self.config.mode != MODE_DRY_RUN and not callable(
                        getattr(self.runtime.board, "setBusServoPulse", None)
                    ):
                        reply["detail"] = "step_unavailable"
                    else:
                        # Resolve through the configured mappings, not a user path.
                        self.session.mode = mode
                        group = self.session.tick_group()
                        self.session.mode = "stand"
                        reply.update(status="accepted", detail="", group=group, conn=conn,
                                     cancel=threading.Event(), heartbeat=time.monotonic())
                        self._step = reply
                    self._step_history[ident] = reply
                reply = dict(reply)
        self._step_reply(conn, reply)
        self._step_wakeup.set()
        return True

    def _play_step(self, request) -> None:
        """Play one validated d6a sequence using the vendor's frame semantics.

        runActionGroup swallows exceptions and runAction silently skips missing
        files/busy state. Read the immutable sequence before any servo output,
        and use the same Board API with explicit failure/cancellation reporting.
        """
        if self.config.mode == MODE_DRY_RUN:
            print("[DRY] single step:", request["group"])
            return
        path = (self.action_directory / (request["group"] + ".d6a")).resolve()
        if path.parent != self.action_directory.resolve():
            raise ActuatorError("step action outside action directory")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
            rows = database.execute("SELECT * FROM ActionGroup ORDER BY rowid").fetchall()
        if not rows:
            raise ActuatorError("empty step action")
        total_ms = 0
        for row in rows:
            if len(row) < 3 or not isinstance(row[1], int) or not 20 <= row[1] <= 30000:
                raise ActuatorError("invalid step frame duration")
            if len(row) > 26 or any(type(p) is not int or not 0 <= p <= 1000 for p in row[2:]):
                raise ActuatorError("invalid step servo pulse")
            total_ms += row[1]
        if total_ms > 30000:
            raise ActuatorError("step action exceeds 30 seconds")
        for row in rows:
            if request["cancel"].is_set():
                return
            if time.monotonic() - request["heartbeat"] > 1.5:
                request["cancel"].set()
                return
            for servo, pulse in enumerate(row[2:], 1):
                self.runtime.board.setBusServoPulse(servo, pulse, row[1])
            # Finish the already-issued physical frame before reporting cancel.
            time.sleep(row[1] / 1000.0)
        if time.monotonic() - request["heartbeat"] > 1.5:
            request["cancel"].set()

    def _run_step(self) -> bool:
        with self._lock:
            request = self._step
        if request is None:
            return False
        status, detail = "done", ""
        try:
            with self._actuator_lock:
                if not request["cancel"].is_set():
                    self._play_step(request)
        except Exception as exc:
            status, detail = "error", type(exc).__name__ + ": " + str(exc)
        with self._lock:
            if request["cancel"].is_set() and status != "error":
                status, detail = "cancelled", "interrupted_or_heartbeat_lost"
            request.update(status=status, detail=detail)
            if self._step is request:
                self._step = None
            reply = dict(request)
        self._step_reply(request["conn"], reply)
        if status == "error":
            self._latch_fault("single step failed: " + detail)
        return True

    def _send_line(self, conn: socket.socket, data: bytes, tag: str) -> bool:
        try:
            with self._write_lock:
                conn.sendall(data)
        except OSError as exc:
            if not self._stop.is_set():
                print("[%s] send error:" % tag, exc)
            return False
        return True

    def _send_to_unity_text(self, text: str) -> None:
        data = (text.strip() + "\n").encode("utf-8")
        with self._lock:
            conn = self._conn
        if conn is None:
            return
        if self._send_line(conn, data, "FWD") and self.config.print_fwd:
            print("[FWD] -> Unity:", text.strip())

    def _udp_color_listener(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with self._lock:
            self._udp_socket = sock
        try:
            sock.bind((self.config.udp_color_host, self.config.udp_color_port))
            sock.settimeout(1.0)
            print(
                "[UDP] Color listen on %s:%d"
                % (self.config.udp_color_host, self.config.udp_color_port)
            )
            while not self._stop.is_set():
                try:
                    data, _ = sock.recvfrom(1024)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError as exc:
                    if not self._stop.is_set():
                        self._latch_fault("UDP listener failed: %s" % exc)
                    break
                tag = data.decode("utf-8", "ignore").strip().upper()
                if tag in ("RED", "GREEN"):
                    self._send_to_unity_text("COLOR_SIGNAL:%s" % tag)
        except OSError as exc:
            if not self._stop.is_set():
                self._latch_fault("UDP bind failed: %s" % exc)
        finally:
            try:
                sock.close()
            except OSError:
                pass
            with self._lock:
                if self._udp_socket is sock:
                    self._udp_socket = None

    def _motion_loop(self) -> None:
        while not self._stop.is_set():
            if self._run_step():
                continue
            with self._lock:
                watchdog = self.session.watchdog_plan()
            if watchdog and not self._execute_plan(watchdog):
                break
            if not self._run_current_group():
                break
            self._step_wakeup.wait(self.config.step_interval_s)
            self._step_wakeup.clear()

    def _warn_sonar(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_sonar_warning >= self.config.sonar_warn_interval_s:
            print("[WARN] Sonar:", message)
            self._last_sonar_warning = now

    def _distance_loop(self, source: Callable[[], Optional[int]], done: threading.Event) -> None:
        consecutive_failures = 0
        while not self._stop.is_set() and not done.is_set():
            value = None  # type: Optional[int]
            problem = None  # type: Optional[str]
            try:
                raw = source()
                if raw is None:
                    problem = "read returned no value"
                else:
                    value = int(raw)
                    if value == self.config.sonar_disconnected_sentinel:
                        problem = "sensor disconnected sentinel %d" % value
                    elif not 0 <= value <= self.config.sonar_max_valid_mm:
                        problem = "out-of-range reading %d mm" % value
            except Exception as exc:
                problem = "read error: %s" % exc

            if problem is None:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                self._warn_sonar(problem)
                if (
                    self.config.require_sonar
                    and consecutive_failures >= self.config.sonar_failure_limit
                ):
                    self._latch_fault(
                        "required Sonar failed %d consecutive reads: %s"
                        % (consecutive_failures, problem)
                    )
                    break

            # Preserve DIST framing and the SDK's 99999 sentinel when a value exists.
            if value is not None:
                data = ("DIST:%d\n" % value).encode("ascii")
                with self._lock:
                    conn = self._conn
                if conn is not None and self._send_line(conn, data, "DIST"):
                    print("[DIST]", value, "mm")
            done.wait(self.config.dist_interval_s)

    def _handle_connection(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        buffer = b""
        while not self._stop.is_set():
            try:
                data = conn.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    print("[TCP] conn error:", exc)
                break
            if not data:
                break
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode("utf-8", "ignore").strip()
                if not text:
                    continue
                if self._handle_step_line(conn, text):
                    continue
                with self._lock:
                    self._cancel_step_locked()
                    plan = self.session.handle_line(text)
                if not self._execute_plan(plan):
                    return

    def serve_forever(self) -> None:
        print("[TCP] Listening on %s:%d" % (self.host, self.port))
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with self._lock:
            self._server_socket = srv
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(1)
            srv.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    conn, addr = srv.accept()
                except (socket.timeout, TimeoutError):
                    continue
                except OSError as exc:
                    if self._stop.is_set():
                        break
                    raise StartupError("TCP accept failed: %s" % exc)
                print("[TCP] Connected:", addr)
                with self._lock:
                    self._conn = conn
                    self._step_history = {}
                    plan = self.session.force_stand()
                if not self._execute_plan(plan):
                    break
                if self.config.mode == MODE_DRY_RUN or callable(
                    getattr(self.runtime.board, "setBusServoPulse", None)
                ):
                    self._send_line(conn, b"CAPS:STEP_V1\n", "CAPS")

                try:
                    self._handle_connection(conn)
                finally:
                    with self._lock:
                        self._cancel_step_locked()
                    try:
                        conn.close()
                    except OSError:
                        pass
                print("[TCP] Disconnected:", addr)
                with self._lock:
                    if self._conn is conn:
                        self._conn = None
                    plan = self.session.settle_stand()
                if not self._stop.is_set():
                    self._execute_plan(plan)
        finally:
            try:
                srv.close()
            except OSError:
                pass
            with self._lock:
                if self._server_socket is srv:
                    self._server_socket = None

    def _final_stand(self) -> None:
        with self._lock:
            plan = self.session.force_stand()
        self._execute_plan(plan, fatal=False)

    def run(self) -> None:
        motion = threading.Thread(target=self._motion_loop, name="motion", daemon=True)
        udp = threading.Thread(target=self._udp_color_listener, name="udp-color", daemon=True)
        motion.start()
        udp.start()
        try:
            self.serve_forever()
        finally:
            self.stop()
            motion.join(timeout=2.0)
            udp.join(timeout=2.0)
            self._final_stand()


def check_sonar_reading(config: ServiceConfig, runtime: RuntimeHardware) -> None:
    """Read one required Sonar sample during preflight without moving actuators."""
    if not config.require_sonar:
        return
    source = build_distance_source(config, runtime.sonar)
    if source is None:
        raise StartupError("required Sonar source unavailable during preflight")
    try:
        value = source()
        if value is None:
            raise ValueError("read returned no value")
        reading = int(value)
    except Exception as exc:
        raise StartupError("required Sonar preflight read failed: %s" % exc)
    if reading == config.sonar_disconnected_sentinel:
        raise StartupError("required Sonar reports disconnected sentinel %d" % reading)
    if not 0 <= reading <= config.sonar_max_valid_mm:
        raise StartupError("required Sonar preflight reading out of range: %d mm" % reading)


def check_network_bindings(config: ServiceConfig) -> None:
    """Verify TCP/UDP listen addresses without starting threads or actuators."""
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind((config.host, config.port))
        udp.bind((config.udp_color_host, config.udp_color_port))
    except OSError as exc:
        raise StartupError("network preflight failed: %s" % exc)
    finally:
        tcp.close()
        udp.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TonyPi JSONL/CMD/DIST robot-side service")
    parser.add_argument("--mode", choices=(MODE_HARDWARE, MODE_DRY_RUN))
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate config, SDK/API and binds, then exit",
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--udp-color-host")
    parser.add_argument("--udp-color-port", type=int)
    parser.add_argument("--dist-interval-s", type=float)
    parser.add_argument("--deadzone", type=float)
    parser.add_argument("--watchdog-s", type=float)
    parser.add_argument("--step-interval-s", type=float)
    parser.add_argument("--cmd-cooldown-s", type=float)
    parser.add_argument("--sonar-sim")
    parser.add_argument("--sonar-failure-limit", type=int)
    parser.add_argument("--sonar-warn-interval-s", type=float)

    required = parser.add_mutually_exclusive_group()
    required.add_argument("--require-sonar", dest="require_sonar", action="store_true")
    required.add_argument("--no-require-sonar", dest="require_sonar", action="store_false")
    parser.set_defaults(require_sonar=None)

    mixed = parser.add_mutually_exclusive_group()
    mixed.add_argument(
        "--allow-sonar-sim-in-hardware",
        dest="allow_sonar_sim_in_hardware",
        action="store_true",
    )
    mixed.add_argument(
        "--no-allow-sonar-sim-in-hardware",
        dest="allow_sonar_sim_in_hardware",
        action="store_false",
    )
    parser.set_defaults(allow_sonar_sim_in_hardware=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args)
        runtime = prepare_runtime(config)
        if args.check:
            check_network_bindings(config)
            print(
                "[CHECK] OK: mode=%s, configuration, SDK/API and network binds are ready"
                % config.mode
            )
            return 0
    except (ConfigError, StartupError) as exc:
        print("[FATAL]", exc, file=sys.stderr)
        return 2

    service = TonyPiService(config=config, runtime=runtime)

    def _on_signal(signum: int, _frame: Any) -> None:
        if signum == getattr(signal, "SIGTERM", None):
            name = "SIGTERM"
        elif signum == getattr(signal, "SIGBREAK", None):
            name = "SIGBREAK"
        else:
            name = "SIGINT"
        print("\n[SYS] %s, shutting down..." % name)
        service.stop()

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)
    print("[SYS] %s mode" % config.mode)
    try:
        service.run()
    except (OSError, StartupError) as exc:
        print("[FATAL]", exc, file=sys.stderr)
        return 2
    print("[SYS] bye")
    return 1 if service.fault is not None else 0


if __name__ == "__main__":
    sys.exit(main())
