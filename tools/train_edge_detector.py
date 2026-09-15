"""Train and evaluate the small red-ball SVM using manually labeled captures.

Example: python tools/train_edge_detector.py --annotations artifacts/ball-annotations.json
Annotations: [{"capture": "capture-...", "frame": "frame-00001.png",
               "box": [x1,y1,x2,y2] or null, "split": "train" or "test"}]
All boxes are exclusive at x2/y2. Test labels never enter model fitting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from pathlib import Path

import cv2
import numpy as np

from hcirobot.detector import DetectorConfig
from hcirobot.edge_detector import (
    FEATURE_VERSION,
    SUPPORTED_FEATURE_VERSIONS,
    EdgeBallDetector,
    FeatureExtractor,
    box_iou,
    proposal_boxes,
)


def load_sample_rows(rows: list[dict], root: Path) -> list[dict]:
    """Load and validate already-decoded annotation rows."""
    samples = []
    for row in rows:
        if row["split"] not in ("train", "test"):
            raise ValueError("split must be train or test")
        image_path = root / row["capture"] / row["frame"]
        image = cv2.imread(str(image_path))
        if image is None:
            raise ValueError(f"could not read {image_path}")
        h, w = image.shape[:2]
        box = row["box"]
        if box is not None:
            if len(box) != 4 or not (0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h):
                raise ValueError(f"invalid annotation box: {row}")
            box = tuple(round(v * (640 / w if i % 2 == 0 else 480 / h))
                        for i, v in enumerate(box))
        samples.append(dict(row, image=cv2.resize(image, (640, 480)), box=box,
                            sha256=hashlib.sha256(image_path.read_bytes()).hexdigest()))
    if not any(row["split"] == "train" for row in samples):
        raise ValueError("at least one train sample is required")
    if not any(row["split"] == "test" for row in samples):
        raise ValueError("at least one held-out test sample is required")
    train_hashes = {row["sha256"] for row in samples if row["split"] == "train"}
    test_hashes = {row["sha256"] for row in samples if row["split"] == "test"}
    if train_hashes & test_hashes:
        raise ValueError("identical image content appears in both train and test splits")
    return samples


def load_samples(path: Path, root: Path) -> list[dict]:
    return load_sample_rows(json.loads(path.read_text(encoding="utf-8")), root)


def training_data(samples: list[dict], feature_version: int = FEATURE_VERSION
                  ) -> tuple[np.ndarray, np.ndarray, dict]:
    extractor = FeatureExtractor(version=feature_version)
    rng = np.random.default_rng(20260914)
    features, labels = [], []
    for row in samples:
        if row["split"] != "train":
            continue
        image, truth = row["image"], row["box"]
        boxes = proposal_boxes(image)
        if truth is not None:
            boxes.append(truth)
            x1, y1, x2, y2 = truth
            w, h = x2-x1, y2-y1
            for _ in range(12):
                jitter = rng.uniform(-.12, .12, 4)
                boxes.append((max(0, round(x1+jitter[0]*w)),
                              max(0, round(y1+jitter[1]*h)),
                              min(640, round(x2+jitter[2]*w)),
                              min(480, round(y2+jitter[3]*h))))
        # Background samples of several scales; exclude even a partial ball overlap.
        for _ in range(64):
            w = int(rng.integers(12, 160))
            h = max(8, min(220, round(w * rng.uniform(.65, 1.5))))
            x, y = int(rng.integers(0, 641-w)), int(rng.integers(0, 481-h))
            boxes.append((x, y, x+w, y+h))
        for box in boxes:
            iou = box_iou(box, truth) if truth is not None else 0
            if truth is not None and iou >= .5:
                label = 1
            elif iou == 0:
                label = -1
            else:
                continue
            features.append(extractor.extract(image, box))
            labels.append(label)
    x, y = np.asarray(features, np.float32), np.asarray(labels, np.int32)
    if set(y.tolist()) != {-1, 1}:
        raise ValueError("training requires positive and negative examples")
    return x, y, {"positive_crops": int((y == 1).sum()), "negative_crops": int((y == -1).sum())}


def fit_model(x: np.ndarray, y: np.ndarray, path: Path,
              feature_version: int = FEATURE_VERSION, max_epochs: int = 1000) -> dict:
    if path.exists():
        raise FileExistsError(f'refusing to overwrite model version: {path}')
    if max_epochs < 1:
        raise ValueError("max_epochs must be positive")
    FeatureExtractor(version=feature_version)  # reject unsupported export versions before fitting
    # OpenCV 5's minimal wheels can omit cv2.ml as well as HOGDescriptor.
    # Dual coordinate descent for L2-regularized squared-hinge linear SVM:
    # min 0.5||w||² + sum(C_i * max(0, 1-y_i*w*x_i)²), with regularized bias.
    extended = np.column_stack((x, np.ones(len(x), np.float32))).astype(np.float64)
    costs = np.asarray([len(y)/(2*(y == label).sum()) for label in y])
    diagonal = np.einsum("ij,ij->i", extended, extended) + 1/(2*costs)
    alpha = np.zeros(len(y))
    solution = np.zeros(extended.shape[1])
    rng = np.random.default_rng(20260914)
    max_gradient = float("inf")
    for epoch in range(max_epochs):
        max_gradient = 0.0
        for i in rng.permutation(len(y)):
            gradient = y[i] * np.dot(solution, extended[i]) - 1 + alpha[i]/(2*costs[i])
            projected = min(gradient, 0) if alpha[i] == 0 else gradient
            max_gradient = max(max_gradient, abs(projected))
            if abs(projected) < 1e-10:
                continue
            updated = max(0, alpha[i] - gradient/diagonal[i])
            solution += (updated-alpha[i]) * y[i] * extended[i]
            alpha[i] = updated
        if max_gradient < 1e-4:
            break
    weights, bias = solution[:-1].astype(np.float32), float(solution[-1])
    np.savez_compressed(path, weights=weights, bias=np.float32(bias),
                        threshold=np.float32(0), feature_version=np.int32(feature_version))
    return {"feature_version": feature_version,
            "feature_count": x.shape[1], "model_bytes": path.stat().st_size,
            "train_crop_accuracy": float(np.mean((x @ weights + bias >= 0) == (y == 1))),
            "svm_c": 1.0, "threshold": 0.0,
            "solver": "NumPy dual coordinate descent, L2 squared hinge, regularized bias",
            "solver_epochs": epoch+1, "solver_max_projected_gradient": float(max_gradient),
            "solver_converged": bool(max_gradient < 1e-4)}


def evaluate(detector: EdgeBallDetector, samples: list[dict], split: str) -> dict:
    rows, elapsed = [], []
    for row in samples:
        if row["split"] != split:
            continue
        image, truth = row["image"], row["box"]
        proposals = proposal_boxes(image)
        before = time.perf_counter()
        predictions = detector.predict_boxes(image)
        elapsed.append((time.perf_counter()-before)*1000)
        predicted = predictions[0][0] if predictions else None
        rows.append({"capture": row["capture"], "frame": row["frame"], "truth": truth,
                     "prediction": predicted, "score": predictions[0][1] if predictions else None,
                     "iou": box_iou(predicted, truth) if predicted and truth else 0,
                     "proposal_best_iou": max((box_iou(p, truth) for p in proposals), default=0)
                     if truth else 0, "proposal_count": len(proposals)})
    positives = sum(row["truth"] is not None for row in rows)
    predicted_count = sum(row["prediction"] is not None for row in rows)
    metrics = {}
    for threshold in (.3, .5):
        tp = sum(row["truth"] is not None and row["prediction"] is not None
                 and row["iou"] >= threshold for row in rows)
        metrics[f"iou_{threshold}"] = {
            "true_positive": tp, "false_positive": predicted_count-tp,
            "false_negative": positives-tp,
            "precision": tp/predicted_count if predicted_count else 0,
            "recall": tp/positives if positives else 0,
            "proposal_recall": sum(row["truth"] is not None
                                   and row["proposal_best_iou"] >= threshold
                                   for row in rows)/positives if positives else 0,
        }
    return {"frames": len(rows), "positive_frames": positives,
            "negative_frames": len(rows)-positives, "metrics": metrics,
            "mean_iou_positive_frames": float(np.mean([row["iou"] for row in rows
                                                       if row["truth"] is not None]))
            if positives else 0,
            "pc_latency_ms_mean": float(np.mean(elapsed)) if elapsed else None,
            "pc_latency_ms_p95": float(np.percentile(elapsed, 95)) if elapsed else None,
            "predictions": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", required=True, type=Path)
    parser.add_argument("--captures-root", type=Path, default=Path("artifacts/captures"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/edge-model-20260914"))
    parser.add_argument("--feature-version", type=int, choices=SUPPORTED_FEATURE_VERSIONS,
                        default=FEATURE_VERSION,
                        help="1: original 406 features; 2: remove 18 quantiles, keep 388 features")
    parser.add_argument("--max-epochs", type=int, default=1000,
                        help="maximum dual-coordinate-descent passes (default: 1000)")
    args = parser.parse_args()
    cv2.setNumThreads(1)
    cv2.setRNGSeed(20260914)
    args.output.mkdir(parents=True, exist_ok=True)
    if any((args.output / name).exists() for name in ('red_ball_svm.npz', 'report.json')):
        parser.error('output already contains a version; choose a new output directory')
    samples = load_samples(args.annotations, args.captures_root)
    x, y, crop_counts = training_data(samples, feature_version=args.feature_version)
    path = args.output / "red_ball_svm.npz"
    fitting = fit_model(x, y, path, feature_version=args.feature_version,
                        max_epochs=args.max_epochs)
    detector = EdgeBallDetector(path, DetectorConfig())
    detector.process(samples[0]["image"])  # warm up; evaluate bypasses temporal state
    train_hashes = {row["sha256"] for row in samples if row["split"] == "train"}
    test_hashes = {row["sha256"] for row in samples if row["split"] == "test"}
    report = {
        "model": str(path), "annotations": str(args.annotations),
        "feature_version": args.feature_version,
        "feature_description": "324 HOG + 12 color mean/std + "
                               + ("18 color quantiles + " if args.feature_version == 1 else "")
                               + "48 LAB grid + 4 geometry",
        "annotations_sha256": hashlib.sha256(args.annotations.read_bytes()).hexdigest(),
        "algorithm": "OpenCV LAB/HSV proposals + NumPy HOG/color + NumPy class-balanced linear SVM",
        "training": dict(crop_counts, **fitting),
        "split_policy": "only train rows create crops and fit weights; duplicate image hashes "
                        "across splits are rejected before fitting; fixed C=1 and margin=0; "
                        "test is evaluated without parameter search",
        "cross_split_duplicate_image_hashes": len(train_hashes & test_hashes),
        "measurement": {"platform": platform.platform(), "processor": platform.processor(),
                        "opencv": cv2.__version__, "opencv_threads": cv2.getNumThreads(),
                        "image_size": [640, 480],
                        "latency_scope": "proposal extraction + features + classification; "
                                         "excludes disk IO and resize; PC only, not OrangePi"},
        "limitations": [("Small manual sample from two same-environment captures; "
                          "no cross-room/generalization claim."),
                        "Color proposals limit recall, especially severe occlusion or color shift.",
                        "Top-one detections evaluated; wrong localization counts as FP and FN.",
                        "Timing is PC single OpenCV thread, not an OrangePi benchmark."],
        "train": evaluate(detector, samples, "train"),
        "test": evaluate(detector, samples, "test"),
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"model": str(path), "training": report["training"],
                      "test": {k: v for k, v in report["test"].items() if k != "predictions"}},
                     indent=2))


if __name__ == "__main__":
    main()
