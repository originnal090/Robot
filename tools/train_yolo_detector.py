"""Train a nano YOLO red-ball detector with route-level model selection.

This optional PC experiment requires ``ultralytics``.  The official annotation
test split is materialized only after the validation-selected epoch and score
threshold have been fixed.  Existing runtime models are never modified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shutil
import time
from pathlib import Path

import cv2
import numpy as np
from train_cnn_detector import (
    Prediction,
    load_rows,
    public_metrics,
    score_predictions,
    split_selection_rows,
)


def link_or_copy(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def write_yolo_split(root: Path, name: str, rows: list[dict]) -> Path:
    images = root / "images" / name
    labels = root / "labels" / name
    images.mkdir(parents=True, exist_ok=False)
    labels.mkdir(parents=True, exist_ok=False)
    for index, row in enumerate(rows):
        stem = f"{index:04d}-{row['capture']}-{Path(row['frame']).stem}"
        destination = images / f"{stem}.png"
        link_or_copy(Path(row["path"]), destination)
        text = ""
        if row["box"] is not None:
            x1, y1, x2, y2 = row["box"]
            width, height = row["width"], row["height"]
            text = (
                f"0 {(x1 + x2) / (2 * width):.8f} {(y1 + y2) / (2 * height):.8f} "
                f"{(x2 - x1) / width:.8f} {(y2 - y1) / height:.8f}\n"
            )
        (labels / f"{stem}.txt").write_text(text, encoding="ascii")
    return images


def write_yaml(path: Path, train: Path, val: Path) -> None:
    # JSON strings are also valid YAML scalars and handle Windows drive letters safely.
    path.write_text(
        f"train: {json.dumps(str(train.resolve()))}\n"
        f"val: {json.dumps(str(val.resolve()))}\n"
        "names:\n  0: red_ball\n",
        encoding="utf-8",
    )


def yolo_predictions(model, rows: list[dict], imgsz: int) -> tuple[list[Prediction], list[float]]:
    predictions = []
    inference_ms = []
    for row in rows:
        image = cv2.imread(row["path"])
        result = model.predict(
            image, imgsz=imgsz, conf=0.001, iou=0.7, max_det=20, classes=[0], verbose=False
        )[0]
        inference_ms.append(float(result.speed["inference"]))
        if result.boxes is None or len(result.boxes) == 0:
            predictions.append(Prediction(None, None))
            continue
        scores = result.boxes.conf.detach().cpu().numpy()
        best = int(scores.argmax())
        predictions.append(
            Prediction(result.boxes.xyxy[best].detach().cpu().numpy().tolist(), float(scores[best]))
        )
    return predictions, inference_ms


def choose_threshold(rows: list[dict], predictions: list[Prediction]) -> dict:
    values = [prediction.score for prediction in predictions if prediction.score is not None]
    candidates = np.unique(np.concatenate((np.linspace(0.01, 0.99, 99), values)))
    reports = [score_predictions(rows, predictions, float(threshold)) for threshold in candidates]
    return max(
        reports,
        key=lambda report: (
            report["f1"],
            -report["fp"],
            report["mean_iou_positive"],
            report["threshold"],
        ),
    )


def best_epoch(csv_path: Path) -> tuple[int, dict]:
    with csv_path.open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    metric = "metrics/mAP50(B)"
    best_index = max(range(len(rows)), key=lambda index: float(rows[index][metric]))
    return best_index + 1, {
        key.strip(): float(value) for key, value in rows[best_index].items() if value.strip()
    }


def train(
    model_path: str | Path, data: Path, project: Path, name: str, args, epochs: int, validate: bool
):
    from ultralytics import YOLO

    model = YOLO(model_path)
    model.train(
        data=str(data),
        epochs=epochs,
        imgsz=args.imgsz,
        batch=args.batch_size,
        device=0 if args.device == "cuda" else args.device,
        workers=0,
        optimizer="AdamW",
        lr0=args.lr,
        lrf=0.02,
        weight_decay=args.weight_decay,
        warmup_epochs=min(3, max(1, epochs // 5)),
        patience=args.patience,
        hsv_h=0.03,
        hsv_s=0.55,
        hsv_v=0.45,
        translate=0.08,
        scale=0.35,
        fliplr=0.5,
        mosaic=0.5,
        close_mosaic=min(10, max(1, epochs // 5)),
        degrees=0,
        shear=0,
        perspective=0,
        mixup=0,
        copy_paste=0,
        seed=args.seed,
        deterministic=True,
        single_cls=True,
        amp=True,
        val=validate,
        plots=False,
        save=True,
        save_period=-1,
        project=str(project.resolve()),
        name=name,
        exist_ok=False,
        verbose=False,
    )
    return project.resolve() / name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--annotations",
        type=Path,
        default=Path("data/red-ball-route-20260914/annotations-balanced.json"),
    )
    parser.add_argument("--captures-root", type=Path, default=Path("artifacts/captures"))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/cnn-route-20260915/yolo11n-640-v1")
    )
    parser.add_argument("--val-capture", default="capture-20260914-091540")
    parser.add_argument("--base-model", default="yolo11n.pt")
    parser.add_argument("--epochs", type=int, default=120)
    parser.add_argument("--patience", type=int, default=30)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=5e-4)
    parser.add_argument("--weight-decay", type=float, default=5e-4)
    parser.add_argument("--seed", type=int, default=20260915)
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output is not empty; choose a new directory")
    args.output.mkdir(parents=True, exist_ok=True)

    rows = load_rows(args.annotations, args.captures_root)
    selection_train, val_rows = split_selection_rows(rows, args.val_capture)
    official_train = [row for row in rows if row["split"] == "train"]
    test_rows = [row for row in rows if row["split"] == "test"]
    dataset = args.output / "dataset"
    selection_train_dir = write_yolo_split(dataset, "selection_train", selection_train)
    val_dir = write_yolo_split(dataset, "selection_val", val_rows)
    final_train_dir = write_yolo_split(dataset, "final_train", official_train)
    selection_yaml = args.output / "selection.yaml"
    final_yaml = args.output / "final.yaml"
    write_yaml(selection_yaml, selection_train_dir, val_dir)
    write_yaml(final_yaml, final_train_dir, final_train_dir)

    started = time.time()
    runs = args.output / "runs"
    selection_dir = train(
        args.base_model, selection_yaml, runs, "selection", args, args.epochs, validate=True
    )
    selected_epoch, selected_row = best_epoch(selection_dir / "results.csv")
    from ultralytics import YOLO

    selection_model = YOLO(selection_dir / "weights" / "best.pt")
    val_predictions, _ = yolo_predictions(selection_model, val_rows, args.imgsz)
    val_report = choose_threshold(val_rows, val_predictions)
    threshold = float(val_report["threshold"])

    final_dir = train(
        args.base_model, final_yaml, runs, "final", args, selected_epoch, validate=False
    )
    final_weights = final_dir / "weights" / "last.pt"
    final_model = YOLO(final_weights)
    test_predictions, test_latency = yolo_predictions(final_model, test_rows, args.imgsz)
    test_report = score_predictions(test_rows, test_predictions, threshold)
    final_path = args.output / "model.pt"
    shutil.copy2(final_weights, final_path)
    parameters = sum(parameter.numel() for parameter in final_model.model.parameters())
    report = {
        "id": "yolo11n-640-red-ball-v1",
        "purpose": "offline PC feasibility experiment; runtime detector unchanged",
        "architecture": "Ultralytics YOLO11n",
        "initialization": args.base_model,
        "parameters": parameters,
        "model_bytes": final_path.stat().st_size,
        "model_sha256": hashlib.sha256(final_path.read_bytes()).hexdigest(),
        "annotations": str(args.annotations),
        "annotations_sha256": hashlib.sha256(args.annotations.read_bytes()).hexdigest(),
        "counts": {
            "selection_train": len(selection_train),
            "validation": len(val_rows),
            "final_train": len(official_train),
            "test": len(test_rows),
        },
        "split_policy": {
            "selection_train": "official train excluding one complete validation capture",
            "validation_capture": args.val_capture,
            "validation_use": "epoch and confidence threshold selection only",
            "final_fit": "restart from base weights on all official train rows",
            "test_use": "one final evaluation after model selection",
        },
        "hyperparameters": vars(args)
        | {
            "annotations": str(args.annotations),
            "captures_root": str(args.captures_root),
            "output": str(args.output),
        },
        "selection": {
            "best_epoch": selected_epoch,
            "training_row": selected_row,
            "validation": public_metrics(val_report),
        },
        "test": public_metrics(test_report),
        "test_predictions": test_report["predictions"],
        "latency": {
            "device": args.device,
            "test_inference_ms_mean": float(np.mean(test_latency)),
            "test_inference_ms_p95": float(np.percentile(test_latency, 95)),
            "scope": "Ultralytics reported model inference; excludes decode/pre/postprocess",
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "opencv": cv2.__version__,
        },
        "elapsed_minutes": (time.time() - started) / 60,
        "limitations": [
            "Only 138 sparse labels from one room and two recording periods.",
            "Validation includes one capture also used for separate official-test background frames.",
            "PC CUDA latency does not predict Ascend NPU latency.",
            "Ultralytics is an experiment dependency and is not added to the robot runtime.",
        ],
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "model": str(final_path),
                "selected_epoch": selected_epoch,
                "threshold": threshold,
                "test": public_metrics(test_report),
                "latency": report["latency"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
