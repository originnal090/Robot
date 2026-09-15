"""Fine-tune and evaluate a compact CNN red-ball detector on recorded frames.

The official annotation ``test`` split is never used for checkpoint, epoch, or
score-threshold selection.  Model selection holds out one complete training
capture; after selection, the model is restarted from COCO weights and fitted
on every official training row for the selected number of epochs.

This is an offline experiment.  It neither changes the runtime detector nor
overwrites an existing output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import cv2
import numpy as np

from hcirobot.edge_detector import box_iou

COCO_SPORTS_BALL_LABEL = 37
DEFAULT_VAL_CAPTURE = "capture-20260914-091540"


def _torch():
    try:
        import torch
        import torchvision
    except ImportError as exc:  # pragma: no cover - depends on optional tooling
        raise SystemExit(
            "CNN training needs PyTorch and TorchVision. Install them in the active "
            "environment before running this offline tool."
        ) from exc
    return torch, torchvision


def seed_everything(seed: int) -> None:
    torch, _ = _torch()
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def load_rows(annotations: Path, captures_root: Path) -> list[dict]:
    rows = json.loads(annotations.read_text(encoding="utf-8"))
    result = []
    seen: dict[str, str] = {}
    for row in rows:
        if row.get("split") not in {"train", "test"}:
            raise ValueError(f"invalid split in {row}")
        path = captures_root / row["capture"] / row["frame"]
        image = cv2.imread(str(path))
        if image is None:
            raise ValueError(f"could not read {path}")
        height, width = image.shape[:2]
        box = row.get("box")
        if box is not None:
            if len(box) != 4 or not (
                0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
            ):
                raise ValueError(f"invalid box in {row}")
            box = [float(value) for value in box]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        other_split = seen.get(digest)
        if other_split is not None and other_split != row["split"]:
            raise ValueError("identical image content appears in train and test")
        seen[digest] = row["split"]
        result.append(dict(row, path=str(path), box=box, width=width, height=height, sha256=digest))
    if not any(row["split"] == "train" for row in result):
        raise ValueError("no training rows")
    if not any(row["split"] == "test" for row in result):
        raise ValueError("no test rows")
    return result


def split_selection_rows(rows: list[dict], val_capture: str) -> tuple[list[dict], list[dict]]:
    train = [row for row in rows if row["split"] == "train" and row["capture"] != val_capture]
    val = [row for row in rows if row["split"] == "train" and row["capture"] == val_capture]
    if not train or not val:
        raise ValueError(f"validation capture {val_capture!r} does not split training rows")
    if not any(row["box"] is not None for row in train):
        raise ValueError("selection training set has no positive frames")
    if not any(row["box"] is not None for row in val) or not any(row["box"] is None for row in val):
        raise ValueError("validation capture must contain positive and negative frames")
    return train, val


def _augment(
    image: np.ndarray, box: list[float] | None, rng: np.random.Generator
) -> tuple[np.ndarray, list[float] | None]:
    height, width = image.shape[:2]
    output = image.copy()
    transformed = box.copy() if box is not None else None

    if rng.random() < 0.5:
        output = cv2.flip(output, 1)
        if transformed is not None:
            x1, y1, x2, y2 = transformed
            transformed = [width - x2, y1, width - x1, y2]

    # Mild scale and translation preserve the route camera geometry while
    # exposing the detector to more positions and partially clipped balls.
    if rng.random() < 0.8:
        scale = float(rng.uniform(0.88, 1.12))
        tx = float(rng.uniform(-0.06, 0.06) * width)
        ty = float(rng.uniform(-0.06, 0.06) * height)
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), 0, scale)
        matrix[:, 2] += (tx, ty)
        output = cv2.warpAffine(
            output,
            matrix,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT_101,
        )
        if transformed is not None:
            x1, y1, x2, y2 = transformed
            points = np.float32([[x1, y1, 1], [x2, y2, 1]]) @ matrix.T
            transformed = [
                float(np.clip(points[0, 0], 0, width - 1)),
                float(np.clip(points[0, 1], 0, height - 1)),
                float(np.clip(points[1, 0], 1, width)),
                float(np.clip(points[1, 1], 1, height)),
            ]
            if transformed[2] <= transformed[0] + 2 or transformed[3] <= transformed[1] + 2:
                return _augment(image, box, rng)

    alpha = float(rng.uniform(0.75, 1.25))
    beta = float(rng.uniform(-24, 24))
    output = np.clip(output.astype(np.float32) * alpha + beta, 0, 255).astype(np.uint8)
    if rng.random() < 0.45:
        hsv = cv2.cvtColor(output, cv2.COLOR_BGR2HSV)
        hsv[:, :, 0] = (hsv[:, :, 0].astype(np.int16) + int(rng.integers(-8, 9))) % 180
        hsv[:, :, 1] = np.clip(
            hsv[:, :, 1].astype(np.float32) * rng.uniform(0.7, 1.3), 0, 255
        ).astype(np.uint8)
        output = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    if rng.random() < 0.3:
        length = int(rng.choice([3, 5, 7]))
        kernel = np.zeros((length, length), np.float32)
        if rng.random() < 0.5:
            kernel[length // 2, :] = 1 / length
        else:
            kernel[:, length // 2] = 1 / length
        output = cv2.filter2D(output, -1, kernel)
    return output, transformed


class BallDataset:
    def __init__(self, rows: list[dict], augment: bool, repeats: int, seed: int, target_label: int):
        self.rows = rows
        self.augment = augment
        self.repeats = repeats
        self.seed = seed
        self.target_label = target_label
        self.epoch = 0

    def __len__(self) -> int:
        return len(self.rows) * self.repeats

    def set_epoch(self, epoch: int) -> None:
        self.epoch = epoch

    def __getitem__(self, index: int):
        torch, _ = _torch()
        row = self.rows[index % len(self.rows)]
        image = cv2.imread(row["path"])
        if image is None:
            raise ValueError(f"could not read {row['path']}")
        box = row["box"]
        if self.augment:
            rng = np.random.default_rng(self.seed + self.epoch * len(self) + index)
            image, box = _augment(image, box, rng)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        tensor = torch.from_numpy(np.ascontiguousarray(image.transpose(2, 0, 1))).float() / 255
        if box is None:
            boxes = torch.empty((0, 4), dtype=torch.float32)
            labels = torch.empty((0,), dtype=torch.int64)
        else:
            boxes = torch.tensor([box], dtype=torch.float32)
            labels = torch.tensor([self.target_label], dtype=torch.int64)
        target = {
            "boxes": boxes,
            "labels": labels,
            "image_id": torch.tensor(index % len(self.rows), dtype=torch.int64),
        }
        return tensor, target


def collate(batch):
    return tuple(zip(*batch))


def make_model(device, binary_head: bool):
    torch, torchvision = _torch()
    weights = torchvision.models.detection.SSDLite320_MobileNet_V3_Large_Weights.DEFAULT
    model = torchvision.models.detection.ssdlite320_mobilenet_v3_large(
        weights=weights, score_thresh=0.001, detections_per_img=100, topk_candidates=200
    )
    if binary_head:
        from torchvision.models.detection import _utils as detection_utils
        from torchvision.models.detection.ssdlite import SSDLiteClassificationHead

        old_head = model.head.classification_head
        channels = detection_utils.retrieve_out_channels(model.backbone, (320, 320))
        anchors = model.anchor_generator.num_anchors_per_location()
        norm = partial(torch.nn.BatchNorm2d, eps=0.001, momentum=0.03)
        new_head = SSDLiteClassificationHead(channels, anchors, 2, norm)
        with torch.no_grad():
            for old_block, new_block, anchor_count in zip(
                old_head.module_list, new_head.module_list, anchors, strict=True
            ):
                # Preserve the COCO feature projection, then retain its background and
                # sports-ball logits for each anchor in the compact two-class output.
                new_block[0].load_state_dict(old_block[0].state_dict())
                old_weight = old_block[1].weight.reshape(
                    anchor_count, 91, *old_block[1].weight.shape[1:]
                )
                old_bias = old_block[1].bias.reshape(anchor_count, 91)
                indices = torch.tensor([0, COCO_SPORTS_BALL_LABEL])
                new_block[1].weight.copy_(old_weight[:, indices].reshape_as(new_block[1].weight))
                new_block[1].bias.copy_(old_bias[:, indices].reshape_as(new_block[1].bias))
        model.head.classification_head = new_head
    return model.to(device)


def set_trainable(model, full: bool) -> None:
    for parameter in model.parameters():
        parameter.requires_grad = full
    if not full:
        for parameter in model.head.classification_head.parameters():
            parameter.requires_grad = True


def keep_batch_norm_frozen(module) -> None:
    torch, _ = _torch()
    if isinstance(module, torch.nn.modules.batchnorm._BatchNorm):
        module.eval()


@dataclass
class Prediction:
    box: list[float] | None
    score: float | None


def predict_rows(
    model, rows: Iterable[dict], device, target_label: int, *, warmup: bool = False
) -> tuple[list[Prediction], list[float]]:
    torch, _ = _torch()
    model.eval()
    predictions: list[Prediction] = []
    elapsed: list[float] = []
    with torch.inference_mode():
        for index, row in enumerate(rows):
            image = cv2.cvtColor(cv2.imread(row["path"]), cv2.COLOR_BGR2RGB)
            tensor = torch.from_numpy(np.ascontiguousarray(image.transpose(2, 0, 1)))
            tensor = tensor.float().div_(255).to(device)
            if warmup and index == 0:
                for _ in range(3):
                    model([tensor])
                if device.type == "cuda":
                    torch.cuda.synchronize()
            before = time.perf_counter()
            output = model([tensor])[0]
            if device.type == "cuda":
                torch.cuda.synchronize()
            elapsed.append((time.perf_counter() - before) * 1000)
            mask = output["labels"] == target_label
            boxes = output["boxes"][mask]
            scores = output["scores"][mask]
            if len(scores):
                best = int(scores.argmax())
                predictions.append(
                    Prediction(
                        box=boxes[best].detach().cpu().tolist(),
                        score=float(scores[best].detach().cpu()),
                    )
                )
            else:
                predictions.append(Prediction(box=None, score=None))
    return predictions, elapsed


def score_predictions(rows: list[dict], predictions: list[Prediction], threshold: float) -> dict:
    evaluated = []
    for row, prediction in zip(rows, predictions, strict=True):
        box = (
            prediction.box
            if prediction.score is not None and prediction.score >= threshold
            else None
        )
        truth = row["box"]
        iou = box_iou(tuple(box), tuple(truth)) if box is not None and truth is not None else 0.0
        evaluated.append(
            {
                "capture": row["capture"],
                "frame": row["frame"],
                "truth": truth,
                "prediction": box,
                "score": prediction.score,
                "iou": iou,
            }
        )
    positives = sum(row["truth"] is not None for row in evaluated)
    predicted = sum(row["prediction"] is not None for row in evaluated)
    tp = sum(
        row["truth"] is not None and row["prediction"] is not None and row["iou"] >= 0.5
        for row in evaluated
    )
    fp = predicted - tp
    fn = positives - tp
    precision = tp / predicted if predicted else 0.0
    recall = tp / positives if positives else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "frames": len(evaluated),
        "positive_frames": positives,
        "negative_frames": len(evaluated) - positives,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "negative_frame_false_positives": sum(
            row["truth"] is None and row["prediction"] is not None for row in evaluated
        ),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "mean_iou_positive": float(
            np.mean([row["iou"] for row in evaluated if row["truth"] is not None])
        )
        if positives
        else 0.0,
        "threshold": threshold,
        "predictions": evaluated,
    }


def choose_threshold(rows: list[dict], predictions: list[Prediction]) -> dict:
    candidates = np.unique(
        np.concatenate(
            (
                np.linspace(0.02, 0.98, 97),
                np.asarray(
                    [prediction.score for prediction in predictions if prediction.score is not None]
                ),
            )
        )
    )
    reports = [score_predictions(rows, predictions, float(threshold)) for threshold in candidates]
    # False positives are the practical safety tie-breaker; IoU breaks the remaining ties.
    return max(
        reports,
        key=lambda report: (
            report["f1"],
            -report["fp"],
            report["mean_iou_positive"],
            report["threshold"],
        ),
    )


def public_metrics(report: dict) -> dict:
    return {key: value for key, value in report.items() if key != "predictions"}


def train_epoch(model, loader, optimizer, device) -> dict:
    torch, _ = _torch()
    model.train()
    model.apply(keep_batch_norm_frozen)
    totals: dict[str, float] = {}
    batches = 0
    for images, targets in loader:
        images = [image.to(device, non_blocking=True) for image in images]
        targets = [
            {key: value.to(device, non_blocking=True) for key, value in target.items()}
            for target in targets
        ]
        losses = model(images, targets)
        loss = sum(losses.values())
        if not torch.isfinite(loss):
            raise RuntimeError(f"non-finite training loss: {losses}")
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(
            [parameter for parameter in model.parameters() if parameter.requires_grad], 5.0
        )
        optimizer.step()
        batches += 1
        for name, value in losses.items():
            totals[name] = totals.get(name, 0.0) + float(value.detach().cpu())
    return {name: value / batches for name, value in totals.items()}


def selection_run(train_rows: list[dict], val_rows: list[dict], args, device):
    torch, _ = _torch()
    seed_everything(args.seed)
    target_label = 1 if args.binary_head else COCO_SPORTS_BALL_LABEL
    model = make_model(device, args.binary_head)
    dataset = BallDataset(
        train_rows, augment=True, repeats=args.repeats, seed=args.seed, target_label=target_label
    )
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=0,
        collate_fn=collate,
        pin_memory=device.type == "cuda",
    )
    history = []
    best = None
    initial_predictions, _ = predict_rows(model, val_rows, device, target_label)
    initial = choose_threshold(val_rows, initial_predictions)
    for epoch in range(1, args.head_epochs + args.full_epochs + 1):
        full = epoch > args.head_epochs
        if epoch == 1 or epoch == args.head_epochs + 1:
            set_trainable(model, full=full)
            lr = args.full_lr if full else args.head_lr
            optimizer = torch.optim.AdamW(
                [parameter for parameter in model.parameters() if parameter.requires_grad],
                lr=lr,
                weight_decay=args.weight_decay,
            )
            remaining = args.full_epochs if full else args.head_epochs
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer, T_max=max(1, remaining), eta_min=lr * 0.05
            )
        dataset.set_epoch(epoch)
        losses = train_epoch(model, loader, optimizer, device)
        predictions, _ = predict_rows(model, val_rows, device, target_label)
        report = choose_threshold(val_rows, predictions)
        phase_epoch = epoch - args.head_epochs if full else epoch
        history.append(
            {
                "epoch": epoch,
                "phase": "full" if full else "head",
                "phase_epoch": phase_epoch,
                "lr": optimizer.param_groups[0]["lr"],
                "losses": losses,
                "validation": public_metrics(report),
            }
        )
        key = (report["f1"], -report["fp"], report["mean_iou_positive"], report["threshold"])
        if best is None or key > best[0]:
            best = (key, epoch, "full" if full else "head", report)
        scheduler.step()
        print(
            json.dumps(
                {
                    "epoch": epoch,
                    "phase": history[-1]["phase"],
                    "loss": sum(losses.values()),
                    "val": public_metrics(report),
                }
            ),
            flush=True,
        )
    assert best is not None
    return initial, history, {"epoch": best[1], "phase": best[2], "validation": best[3]}


def final_run(rows: list[dict], epochs: int, args, device):
    torch, _ = _torch()
    seed_everything(args.seed)
    target_label = 1 if args.binary_head else COCO_SPORTS_BALL_LABEL
    model = make_model(device, args.binary_head)
    dataset = BallDataset(
        rows, augment=True, repeats=args.repeats, seed=args.seed, target_label=target_label
    )
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=0,
        collate_fn=collate,
        pin_memory=device.type == "cuda",
    )
    history = []
    optimizer = scheduler = None
    for epoch in range(1, epochs + 1):
        full = epoch > args.head_epochs
        if epoch == 1 or epoch == args.head_epochs + 1:
            set_trainable(model, full=full)
            lr = args.full_lr if full else args.head_lr
            optimizer = torch.optim.AdamW(
                [parameter for parameter in model.parameters() if parameter.requires_grad],
                lr=lr,
                weight_decay=args.weight_decay,
            )
            phase_length = (epochs - args.head_epochs) if full else min(epochs, args.head_epochs)
            scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer, T_max=max(1, phase_length), eta_min=lr * 0.05
            )
        dataset.set_epoch(epoch)
        losses = train_epoch(model, loader, optimizer, device)
        history.append(
            {
                "epoch": epoch,
                "phase": "full" if full else "head",
                "lr": optimizer.param_groups[0]["lr"],
                "losses": losses,
            }
        )
        scheduler.step()
        print(f"final fit {epoch}/{epochs}: loss={sum(losses.values()):.4f}", flush=True)
    return model, history


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--annotations",
        type=Path,
        default=Path("data/red-ball-route-20260914/annotations-balanced.json"),
    )
    parser.add_argument("--captures-root", type=Path, default=Path("artifacts/captures"))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/cnn-route-20260915/ssdlite-coco-finetune")
    )
    parser.add_argument("--val-capture", default=DEFAULT_VAL_CAPTURE)
    parser.add_argument("--head-epochs", type=int, default=12)
    parser.add_argument("--full-epochs", type=int, default=28)
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--head-lr", type=float, default=3e-4)
    parser.add_argument("--full-lr", type=float, default=3e-5)
    parser.add_argument("--weight-decay", type=float, default=2e-4)
    parser.add_argument("--seed", type=int, default=20260915)
    parser.add_argument(
        "--binary-head",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="collapse COCO logits to background/ball and copy their weights",
    )
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output is not empty; use a new directory to preserve prior results")
    if min(args.head_epochs, args.repeats, args.batch_size) < 1 or args.full_epochs < 0:
        parser.error("epochs, repeats, and batch size must be positive")
    args.output.mkdir(parents=True, exist_ok=True)

    torch, torchvision = _torch()
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA requested but unavailable")
    device = torch.device(
        "cuda"
        if args.device == "auto" and torch.cuda.is_available()
        else args.device
        if args.device != "auto"
        else "cpu"
    )
    rows = load_rows(args.annotations, args.captures_root)
    selection_train, val_rows = split_selection_rows(rows, args.val_capture)
    official_train = [row for row in rows if row["split"] == "train"]
    test_rows = [row for row in rows if row["split"] == "test"]

    started = time.time()
    pretrained_val, selection_history, best = selection_run(selection_train, val_rows, args, device)
    chosen_epoch = int(best["epoch"])
    threshold = float(best["validation"]["threshold"])
    final_model, final_history = final_run(official_train, chosen_epoch, args, device)

    target_label = 1 if args.binary_head else COCO_SPORTS_BALL_LABEL
    test_predictions, test_latency = predict_rows(
        final_model, test_rows, device, target_label, warmup=True
    )
    test_report = score_predictions(test_rows, test_predictions, threshold)
    val_predictions, val_latency = predict_rows(
        final_model, val_rows, device, target_label, warmup=True
    )
    refit_val_report = score_predictions(val_rows, val_predictions, threshold)
    model_path = args.output / "model.pt"
    torch.save(
        {
            "architecture": "torchvision-ssdlite320-mobilenet-v3-large",
            "weights": "COCO_V1-finetuned-binary-ball"
            if args.binary_head
            else "COCO_V1-finetuned-sports-ball-label-37",
            "state_dict": final_model.state_dict(),
            "score_threshold": threshold,
            "epoch": chosen_epoch,
        },
        model_path,
    )

    parameters = sum(parameter.numel() for parameter in final_model.parameters())
    report = {
        "id": "ssdlite320-mobilenet-v3-large-coco-red-ball-v1",
        "purpose": "offline PC feasibility experiment; runtime detector unchanged",
        "architecture": "TorchVision SSDLite320 MobileNetV3 Large",
        "initialization": (
            "COCO_V1; binary head initialized from background/sports-ball "
            "logits; localization head retained"
            if args.binary_head
            else "COCO_V1; retain label 37 (sports ball) and localization head"
        ),
        "parameters": parameters,
        "model_bytes": model_path.stat().st_size,
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "annotations": str(args.annotations),
        "annotations_sha256": hashlib.sha256(args.annotations.read_bytes()).hexdigest(),
        "split_policy": {
            "selection_train": "official train rows excluding one complete validation capture",
            "validation_capture": args.val_capture,
            "validation_use": "checkpoint epoch and confidence threshold only",
            "final_fit": "restart from COCO and fit all official train rows for chosen epoch count",
            "test_use": "one final evaluation after epoch and threshold selection",
        },
        "counts": {
            "selection_train": len(selection_train),
            "validation": len(val_rows),
            "final_train": len(official_train),
            "test": len(test_rows),
        },
        "hyperparameters": vars(args)
        | {
            "annotations": str(args.annotations),
            "captures_root": str(args.captures_root),
            "output": str(args.output),
        },
        "selection": {
            "pretrained_coco_validation": public_metrics(pretrained_val),
            "best": {
                "epoch": chosen_epoch,
                "phase": best["phase"],
                "validation": public_metrics(best["validation"]),
            },
            "history": selection_history,
        },
        "final_fit_history": final_history,
        "refit_validation": public_metrics(refit_val_report),
        "test": public_metrics(test_report),
        "test_predictions": test_report["predictions"],
        "latency": {
            "device": str(device),
            "validation_ms_mean": float(np.mean(val_latency)),
            "validation_ms_p95": float(np.percentile(val_latency, 95)),
            "test_ms_mean": float(np.mean(test_latency)),
            "test_ms_p95": float(np.percentile(test_latency, 95)),
            "scope": "batch=1 model forward including resize/postprocess; excludes image decode",
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "torch": torch.__version__,
            "torchvision": torchvision.__version__,
            "opencv": cv2.__version__,
            "gpu": torch.cuda.get_device_name() if device.type == "cuda" else None,
        },
        "elapsed_minutes": (time.time() - started) / 60,
        "limitations": [
            "Only 138 sparse labels from one room and two recording periods.",
            "Validation includes one capture also used for separate official-test background frames.",
            "PC CUDA latency does not predict Orange Pi CPU or Ascend NPU latency.",
            "This experiment has no temporal tracking or navigation integration.",
        ],
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "model": str(model_path),
                "chosen_epoch": chosen_epoch,
                "threshold": threshold,
                "test": public_metrics(test_report),
                "latency": report["latency"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
