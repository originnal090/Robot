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
import math
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


def enable_ascend_npu_compatibility() -> None:
    """Enable the experimental Ascend 310B1 YOLO training probe.

    Ascend 310B1 lacks a few operators used by Ultralytics' no-grad target
    preparation and task-aligned assigner.  These tensors describe targets;
    they are not part of autograd, so doing only this small bookkeeping step on
    CPU preserves their intended values. CANN 8.0 on the tested board still
    lacks the classification-loss backward path, so this is diagnostic support,
    not a production-ready training backend.
    """
    import torch
    import torch.nn.functional as torch_functional
    import torch_npu
    import ultralytics.engine.trainer as trainer_module
    import ultralytics.utils.loss as loss_module
    from ultralytics.utils.loss import DFLoss, v8DetectionLoss
    from ultralytics.utils.ops import xywh2xyxy
    from ultralytics.utils.tal import TaskAlignedAssigner

    # Ultralytics' generic AMP probe uses an FP32 MaxPool path unsupported by
    # 310B1 even though the actual FP16 training path is supported.
    trainer_module.check_amp = lambda model: True

    class StaticGradScaler:
        """AMP-compatible scaler that avoids unsupported NPU overflow-status ops."""

        def __init__(self, *args, **kwargs):
            self._enabled = bool(kwargs.get("enabled", True))

        def scale(self, value):
            return value

        def unscale_(self, optimizer):
            return None

        def step(self, optimizer, *args, **kwargs):
            return optimizer.step(*args, **kwargs)

        def update(self, new_scale=None):
            return None

        def state_dict(self):
            return {"enabled": self._enabled, "static_scale": 1.0}

        def load_state_dict(self, state_dict):
            return None

        def get_scale(self):
            return 1.0

        def is_enabled(self):
            return self._enabled

    # The 310B1 runtime cannot compile NPUClearFloatStatusV2, which the
    # dynamic GradScaler uses for overflow tracking. Autocast remains enabled;
    # this replaces only dynamic loss scaling for this process.
    torch_npu.npu.amp.GradScaler = StaticGradScaler

    def cpu_preprocess(self, targets, batch_size, scale_tensor):
        target_device = self.device
        targets_cpu = targets.detach().cpu()
        nl, ne = targets_cpu.shape
        if nl == 0:
            return torch.zeros(batch_size, 0, ne - 1, device=target_device)

        batch_idx = targets_cpu[:, 0].long()
        _, counts = batch_idx.unique(return_counts=True)
        out = torch.zeros(batch_size, int(counts.max()), ne - 1)
        offsets = torch.zeros(batch_size + 1, dtype=torch.long)
        offsets.scatter_add_(0, batch_idx + 1, torch.ones_like(batch_idx))
        offsets = offsets.cumsum(0)
        within_idx = torch.arange(nl) - offsets[batch_idx]
        out[batch_idx, within_idx] = targets_cpu[:, 1:]
        out[..., 1:5] = xywh2xyxy(out[..., 1:5].mul_(scale_tensor.detach().cpu()))
        return out.to(target_device)

    original_assign = TaskAlignedAssigner.forward

    def cpu_assign(self, pd_scores, pd_bboxes, anc_points, gt_labels, gt_bboxes, mask_gt):
        target_device = pd_scores.device
        result = original_assign(
            self,
            pd_scores.detach().float().cpu(),
            pd_bboxes.detach().float().cpu(),
            anc_points.detach().float().cpu(),
            gt_labels.detach().cpu(),
            gt_bboxes.detach().float().cpu(),
            mask_gt.detach().cpu(),
        )
        target_labels, target_bboxes, target_scores, fg_mask, target_gt_idx = result
        return (
            target_labels.to(target_device),
            target_bboxes.to(device=target_device, dtype=pd_bboxes.dtype),
            target_scores.to(device=target_device, dtype=pd_scores.dtype),
            fg_mask.to(target_device),
            target_gt_idx.to(target_device),
        )

    def npu_dfl_loss(self, pred_dist, target):
        """Equivalent DFL without the 310B1-incompatible GatherElements kernel."""
        target_cpu = target.detach().float().cpu().clamp_(0, self.reg_max - 1 - 0.01)
        target_left = target_cpu.long()
        target_right = target_left + 1
        weight_left = (target_right - target_cpu).to(device=pred_dist.device, dtype=pred_dist.dtype)
        weight_right = 1 - weight_left
        left_mask = torch_functional.one_hot(
            target_left.reshape(-1), num_classes=self.reg_max
        ).to(device=pred_dist.device, dtype=pred_dist.dtype)
        right_mask = torch_functional.one_hot(
            target_right.reshape(-1), num_classes=self.reg_max
        ).to(device=pred_dist.device, dtype=pred_dist.dtype)
        log_probabilities = torch_functional.log_softmax(pred_dist, dim=1)
        left_logp = (log_probabilities * left_mask).sum(1).view(target_left.shape)
        right_logp = (log_probabilities * right_mask).sum(1).view(target_left.shape)
        return -(left_logp * weight_left + right_logp * weight_right).mean(-1, keepdim=True)

    def npu_bbox_iou(box1, box2, xywh=True, GIoU=False, DIoU=False, CIoU=False, eps=1e-7):
        """Ultralytics bbox IoU without Minimum/Maximum backward SelectV2."""

        def minimum(left, right):
            return (left + right - (left - right).abs()) * 0.5

        def maximum(left, right):
            return (left + right + (left - right).abs()) * 0.5

        if xywh:
            (x1, y1, w1, h1), (x2, y2, w2, h2) = box1.chunk(4, -1), box2.chunk(4, -1)
            w1_half, h1_half, w2_half, h2_half = w1 / 2, h1 / 2, w2 / 2, h2 / 2
            b1_x1, b1_x2, b1_y1, b1_y2 = x1 - w1_half, x1 + w1_half, y1 - h1_half, y1 + h1_half
            b2_x1, b2_x2, b2_y1, b2_y2 = x2 - w2_half, x2 + w2_half, y2 - h2_half, y2 + h2_half
        else:
            b1_x1, b1_y1, b1_x2, b1_y2 = box1.chunk(4, -1)
            b2_x1, b2_y1, b2_x2, b2_y2 = box2.chunk(4, -1)
            w1, h1 = b1_x2 - b1_x1, b1_y2 - b1_y1 + eps
            w2, h2 = b2_x2 - b2_x1, b2_y2 - b2_y1 + eps

        inter_width = torch_functional.relu(minimum(b1_x2, b2_x2) - maximum(b1_x1, b2_x1))
        inter_height = torch_functional.relu(minimum(b1_y2, b2_y2) - maximum(b1_y1, b2_y1))
        intersection = inter_width * inter_height
        union = w1 * h1 + w2 * h2 - intersection + eps
        iou = intersection / union
        if not (CIoU or DIoU or GIoU):
            return iou

        convex_width = maximum(b1_x2, b2_x2) - minimum(b1_x1, b2_x1)
        convex_height = maximum(b1_y2, b2_y2) - minimum(b1_y1, b2_y1)
        if CIoU or DIoU:
            convex_diagonal = convex_width.pow(2) + convex_height.pow(2) + eps
            center_distance = (
                (b2_x1 + b2_x2 - b1_x1 - b1_x2).pow(2)
                + (b2_y1 + b2_y2 - b1_y1 - b1_y2).pow(2)
            ) / 4
            if CIoU:
                aspect = (4 / math.pi**2) * ((w2 / h2).atan() - (w1 / h1).atan()).pow(2)
                with torch.no_grad():
                    alpha = aspect / (1 - iou + aspect + eps)
                return iou - (center_distance / convex_diagonal + aspect * alpha)
            return iou - center_distance / convex_diagonal
        convex_area = convex_width * convex_height + eps
        return iou - (convex_area - union) / convex_area

    v8DetectionLoss.preprocess = cpu_preprocess
    TaskAlignedAssigner.forward = cpu_assign
    DFLoss.__call__ = npu_dfl_loss
    loss_module.bbox_iou = npu_bbox_iou


