"""Prepare the reviewed TonyPi + conservative iPhone 7 training corpus for YOLO v3.

"v3" is the project model iteration, not the historical Darknet YOLOv3
architecture.  iPhone labels are accepted only when the current v1 teacher is
confident and the proposed box contains enough saturated red pixels.  Very
low-confidence frames are retained as negatives; ambiguous frames are omitted.
One complete iPhone session is materialized as a replay holdout and is never
included in the training annotation file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections import Counter
from pathlib import Path

import cv2
import numpy as np


def sample_indices(count: int, stride: int) -> list[int]:
    """Return deterministic temporal samples and always include the final frame."""
    if count <= 0:
        return []
    indices = list(range(0, count, stride))
    if indices[-1] != count - 1:
        indices.append(count - 1)
    return indices


def red_pixel_ratio(image: np.ndarray, box: list[float]) -> float:
    height, width = image.shape[:2]
    x1, y1, x2, y2 = box
    left = max(0, min(width - 1, int(np.floor(x1))))
    top = max(0, min(height - 1, int(np.floor(y1))))
    right = max(left + 1, min(width, int(np.ceil(x2))))
    bottom = max(top + 1, min(height, int(np.ceil(y2))))
    hsv = cv2.cvtColor(image[top:bottom, left:right], cv2.COLOR_BGR2HSV)
    red = (
        ((hsv[:, :, 0] <= 15) | (hsv[:, :, 0] >= 160))
        & (hsv[:, :, 1] >= 70)
        & (hsv[:, :, 2] >= 40)
    )
    return float(red.mean())


def classify_teacher_label(
    score: float,
    red_ratio: float,
    *,
    positive_confidence: float,
    negative_confidence: float,
    minimum_red_ratio: float,
) -> str:
    if score >= positive_confidence and red_ratio >= minimum_red_ratio:
        return "positive"
    if score <= negative_confidence:
        return "negative"
    return "ambiguous"


def link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def clamp_box(box: np.ndarray, width: int, height: int) -> list[float]:
    x1, y1, x2, y2 = (float(value) for value in box)
    return [
        max(0.0, min(float(width - 1), x1)),
        max(0.0, min(float(height - 1), y1)),
        max(1.0, min(float(width), x2)),
        max(1.0, min(float(height), y2)),
    ]


def teacher_prediction(result, image: np.ndarray) -> tuple[float, list[float] | None, float]:
    if result.boxes is None or len(result.boxes) == 0:
        return 0.0, None, 0.0
    scores = result.boxes.conf.detach().cpu().numpy()
    best = int(scores.argmax())
    height, width = image.shape[:2]
    box = clamp_box(result.boxes.xyxy[best].detach().cpu().numpy(), width, height)
    return float(scores[best]), box, red_pixel_ratio(image, box)


def draw_review_tile(
    image: np.ndarray, box: list[float] | None, status: str, score: float, name: str
) -> np.ndarray:
    tile = cv2.resize(image, (320, 240), interpolation=cv2.INTER_AREA)
    color = {"positive": (0, 255, 0), "negative": (255, 180, 0), "ambiguous": (0, 180, 255)}[
        status
    ]
    if box is not None:
        x1, y1, x2, y2 = box
        cv2.rectangle(
            tile,
            (round(x1 / image.shape[1] * 320), round(y1 / image.shape[0] * 240)),
            (round(x2 / image.shape[1] * 320), round(y2 / image.shape[0] * 240)),
            color,
            2,
        )
    cv2.rectangle(tile, (0, 0), (320, 34), (0, 0, 0), -1)
    cv2.putText(tile, f"{status} {score:.3f}", (5, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1)
    cv2.putText(tile, name[-24:], (5, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (255, 255, 255), 1)
    return tile


def write_contact_sheets(tiles: list[np.ndarray], destination: Path, prefix: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    per_sheet = 16
    blank = np.zeros((240, 320, 3), dtype=np.uint8)
    for page, start in enumerate(range(0, len(tiles), per_sheet), start=1):
        page_tiles = tiles[start : start + per_sheet]
        page_tiles.extend([blank] * (per_sheet - len(page_tiles)))
        rows = [np.hstack(page_tiles[row : row + 4]) for row in range(0, per_sheet, 4)]
        cv2.imwrite(str(destination / f"{prefix}-{page:02d}.jpg"), np.vstack(rows))


def materialize_reviewed(
    annotation_paths: list[Path], source_root: Path, destination_root: Path
) -> list[dict]:
    rows: list[dict] = []
    for annotation_path in annotation_paths:
        values = json.loads(annotation_path.read_text(encoding="utf-8"))
        if not isinstance(values, list):
            raise TypeError(f"annotation file must contain a list: {annotation_path}")
        for value in values:
            source = source_root / value["capture"] / value["frame"]
            if not source.is_file():
                raise FileNotFoundError(source)
            destination = destination_root / value["capture"] / value["frame"]
            if not destination.exists():
                link_or_copy(source, destination)
            rows.append(dict(value, label_source="manual-tonypi"))
    return rows


def annotate_iphone_sessions(args, model, captures_root: Path, holdout_root: Path):
    training_rows: list[dict] = []
    holdout_rows: list[dict] = []
    audit_rows: list[dict] = []
    review_tiles: dict[str, list[np.ndarray]] = {"train": [], "holdout": []}
    sessions = sorted(path for path in args.iphone_root.iterdir() if path.is_dir())
    if not sessions:
        raise ValueError(f"no iPhone sessions under {args.iphone_root}")
    if args.holdout_session not in {session.name for session in sessions}:
        raise ValueError(f"holdout session not found: {args.holdout_session}")

    for session in sessions:
        files = sorted(session.glob("*.jpg"))
        chosen = [files[index] for index in sample_indices(len(files), args.stride)]
        results = model.predict(
            [str(path) for path in chosen],
            imgsz=args.imgsz,
            conf=0.001,
            iou=0.7,
            max_det=20,
            classes=[0],
            device=args.device,
            batch=args.batch_size,
            stream=True,
            verbose=False,
        )
        is_holdout = session.name == args.holdout_session
        capture = f"iphone7-{session.name}"
        for source, result in zip(chosen, results, strict=True):
            image = cv2.imread(str(source))
            if image is None:
                raise ValueError(f"could not read {source}")
            score, box, ratio = teacher_prediction(result, image)
            status = classify_teacher_label(
                score,
                ratio,
                positive_confidence=args.positive_confidence,
                negative_confidence=args.negative_confidence,
                minimum_red_ratio=args.minimum_red_ratio,
            )
            target_root = holdout_root if is_holdout else captures_root
            destination = target_root / capture / source.name
            link_or_copy(source, destination)
            audit = {
                "capture": capture,
                "frame": source.name,
                "box": box if status == "positive" else None,
                "status": status,
                "teacher_score": score,
                "red_pixel_ratio": ratio,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
            audit_rows.append(dict(audit, partition="holdout" if is_holdout else "train"))
            review_tiles["holdout" if is_holdout else "train"].append(
                draw_review_tile(image, box, status, score, source.name)
            )
            # Low-confidence iPhone frames are safe hard negatives only after visual
            # review.  The selected training-session negatives were reviewed, while
            # the motion-heavy holdout contains blurred balls that the teacher misses.
            # Exclude those holdout negatives so replay measures positive recall/IoU
            # instead of treating teacher misses as ground-truth background.
            if status == "ambiguous" or (is_holdout and status == "negative"):
                continue
            row = {
                "capture": capture,
                "frame": source.name,
                "box": box if status == "positive" else None,
                "split": "train",
                "label_source": "v1-teacher-filtered",
                "teacher_score": score,
                "red_pixel_ratio": ratio,
            }
            (holdout_rows if is_holdout else training_rows).append(row)
    return training_rows, holdout_rows, audit_rows, review_tiles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iphone-root", type=Path, required=True)
    parser.add_argument("--tonypi-root", type=Path, required=True)
    parser.add_argument("--tonypi-annotations", type=Path, action="append", required=True)
    parser.add_argument("--teacher", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--holdout-session", default="session-20260916-140239-5E17C5")
    parser.add_argument("--stride", type=int, default=5)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--device", default="0")
    parser.add_argument("--positive-confidence", type=float, default=0.25)
    parser.add_argument("--negative-confidence", type=float, default=0.03)
    parser.add_argument("--minimum-red-ratio", type=float, default=0.12)
    args = parser.parse_args()
    if args.stride <= 0:
        parser.error("--stride must be positive")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("--output must be empty or absent")
    args.output.mkdir(parents=True, exist_ok=True)

    from ultralytics import YOLO

    captures_root = args.output / "captures"
    holdout_root = args.output / "holdout-captures"
    reviewed = materialize_reviewed(args.tonypi_annotations, args.tonypi_root, captures_root)
    model = YOLO(args.teacher)
    iphone_train, iphone_holdout, audit, review_tiles = annotate_iphone_sessions(
        args, model, captures_root, holdout_root
    )
    combined = reviewed + iphone_train
    (args.output / "annotations-combined.json").write_text(
        json.dumps(combined, indent=2), encoding="utf-8"
    )
    (args.output / "annotations-iphone-holdout.json").write_text(
        json.dumps(iphone_holdout, indent=2), encoding="utf-8"
    )
    (args.output / "iphone-label-audit.json").write_text(
        json.dumps(audit, indent=2), encoding="utf-8"
    )
    write_contact_sheets(review_tiles["train"], args.output / "review", "iphone-train")
    write_contact_sheets(review_tiles["holdout"], args.output / "review", "iphone-holdout")

    manifest = {
        "purpose": "YOLO11n red-ball model iteration v3",
        "teacher": str(args.teacher.resolve()),
        "teacher_sha256": hashlib.sha256(args.teacher.read_bytes()).hexdigest(),
        "policy": {
            "iphone_stride": args.stride,
            "positive_confidence": args.positive_confidence,
            "negative_confidence": args.negative_confidence,
            "minimum_red_ratio": args.minimum_red_ratio,
            "holdout_session": args.holdout_session,
            "ambiguous_frames": "excluded",
            "tonypi_labels": "manual; original train/test split preserved",
        },
        "counts": {
            "tonypi": dict(Counter("positive" if row["box"] else "negative" for row in reviewed)),
            "iphone_train": dict(
                Counter("positive" if row["box"] else "negative" for row in iphone_train)
            ),
            "iphone_holdout": dict(
                Counter("positive" if row["box"] else "negative" for row in iphone_holdout)
            ),
            "iphone_audit": dict(Counter(row["status"] for row in audit)),
            "combined": len(combined),
        },
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
