"""Retrain the lightweight route SVM with the reviewed 2026-09-15 captures.

This intentionally keeps the existing feature-v1 and hybrid runtime. It does not
train or load CNN/YOLO models and does not change robot or navigation behavior.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from train_edge_detector import (
    evaluate,
    fit_model,
    load_sample_rows,
    training_data,
)
from train_route_models import augmentation_data

from hcirobot.detector import DetectorConfig
from hcirobot.edge_detector import EdgeBallDetector
from hcirobot.hybrid_detector import HybridBallDetector

ROOT = Path(__file__).resolve().parents[1]
BASE_ANNOTATIONS = ROOT / "data/red-ball-route-20260914/annotations-balanced.json"
NEW_ANNOTATIONS = ROOT / "data/red-ball-route-20260915/annotations-new.json"
BASE_MODEL = ROOT / "artifacts/edge-model-20260914/red_ball_svm.npz"
BASE_PROFILE = ROOT / "artifacts/edge-route-20260914/route-balanced-v1/profile.json"
DEFAULT_OUTPUT = ROOT / "artifacts/edge-route-20260915/route-balanced-a146-v2"


def _lab_negative_features(samples: list[dict]) -> np.ndarray:
    """Mine proposal-stage negatives from every reviewed negative training frame."""
    detector = HybridBallDetector(BASE_MODEL, DetectorConfig())
    features = []
    for row in samples:
        if row["split"] == "train" and row["box"] is None:
            features.extend(
                detector.extractor.extract(row["image"], box)
                for box in detector._lab_boxes(row["image"], None)
            )
    return np.stack(features) if features else np.empty((0, 406), np.float32)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-epochs", type=int, default=3000)
    args = parser.parse_args()
    if args.max_epochs < 1:
        parser.error("--max-epochs must be positive")
    if args.output.exists():
        parser.error("output already exists; choose a new version directory")

    cv2.setNumThreads(1)
    rows = json.loads(BASE_ANNOTATIONS.read_text(encoding="utf-8"))
    rows.extend(json.loads(NEW_ANNOTATIONS.read_text(encoding="utf-8")))
    samples = load_sample_rows(rows, ROOT / "artifacts/captures")
    x, y, crop_counts = training_data(samples, feature_version=1)
    augmented_x, augmented_y, provenance = augmentation_data(samples)
    lab_negative_x = _lab_negative_features(samples)
    x = np.concatenate((x, augmented_x, lab_negative_x))
    y = np.concatenate(
        (
            y,
            augmented_y,
            -np.ones(len(lab_negative_x), dtype=np.int32),
        )
    )

    args.output.mkdir(parents=True)
    model_path = args.output / "red_ball_svm.npz"
    fitting = fit_model(x, y, model_path, feature_version=1, max_epochs=args.max_epochs)
    model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
    profile = json.loads(BASE_PROFILE.read_text(encoding="utf-8"))
    profile.update(
        id="hybrid-route-balanced-a146-v2-fast",
        model="red_ball_svm.npz",
        model_sha256=model_sha256,
        model_bytes=model_path.stat().st_size,
    )
    profile["detector_config"]["lab_min"][1] = 146
    (args.output / "profile.json").write_text(
        json.dumps(profile, indent=2), encoding="utf-8"
    )

    detector = EdgeBallDetector(model_path, DetectorConfig(lab_min=(30, 146, 100)))
    report = {
        "id": profile["id"],
        "base_annotations": str(BASE_ANNOTATIONS.relative_to(ROOT)),
        "new_annotations": str(NEW_ANNOTATIONS.relative_to(ROOT)),
        "base_annotations_sha256": hashlib.sha256(BASE_ANNOTATIONS.read_bytes()).hexdigest(),
        "new_annotations_sha256": hashlib.sha256(NEW_ANNOTATIONS.read_bytes()).hexdigest(),
        "model_sha256": model_sha256,
        "training": fitting,
        "original_crop_counts": crop_counts,
        "added_positive_augmentation_crops": len(augmented_x),
        "added_lab_negative_crops": len(lab_negative_x),
        "augmentation_provenance": provenance,
        "train": evaluate(detector, samples, "train"),
        "test": evaluate(detector, samples, "test"),
        "split_policy": (
            "The 22 new reviewed rows are development/training data. The prior test split "
            "remains excluded from fitting. Exact image hashes across train/test are rejected."
        ),
        "limitations": [
            "The new captures were inspected during tuning and are not an unbiased holdout.",
            "The four no-ball and two severe-blur frames come from one same-room run.",
            "No CNN/YOLO runtime, navigation, or robot-control behavior is changed.",
            "Latency measurements in the companion report are PC-only.",
        ],
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "profile": str(args.output / "profile.json"),
                "training": fitting,
                "test": {key: value for key, value in report["test"].items()
                         if key != "predictions"},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