def train(
    model_path: str | Path, data: Path, project: Path, name: str, args, epochs: int, validate: bool
):
    from ultralytics import YOLO

    if args.device == "npu":
        import torch
        import torch_npu  # noqa: F401  # registers the private-use NPU backend

        torch.npu.set_device(0)
        torch.npu.set_compile_mode(jit_compile=False)
        enable_ascend_npu_compatibility()
        device = "npu:0"
    else:
        device = 0 if args.device == "cuda" else args.device
    model = YOLO(model_path)
    model.train(
        data=str(data),
        epochs=epochs,
        imgsz=args.imgsz,
        batch=args.batch_size,
        device=device,
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
        freeze=args.freeze_layers or None,
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
    parser.add_argument(
        "--extra-annotations",
        type=Path,
        action="append",
        default=[],
        help="additional training annotations to merge before split validation",
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
    parser.add_argument("--device", choices=("cuda", "cpu", "npu"), default="cuda")
    parser.add_argument(
        "--freeze-layers",
        type=int,
        default=0,
        help="freeze the first N model layers; use 11 on Ascend 310B1 to avoid C2PSA backward",
    )
    parser.add_argument("--run-id", default="yolo11n-640-red-ball-v1")
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output is not empty; choose a new directory")
    args.output.mkdir(parents=True, exist_ok=True)

    annotation_paths = [args.annotations, *args.extra_annotations]
    if args.extra_annotations:
        merged_rows = []
        for annotation_path in annotation_paths:
            values = json.loads(annotation_path.read_text(encoding="utf-8"))
            if not isinstance(values, list):
                raise TypeError(f"annotations must be a list: {annotation_path}")
            merged_rows.extend(values)
        merged_annotations = args.output / "annotations-merged.json"
        merged_annotations.write_text(json.dumps(merged_rows, indent=2), encoding="utf-8")
        rows = load_rows(merged_annotations, args.captures_root)
    else:
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
        "id": args.run_id,
        "purpose": "offline PC feasibility experiment; runtime detector unchanged",
        "architecture": "Ultralytics YOLO11n",
        "initialization": args.base_model,
        "parameters": parameters,
        "model_bytes": final_path.stat().st_size,
        "model_sha256": hashlib.sha256(final_path.read_bytes()).hexdigest(),
        "annotations": [str(path) for path in annotation_paths],
        "annotations_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in annotation_paths
        },
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
            "extra_annotations": [str(path) for path in args.extra_annotations],
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
            f"Only {len(rows)} sparse labels from one room and a few recording periods.",
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
