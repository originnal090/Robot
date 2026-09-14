"""Offline LAB capture diagnostics. Never connects to robot hardware."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import cv2
import numpy as np

from hcirobot.detector import DetectorConfig, RedBallDetector


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, default=Path("artifacts/lab-tuning-20260914"))
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--sweep", action="store_true", help="Compare conservative A thresholds")
    parser.add_argument("--annotations", type=Path, help="Manual boxes with train/test split")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    sweep_rows = []
    summary = []
    for capture in args.captures:
        files = sorted(capture.glob("*.png"))
        if not files or args.samples < 1:
            raise ValueError("Each capture needs PNG frames and samples must be positive")
        chosen = set(np.linspace(0, len(files) - 1, args.samples, dtype=int).tolist())
        tiles = []
        # Pin the historic baseline even when shipped defaults change.
        detector = RedBallDetector(replace(DetectorConfig(), lab_min=(30, 132, 100)))
        variants = {
            f"a{value}": RedBallDetector(replace(DetectorConfig(), lab_min=(30, value, 100)))
            for value in ([136, 140, 145, 150] if args.sweep else [136])
        }
        hits = 0
        hashes = set()
        for index, path in enumerate(files):
            hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
            frame = cv2.imread(str(path))
            if frame is None:
                raise ValueError(f"Cannot read {path}")
            detection = detector.process(frame)
            rows.append({"capture": capture.name, "frame": path.name, **asdict(detection)})
            hits += detection.candidate_detected
            for label, variant in variants.items():
                result = variant.process(frame)
                sweep_rows.append(
                    {
                        "capture": capture.name,
                        "frame": path.name,
                        "variant": label,
                        **asdict(result),
                    }
                )
            if index in chosen:
                tile = cv2.resize(frame, (320, 240))
                if detection.candidate_detected:
                    sx, sy = 320 / frame.shape[1], 240 / frame.shape[0]
                    cv2.circle(
                        tile,
                        (round(detection.center_x * sx), round(detection.center_y * sy)),
                        round(detection.radius * (sx + sy) / 2),
                        (0, 255, 0),
                        1,
                    )
                cv2.rectangle(tile, (0, 0), (320, 22), (0, 0, 0), -1)
                cv2.putText(
                    tile, path.stem, (5, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1
                )
                tiles.append(tile)
        for page, start in enumerate(range(0, len(tiles), 20)):
            subset = tiles[start : start + 20]
            subset += [np.zeros((240, 320, 3), np.uint8)] * ((-len(subset)) % 4)
            sheet = np.vstack([np.hstack(subset[i : i + 4]) for i in range(0, len(subset), 4)])
            cv2.imwrite(str(args.output / f"{capture.name}-sheet-{page}.jpg"), sheet)
        stats = {
            "capture": capture.name,
            "frames": len(files),
            "baseline_candidates": hits,
            "unique_file_sha256": len(hashes),
            "exact_duplicate_frames": len(files) - len(hashes),
        }
        for label in variants:
            stats[label + "_candidates"] = sum(
                row["candidate_detected"]
                for row in sweep_rows
                if row["capture"] == capture.name and row["variant"] == label
            )
        summary.append(stats)
        print(json.dumps(stats), flush=True)
    with (args.output / "baseline.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    if sweep_rows:
        with (args.output / "sweep.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(sweep_rows[0]))
            writer.writeheader()
            writer.writerows(sweep_rows)
    report = {"replay": summary, "warning": "Candidate counts are not labeled recall."}
    if args.annotations:
        labels = json.loads(args.annotations.read_text(encoding="utf-8"))
        lookup = {(r["capture"], r["frame"], "baseline"): r for r in rows}
        lookup.update({(r["capture"], r["frame"], r["variant"]): r for r in sweep_rows})
        metrics = []
        # A136 was selected on train labels; evaluate only it and baseline on held-out test.
        for variant in ["baseline", "a136"]:
            for split in ["train", "test"]:
                subset = [item for item in labels if item["split"] == split]
                scores = []
                errors = []
                fp = 0
                for item in subset:
                    result = lookup[item["capture"], item["frame"], variant]
                    truth = item["box"]
                    if not truth:
                        fp += result["candidate_detected"]
                        continue
                    score = 0.0
                    if result["candidate_detected"]:
                        x, y, radius = result["center_x"], result["center_y"], result["radius"]
                        predicted = [
                            max(0, x - radius),
                            max(0, y - radius),
                            min(result["frame_width"], x + radius),
                            min(result["frame_height"], y + radius),
                        ]
                        score = box_iou(predicted, truth)
                        errors.append(
                            float(
                                np.hypot(
                                    x - (truth[0] + truth[2]) / 2, y - (truth[1] + truth[3]) / 2
                                )
                            )
                        )
                    scores.append(score)
                metrics.append(
                    {
                        "variant": variant,
                        "split": split,
                        "positive_frames": len(scores),
                        "negative_frames": len(subset) - len(scores),
                        "tp_iou_03": sum(value >= 0.3 for value in scores),
                        "tp_iou_05": sum(value >= 0.5 for value in scores),
                        "fp_negative_frames": fp,
                        "mean_center_error_detected_px": float(np.mean(errors)) if errors else None,
                    }
                )
        report["manual_label_evaluation"] = metrics
    (args.output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def box_iou(box: list[float], truth: list[float]) -> float:
    x1, y1, x2, y2 = box
    a1, b1, a2, b2 = truth
    intersection = max(0, min(x2, a2) - max(x1, a1)) * max(0, min(y2, b2) - max(y1, b1))
    union = (x2 - x1) * (y2 - y1) + (a2 - a1) * (b2 - b1) - intersection
    return intersection / union if union else 0.0


if __name__ == "__main__":
    main()
