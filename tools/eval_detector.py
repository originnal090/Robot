"""Offline evaluation of a red-ball detector against captured frames.

Runs the detector over every image in ``--frames`` (sorted by name), writes
annotated frames at the requested sampling interval plus a ``summary.json``
containing per-frame results and aggregate stability metrics (detection rate,
run-length distribution of consecutive hits, mean center jitter, and timing).

Example:
    uv run python tools/eval_detector.py --frames artifacts/tonypi-video \
        --out artifacts/eval-output/after
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, replace
from itertools import pairwise
from pathlib import Path

import cv2

from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.edge_detector import EdgeBallDetector

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp"}


def parse_lab_triple(text: str) -> tuple[int, int, int]:
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"expected 'L,A,B', got {text!r}")
    try:
        values = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integers, got {text!r}") from exc
    return values  # type: ignore[return-value]


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--frames", type=Path, required=True, help="directory of frame images")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--edge-model", type=Path, help="optional exported edge SVM model")
    parser.add_argument(
        "--save-every",
        type=int,
        default=1,
        help="save every Nth annotated frame (all frames are still evaluated)",
    )
    parser.add_argument("--lab-min", type=parse_lab_triple, default=None, help="LAB lower bound, e.g. 55,145,118")
    parser.add_argument("--lab-max", type=parse_lab_triple, default=None, help="LAB upper bound, e.g. 190,195,150")
    parser.add_argument("--min-area", type=float, default=None, help="minimum contour area override")
    parser.add_argument("--circularity", type=float, default=None, help="minimum circularity override")
    parser.add_argument("--aspect-min", type=float, default=None, help="minimum aspect ratio override")
    parser.add_argument("--aspect-max", type=float, default=None, help="maximum aspect ratio override")
    parser.add_argument("--confirm", type=int, default=None, help="confirmation frames override")
    parser.add_argument("--release", type=int, default=None, help="release frames override")
    parser.add_argument("--blur", type=int, default=None, help="gaussian blur kernel override")
    parser.add_argument("--morph", type=int, default=None, help="morphology kernel override")
    return parser


def build_config(args: argparse.Namespace) -> DetectorConfig:
    overrides: dict[str, object] = {}
    if args.lab_min is not None:
        overrides["lab_min"] = args.lab_min
    if args.lab_max is not None:
        overrides["lab_max"] = args.lab_max
    if args.min_area is not None:
        overrides["minimum_contour_area"] = args.min_area
    if args.circularity is not None:
        overrides["minimum_circularity"] = args.circularity
    if args.aspect_min is not None:
        overrides["minimum_aspect_ratio"] = args.aspect_min
    if args.aspect_max is not None:
        overrides["maximum_aspect_ratio"] = args.aspect_max
    if args.confirm is not None:
        overrides["confirmation_frames"] = args.confirm
    if args.release is not None:
        overrides["release_frames"] = args.release
    if args.blur is not None:
        overrides["gaussian_blur_kernel"] = args.blur
    if args.morph is not None:
        overrides["morphology_kernel"] = args.morph
    config = DetectorConfig()
    return replace(config, **overrides) if overrides else config  # type: ignore[arg-type]


def collect_frame_paths(frames_dir: Path) -> list[Path]:
    if not frames_dir.is_dir():
        raise SystemExit(f"frames directory not found: {frames_dir}")
    return sorted(path for path in frames_dir.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES)


def run_lengths(flags: list[bool]) -> list[int]:
    runs: list[int] = []
    current = 0
    for flag in flags:
        if flag:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


def distribution(runs: list[int]) -> dict[str, int]:
    result: dict[str, int] = {}
    for length in runs:
        key = str(length)
        result[key] = result.get(key, 0) + 1
    return result


def center_jitter(records: list[dict]) -> dict[str, float | int | None]:
    pairs: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for previous, current in pairwise(records):
        if previous["center"] is None or current["center"] is None:
            continue
        pairs.append((tuple(previous["center"]), tuple(current["center"])))  # type: ignore[arg-type]
    if not pairs:
        return {"pairs": 0, "mean": None, "max": None, "mean_dx": None, "mean_dy": None}
    distances = [abs(complex(b[0] - a[0], b[1] - a[1])) for a, b in pairs]
    return {
        "pairs": len(pairs),
        "mean": round(statistics.mean(distances), 3),
        "max": round(max(distances), 3),
        "mean_dx": round(statistics.mean(abs(b[0] - a[0]) for a, b in pairs), 3),
        "mean_dy": round(statistics.mean(abs(b[1] - a[1]) for a, b in pairs), 3),
    }


def annotate(frame, detection, name: str):
    output = frame.copy()
    if detection.center_x is not None and detection.center_y is not None and detection.radius:
        center = (round(detection.center_x), round(detection.center_y))
        radius = max(2, round(detection.radius))
        color = (0, 255, 0) if detection.detected else (0, 165, 255)
        cv2.circle(output, center, radius, color, 2)
        cv2.drawMarker(output, center, color, cv2.MARKER_CROSS, 12, 2)
    label = f"{name} det={int(detection.detected)} cand={int(detection.candidate_detected)}"
    cv2.putText(output, label, (8, output.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
    cv2.putText(output, label, (8, output.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1)
    return output


def evaluate(args: argparse.Namespace) -> dict:
    config = build_config(args)
    if args.save_every < 1:
        raise SystemExit("--save-every must be positive")
    detector = (
        EdgeBallDetector(args.edge_model, config)
        if args.edge_model is not None
        else RedBallDetector(config)
    )
    frame_paths = collect_frame_paths(args.frames)
    if not frame_paths:
        raise SystemExit(f"no frame images found in {args.frames}")
    args.out.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    skipped: list[str] = []
    elapsed_ms: list[float] = []
    for index, path in enumerate(frame_paths):
        frame = cv2.imread(str(path))
        if frame is None:
            skipped.append(path.name)
            continue
        started = time.perf_counter()
        detection = detector.process(frame)
        elapsed_ms.append((time.perf_counter() - started) * 1000)
        center = None
        if detection.center_x is not None and detection.center_y is not None:
            center = [detection.center_x, detection.center_y]
        records.append(
            {
                "frame": path.name,
                "candidate": bool(detection.candidate_detected),
                "detected": bool(detection.detected),
                "center": center,
                "radius": detection.radius,
                "score": detection.score,
                "area": detection.area,
                "circularity": detection.circularity,
                "aspect_ratio": detection.aspect_ratio,
            }
        )
        if index % args.save_every == 0 or index == len(frame_paths) - 1:
            cv2.imwrite(str(args.out / path.name), annotate(frame, detection, path.name))

    candidate_flags = [record["candidate"] for record in records]
    detected_flags = [record["detected"] for record in records]
    candidate_runs = run_lengths(candidate_flags)
    detected_runs = run_lengths(detected_flags)

    summary = {
        "frames_dir": str(args.frames),
        "detector": {
            "type": "edge_svm" if args.edge_model is not None else "lab",
            "model": str(args.edge_model) if args.edge_model is not None else None,
            "model_bytes": args.edge_model.stat().st_size if args.edge_model is not None else None,
        },
        "total_frames": len(records),
        "skipped": skipped,
        "saved_every_n_frames": args.save_every,
        "config": asdict(config),
        "aggregate": {
            "candidate_frames": sum(candidate_flags),
            "candidate_rate": round(sum(candidate_flags) / len(records), 4),
            "detected_frames": sum(detected_flags),
            "detected_rate": round(sum(detected_flags) / len(records), 4),
            "candidate_runs": {
                "count": len(candidate_runs),
                "longest": max(candidate_runs, default=0),
                "distribution": distribution(candidate_runs),
            },
            "detected_runs": {
                "count": len(detected_runs),
                "longest": max(detected_runs, default=0),
                "distribution": distribution(detected_runs),
            },
            "center_jitter_px": center_jitter(records),
            "inference_ms": {
                "mean": round(statistics.mean(elapsed_ms), 3),
                "median": round(statistics.median(elapsed_ms), 3),
                "p95": round(sorted(elapsed_ms)[round(0.95 * (len(elapsed_ms) - 1))], 3),
                "max": round(max(elapsed_ms), 3),
            },
        },
        "per_frame": records,
    }
    summary_path = args.out / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    args = build_argument_parser().parse_args()
    summary = evaluate(args)
    aggregate = summary["aggregate"]
    print(f"frames: {summary['total_frames']} (skipped {len(summary['skipped'])})")
    print(
        f"candidate: {aggregate['candidate_frames']}/{summary['total_frames']} "
        f"({aggregate['candidate_rate']:.1%})"
    )
    print(
        f"detected:  {aggregate['detected_frames']}/{summary['total_frames']} "
        f"({aggregate['detected_rate']:.1%})"
    )
    print(f"candidate runs: {aggregate['candidate_runs']}")
    print(f"detected runs:  {aggregate['detected_runs']}")
    print(f"center jitter:  {aggregate['center_jitter_px']}")
    print(f"inference ms:   {aggregate['inference_ms']}")
    print(f"summary written to {args.out / 'summary.json'}")


if __name__ == "__main__":
    main()
