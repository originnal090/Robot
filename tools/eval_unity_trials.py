#!/usr/bin/env python3
"""Summarize Unity SimulationTrialRecorder JSONL files into JSON and optional CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any


class EvaluationError(ValueError):
    pass


@dataclass(frozen=True)
class Thresholds:
    max_final_distance_m: float = 0.35
    max_heading_error_deg: float = 15.0
    max_duration_s: float = 30.0


@dataclass
class SourceRecord:
    source: Path
    line_number: int
    payload: dict[str, Any]


@dataclass
class TrialAccumulator:
    trial_id: str
    scenario: str | None = None
    records: list[SourceRecord] = field(default_factory=list)


@dataclass(frozen=True)
class TrialMetrics:
    trial_id: str
    scenario: str | None
    success: bool
    collision: bool
    final_distance_m: float
    final_heading_error_deg: float
    duration_s: float
    path_length_m: float
    straight_line_efficiency: float | None
    minimum_clearance_m: float | None
    avoid_count: int
    false_arrival: bool
    controller_arrived: bool
    terminal_state: str | None
    source_files: list[str]


TRIAL_ID_KEYS = ("trial_id", "trialId", "trial", "session_id", "sessionId")
SCENARIO_KEYS = ("scenario", "scene", "case", "scenario_name")
TIME_KEYS = ("elapsed_s", "elapsedSeconds", "time_s", "sim_time_s", "timestamp_s", "timestamp")
DISTANCE_M_KEYS = (
    "true_distance_m",
    "trueDistanceM",
    "target_distance_m",
    "targetDistanceM",
    "final_distance_m",
    "finalDistanceM",
    "distance_m",
)
DISTANCE_MM_KEYS = ("true_distance_mm", "target_distance_mm", "final_distance_mm")
HEADING_KEYS = (
    "true_heading_error_deg",
    "trueHeadingErrorDeg",
    "heading_error_deg",
    "headingErrorDeg",
    "yaw_error_deg",
    "yawErrorDeg",
    "final_heading_error_deg",
    "finalHeadingErrorDeg",
)
CLEARANCE_M_KEYS = (
    "minimum_clearance_m",
    "minimumClearanceM",
    "min_clearance_m",
    "clearance_m",
)
CLEARANCE_MM_KEYS = ("minimum_clearance_mm", "min_clearance_mm", "clearance_mm")
STATE_KEYS = (
    "controller_state",
    "controllerState",
    "autonomy_state",
    "final_state",
    "state",
    "mode",
)


def finite_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError(f"{field_name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise EvaluationError(f"{field_name} must be finite, got {value!r}")
    return number


def optional_number(payload: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        if key in payload and payload[key] is not None:
            return finite_number(payload[key], field_name=key)
    return None


def optional_bool(payload: dict[str, Any], keys: Iterable[str]) -> bool | None:
    for key in keys:
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "yes", "no"}:
            return value.strip().lower() in {"true", "yes"}
        raise EvaluationError(f"{key} must be boolean, got {value!r}")
    return None


def first_value(payload: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def nested_dicts(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield payload
    for key in ("metrics", "summary", "truth", "ground_truth", "robot", "target", "status"):
        value = payload.get(key)
        if isinstance(value, dict):
            yield value


def find_number(payload: dict[str, Any], keys: Iterable[str]) -> float | None:
    for candidate in nested_dicts(payload):
        value = optional_number(candidate, keys)
        if value is not None:
            return value
    return None


def find_bool(payload: dict[str, Any], keys: Iterable[str]) -> bool | None:
    for candidate in nested_dicts(payload):
        value = optional_bool(candidate, keys)
        if value is not None:
            return value
    return None


def find_text(payload: dict[str, Any], keys: Iterable[str]) -> str | None:
    for candidate in nested_dicts(payload):
        value = first_value(candidate, keys)
        if value is not None:
            if not isinstance(value, str):
                raise EvaluationError(f"{next(key for key in keys if key in candidate)} must be text")
            text = value.strip()
            return text or None
    return None


def parse_time(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise EvaluationError(f"{field_name} must be seconds or an ISO timestamp")
    if isinstance(value, (int, float)):
        return finite_number(value, field_name=field_name)
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            try:
                return datetime.fromisoformat(text).timestamp()
            except ValueError as exc:
                raise EvaluationError(f"{field_name} is not seconds or ISO-8601: {value!r}") from exc
    raise EvaluationError(f"{field_name} must be seconds or an ISO timestamp")


def record_time(payload: dict[str, Any]) -> float | None:
    for candidate in nested_dicts(payload):
        for key in TIME_KEYS:
            if key in candidate and candidate[key] is not None:
                return parse_time(candidate[key], field_name=key)
    return None


def vector3(value: Any, *, field_name: str) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        try:
            return tuple(
                finite_number(value[axis], field_name=f"{field_name}.{axis}")
                for axis in ("x", "y", "z")
            )  # type: ignore[return-value]
        except KeyError as exc:
            raise EvaluationError(f"{field_name} must contain x, y, z") from exc
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(
            finite_number(component, field_name=f"{field_name}[{index}]")
            for index, component in enumerate(value)
        )  # type: ignore[return-value]
    raise EvaluationError(f"{field_name} must be {{x,y,z}} or [x,y,z]")


def find_position(payload: dict[str, Any], subject: str) -> tuple[float, float, float] | None:
    direct_keys = (
        f"{subject}_position",
        f"{subject}Position",
        f"{subject}_world_position",
        f"{subject}WorldPosition",
    )
    for key in direct_keys:
        if key in payload:
            return vector3(payload[key], field_name=key)
    if subject == "robot" and "position" in payload:
        return vector3(payload["position"], field_name="position")
    if subject == "robot" and "robot_position" not in payload and "position" not in payload:
        return None
    nested = payload.get(subject)
    if isinstance(nested, dict):
        for key in ("position", "world_position", "worldPosition"):
            if key in nested:
                return vector3(nested[key], field_name=f"{subject}.{key}")
    truth = payload.get("truth") or payload.get("ground_truth")
    if isinstance(truth, dict):
        return find_position(truth, subject)
    return None


def find_robot_yaw(payload: dict[str, Any]) -> float | None:
    value = find_number(payload, ("robot_yaw_deg", "robotYawDeg", "yaw_deg", "yawDeg"))
    if value is not None:
        return value
    robot = payload.get("robot")
    if isinstance(robot, dict):
        return optional_number(robot, ("yaw_deg", "yawDeg", "yaw"))
    return None


def normalize_angle_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def planar_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.hypot(b[0] - a[0], b[2] - a[2])


def compute_heading_error(
    robot_position: tuple[float, float, float],
    target_position: tuple[float, float, float],
    robot_yaw_deg: float,
) -> float:
    dx = target_position[0] - robot_position[0]
    dz = target_position[2] - robot_position[2]
    desired_yaw = math.degrees(math.atan2(dx, dz))
    return abs(normalize_angle_degrees(desired_yaw - robot_yaw_deg))


def collect_input_paths(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        paths = sorted(path for path in input_path.rglob("*.jsonl") if path.is_file())
        if not paths:
            raise EvaluationError(f"no .jsonl files found under {input_path}")
        return paths
    raise EvaluationError(f"input not found: {input_path}")


def load_records(paths: list[Path], *, allow_invalid: bool) -> tuple[list[SourceRecord], list[str]]:
    records: list[SourceRecord] = []
    warnings: list[str] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise EvaluationError(f"cannot read {path}: {exc}") from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise EvaluationError("JSON value is not an object")
            except (json.JSONDecodeError, EvaluationError) as exc:
                message = f"{path}:{line_number}: {exc}"
                if allow_invalid:
                    warnings.append(message)
                    continue
                raise EvaluationError(message) from exc
            records.append(SourceRecord(path, line_number, payload))
    if not records:
        raise EvaluationError("no valid JSONL records found")
    return records, warnings


def record_trial_id(record: SourceRecord, single_file: bool) -> str:
    value = first_value(record.payload, TRIAL_ID_KEYS)
    if value is None:
        if single_file:
            return record.source.stem
        raise EvaluationError(
            f"{record.source}:{record.line_number}: missing trial_id in a multi-file input"
        )
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise EvaluationError(f"{record.source}:{record.line_number}: trial_id must be text or int")
    text = str(value).strip()
    if not text:
        raise EvaluationError(f"{record.source}:{record.line_number}: trial_id is empty")
    return text


def group_trials(records: list[SourceRecord], *, single_file: bool) -> list[TrialAccumulator]:
    grouped: dict[str, TrialAccumulator] = {}
    for record in records:
        trial_id = record_trial_id(record, single_file)
        trial = grouped.setdefault(trial_id, TrialAccumulator(trial_id))
        scenario = first_value(record.payload, SCENARIO_KEYS)
        if scenario is not None:
            if not isinstance(scenario, str):
                raise EvaluationError(
                    f"{record.source}:{record.line_number}: scenario must be text"
                )
            if trial.scenario not in (None, scenario):
                raise EvaluationError(
                    f"trial {trial_id!r} has conflicting scenarios: "
                    f"{trial.scenario!r} and {scenario!r}"
                )
            trial.scenario = scenario
        trial.records.append(record)
    return list(grouped.values())


def last_available(records: list[SourceRecord], getter: Any) -> Any:
    for record in reversed(records):
        value = getter(record.payload)
        if value is not None:
            return value
    return None


def duration_seconds(records: list[SourceRecord]) -> float:
    explicit = last_available(
        records,
        lambda payload: find_number(payload, ("duration_s", "durationSeconds", "elapsed_s")),
    )
    times = [value for record in records if (value := record_time(record.payload)) is not None]
    derived = max(times) - min(times) if len(times) >= 2 else None
    duration = explicit if explicit is not None else derived
    if duration is None or duration < 0:
        raise EvaluationError("trial is missing a valid duration or at least two timestamps")
    return duration


def positions(records: list[SourceRecord], subject: str) -> list[tuple[float, float, float]]:
    result: list[tuple[float, float, float]] = []
    for record in records:
        value = find_position(record.payload, subject)
        if value is not None:
            result.append(value)
    return result


def path_length(records: list[SourceRecord]) -> float:
    explicit = last_available(
        records,
        lambda payload: find_number(
            payload, ("path_length_m", "pathLengthM", "distance_travelled_m")
        ),
    )
    if explicit is not None:
        if explicit < 0:
            raise EvaluationError("path_length_m must be >= 0")
        return explicit
    points = positions(records, "robot")
    if len(points) < 2:
        raise EvaluationError("trial is missing path_length_m or at least two robot positions")
    return sum(planar_distance(previous, current) for previous, current in pairwise(points))


def final_truth(records: list[SourceRecord]) -> tuple[float, float]:
    distance = last_available(records, lambda payload: find_number(payload, DISTANCE_M_KEYS))
    if distance is None:
        mm = last_available(records, lambda payload: find_number(payload, DISTANCE_MM_KEYS))
        distance = None if mm is None else mm / 1000.0
    final_robot = last_available(records, lambda payload: find_position(payload, "robot"))
    final_target = last_available(records, lambda payload: find_position(payload, "target"))
    if distance is None and final_robot is not None and final_target is not None:
        distance = planar_distance(final_robot, final_target)
    if distance is None or distance < 0:
        raise EvaluationError("trial is missing a valid final true target distance")

    heading = last_available(records, lambda payload: find_number(payload, HEADING_KEYS))
    yaw = last_available(records, find_robot_yaw)
    if heading is None and final_robot is not None and final_target is not None and yaw is not None:
        heading = compute_heading_error(final_robot, final_target, yaw)
    if heading is None:
        raise EvaluationError("trial is missing a final heading error or robot yaw/positions")
    return distance, abs(normalize_angle_degrees(heading))


def minimum_clearance(records: list[SourceRecord]) -> float | None:
    values: list[float] = []
    for record in records:
        value = find_number(record.payload, CLEARANCE_M_KEYS)
        if value is None:
            mm = find_number(record.payload, CLEARANCE_MM_KEYS)
            value = None if mm is None else mm / 1000.0
        if value is not None:
            if value < 0:
                raise EvaluationError("clearance must be >= 0")
            values.append(value)
    return min(values) if values else None


def count_avoids(records: list[SourceRecord]) -> int:
    counters: list[int] = []
    event_count = 0
    for record in records:
        value = find_number(record.payload, ("avoid_count", "avoidCount", "avoidance_count"))
        if value is not None:
            if value < 0 or not value.is_integer():
                raise EvaluationError("avoid_count must be a non-negative integer")
            counters.append(int(value))
        event = find_text(record.payload, ("event", "event_type", "type"))
        if event and event.lower() in {"avoid", "avoidance", "obstacle_avoid"}:
            event_count += 1
    return max(counters, default=event_count)


def collision_occurred(records: list[SourceRecord]) -> bool:
    for record in records:
        if str(record.payload.get("type", "")).lower() == "collision":
            return True
        collision = find_bool(record.payload, ("collision", "collided", "had_collision"))
        if collision:
            return True
        count = find_number(record.payload, ("collision_count", "collisionCount"))
        if count is not None:
            if count < 0 or not count.is_integer():
                raise EvaluationError("collision_count must be a non-negative integer")
            if count > 0:
                return True
    return False


def terminal_state(records: list[SourceRecord]) -> str | None:
    state = last_available(records, lambda payload: find_text(payload, STATE_KEYS))
    return state.upper() if state else None


def explicit_success(records: list[SourceRecord]) -> bool | None:
    value = last_available(records, lambda payload: find_bool(payload, ("success", "passed")))
    if value is not None:
        return value
    result = last_available(records, lambda payload: find_text(payload, ("result", "outcome")))
    if result is None:
        return None
    normalized = result.strip().lower()
    if normalized in {"success", "passed", "pass"}:
        return True
    if normalized in {"failed", "failure", "fail", "timeout", "collision", "blocked"}:
        return False
    return None


def evaluate_trial(trial: TrialAccumulator, thresholds: Thresholds) -> TrialMetrics:
    records = trial.records
    final_distance, heading_error = final_truth(records)
    duration = duration_seconds(records)
    length = path_length(records)
    collision = collision_occurred(records)
    state = terminal_state(records)
    arrived = state == "ARRIVED" or any(
        find_bool(record.payload, ("arrived", "controller_arrived")) is True for record in records
    )
    truth_pass = (
        final_distance <= thresholds.max_final_distance_m
        and heading_error <= thresholds.max_heading_error_deg
        and duration <= thresholds.max_duration_s
        and not collision
    )
    reported_success = explicit_success(records)
    success = truth_pass and arrived if reported_success is None else reported_success and truth_pass
    false_arrival = arrived and not truth_pass

    initial_distance = None
    for record in records:
        initial_distance = find_number(record.payload, DISTANCE_M_KEYS)
        if initial_distance is None:
            mm = find_number(record.payload, DISTANCE_MM_KEYS)
            initial_distance = None if mm is None else mm / 1000.0
        if initial_distance is not None:
            break
    if initial_distance is None:
        first_robot = next((find_position(record.payload, "robot") for record in records if find_position(record.payload, "robot") is not None), None)
        first_target = next((find_position(record.payload, "target") for record in records if find_position(record.payload, "target") is not None), None)
        if first_robot is not None and first_target is not None:
            initial_distance = planar_distance(first_robot, first_target)
    efficiency = None
    if initial_distance is not None and length > 0:
        efficiency = min(1.0, max(0.0, initial_distance / length))

    return TrialMetrics(
        trial_id=trial.trial_id,
        scenario=trial.scenario,
        success=success,
        collision=collision,
        final_distance_m=final_distance,
        final_heading_error_deg=heading_error,
        duration_s=duration,
        path_length_m=length,
        straight_line_efficiency=efficiency,
        minimum_clearance_m=minimum_clearance(records),
        avoid_count=count_avoids(records),
        false_arrival=false_arrival,
        controller_arrived=arrived,
        terminal_state=state,
        source_files=sorted({str(record.source) for record in records}),
    )


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def summarize(metrics: list[TrialMetrics]) -> dict[str, Any]:
    if not metrics:
        raise EvaluationError("no trials to summarize")

    def mean_of(attribute: str) -> float | None:
        values = [getattr(item, attribute) for item in metrics]
        numeric = [value for value in values if value is not None]
        return rounded(statistics.fmean(numeric)) if numeric else None

    total = len(metrics)
    successful = sum(item.success for item in metrics)
    collisions = sum(item.collision for item in metrics)
    false_arrivals = sum(item.false_arrival for item in metrics)
    aggregate = {
        "trial_count": total,
        "success_count": successful,
        "success_rate": rounded(successful / total),
        "collision_count": collisions,
        "collision_rate": rounded(collisions / total),
        "false_arrival_count": false_arrivals,
        "false_arrival_rate": rounded(false_arrivals / total),
        "mean_final_distance_m": mean_of("final_distance_m"),
        "mean_final_heading_error_deg": mean_of("final_heading_error_deg"),
        "mean_duration_s": mean_of("duration_s"),
        "mean_path_length_m": mean_of("path_length_m"),
        "mean_straight_line_efficiency": mean_of("straight_line_efficiency"),
        "minimum_clearance_m": rounded(
            min(
                (item.minimum_clearance_m for item in metrics if item.minimum_clearance_m is not None),
                default=None,
            )
        ),
        "total_avoid_count": sum(item.avoid_count for item in metrics),
        "mean_avoid_count": rounded(statistics.fmean(item.avoid_count for item in metrics)),
    }

    scenarios: dict[str, list[TrialMetrics]] = defaultdict(list)
    for item in metrics:
        scenarios[item.scenario or "(unspecified)"].append(item)
    by_scenario = {
        name: {
            "trial_count": len(items),
            "success_rate": rounded(sum(item.success for item in items) / len(items)),
            "collision_rate": rounded(sum(item.collision for item in items) / len(items)),
            "false_arrival_rate": rounded(sum(item.false_arrival for item in items) / len(items)),
        }
        for name, items in sorted(scenarios.items())
    }
    return {
        "aggregate": aggregate,
        "by_scenario": by_scenario,
        "trials": [
            {
                **asdict(item),
                "final_distance_m": rounded(item.final_distance_m),
                "final_heading_error_deg": rounded(item.final_heading_error_deg),
                "duration_s": rounded(item.duration_s),
                "path_length_m": rounded(item.path_length_m),
                "straight_line_efficiency": rounded(item.straight_line_efficiency),
                "minimum_clearance_m": rounded(item.minimum_clearance_m),
            }
            for item in metrics
        ],
    }


def evaluate_inputs(
    input_path: Path,
    *,
    thresholds: Thresholds,
    allow_invalid: bool = False,
) -> dict[str, Any]:
    paths = collect_input_paths(input_path)
    records, warnings = load_records(paths, allow_invalid=allow_invalid)
    trials = group_trials(records, single_file=len(paths) == 1)
    metrics: list[TrialMetrics] = []
    trial_errors: list[str] = []
    for trial in trials:
        try:
            metrics.append(evaluate_trial(trial, thresholds))
        except EvaluationError as exc:
            message = f"trial {trial.trial_id!r}: {exc}"
            if allow_invalid:
                trial_errors.append(message)
                continue
            raise EvaluationError(message) from exc
    if not metrics:
        raise EvaluationError("no valid trials could be evaluated")
    report = summarize(metrics)
    report["input"] = {
        "path": str(input_path),
        "files": [str(path) for path in paths],
        "invalid_records": warnings,
        "invalid_trials": trial_errors,
    }
    report["thresholds"] = asdict(thresholds)
    return report


def write_csv(path: Path, trials: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "trial_id",
        "scenario",
        "success",
        "collision",
        "final_distance_m",
        "final_heading_error_deg",
        "duration_s",
        "path_length_m",
        "straight_line_efficiency",
        "minimum_clearance_m",
        "avoid_count",
        "false_arrival",
        "controller_arrived",
        "terminal_state",
        "source_files",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for trial in trials:
            row = dict(trial)
            row["source_files"] = ";".join(row["source_files"])
            writer.writerow({field: row.get(field) for field in fields})


def positive_finite(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("value must be finite and > 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="recorder JSONL file or directory")
    parser.add_argument(
        "--json-out", type=Path, default=None, help="write JSON report here (default: stdout)"
    )
    parser.add_argument("--csv-out", type=Path, default=None, help="optional per-trial CSV output")
    parser.add_argument(
        "--max-final-distance-m", type=positive_finite, default=0.35, help="truth pass threshold"
    )
    parser.add_argument(
        "--max-heading-error-deg", type=positive_finite, default=15.0, help="truth pass threshold"
    )
    parser.add_argument(
        "--max-duration-s", type=positive_finite, default=30.0, help="truth pass threshold"
    )
    parser.add_argument(
        "--allow-invalid",
        action="store_true",
        help="skip malformed records/trials and include diagnostics in the report",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    thresholds = Thresholds(
        max_final_distance_m=args.max_final_distance_m,
        max_heading_error_deg=args.max_heading_error_deg,
        max_duration_s=args.max_duration_s,
    )
    try:
        report = evaluate_inputs(
            args.input,
            thresholds=thresholds,
            allow_invalid=args.allow_invalid,
        )
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.json_out is None:
            sys.stdout.write(text)
        else:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(text, encoding="utf-8")
            print(f"JSON report written to {args.json_out}", file=sys.stderr)
        if args.csv_out is not None:
            write_csv(args.csv_out, report["trials"])
            print(f"CSV report written to {args.csv_out}", file=sys.stderr)
    except (EvaluationError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
