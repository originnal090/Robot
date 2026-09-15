"""Run the red-ball YOLO OM model with AscendCL/ACLLite.

This entry point is intentionally independent from the robot service.  It can be
copied to an Orange Pi AI Pro and used to validate an OM model against one image.
"""

from __future__ import annotations

import argparse
import gc
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

DEFAULT_CONFIDENCE = 0.053634531795978546


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True, help="compiled Ascend .om model")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--image-size", type=int, default=640)
    parser.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE)
    parser.add_argument("--iou", type=float, default=0.45, help="NMS IoU threshold")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--annotated", type=Path, help="optional annotated image output")
    return parser.parse_args()


def letterbox(image: Image.Image, size: int) -> tuple[np.ndarray, dict[str, Any]]:
    """Resize with Ultralytics-compatible centered padding and return NCHW RGB."""
    image = image.convert("RGB")
    width, height = image.size
    ratio = min(size / width, size / height)
    resized_width = round(width * ratio)
    resized_height = round(height * ratio)
    pad_width = size - resized_width
    pad_height = size - resized_height
    left = round(pad_width / 2 - 0.1)
    top = round(pad_height / 2 - 0.1)

    if (resized_width, resized_height) != image.size:
        image = image.resize((resized_width, resized_height), Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (size, size), (114, 114, 114))
    canvas.paste(image, (left, top))

    tensor = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)
    tensor = np.ascontiguousarray(tensor[None] / 255.0)
    meta = {
        "original_size": (width, height),
        "ratio": ratio,
        "padding": (left, top),
    }
    return tensor, meta


def box_iou(one: np.ndarray, many: np.ndarray) -> np.ndarray:
    x1 = np.maximum(one[0], many[:, 0])
    y1 = np.maximum(one[1], many[:, 1])
    x2 = np.minimum(one[2], many[:, 2])
    y2 = np.minimum(one[3], many[:, 3])
    intersection = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    one_area = max(0.0, float(one[2] - one[0])) * max(0.0, float(one[3] - one[1]))
    many_area = np.maximum(0.0, many[:, 2] - many[:, 0]) * np.maximum(0.0, many[:, 3] - many[:, 1])
    return intersection / np.maximum(one_area + many_area - intersection, 1e-9)


def decode_detections(
    output: np.ndarray,
    meta: dict[str, Any],
    confidence: float,
    iou_threshold: float,
) -> list[dict[str, Any]]:
    """Decode a single-class YOLO11 output and apply class-agnostic NMS."""
    prediction = np.asarray(output)
    if prediction.ndim == 3:
        prediction = prediction[0]
    if prediction.shape[0] == 5:
        prediction = prediction.T
    if prediction.ndim != 2 or prediction.shape[1] != 5:
        raise ValueError(f"expected YOLO output shaped (1, 5, N), got {output.shape}")

    prediction = prediction[prediction[:, 4] >= confidence]
    if not len(prediction):
        return []

    xywh = prediction[:, :4]
    boxes = np.column_stack(
        (
            xywh[:, 0] - xywh[:, 2] / 2,
            xywh[:, 1] - xywh[:, 3] / 2,
            xywh[:, 0] + xywh[:, 2] / 2,
            xywh[:, 1] + xywh[:, 3] / 2,
        )
    )
    scores = prediction[:, 4]
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while len(order):
        current = int(order[0])
        keep.append(current)
        if len(order) == 1:
            break
        remaining = order[1:]
        order = remaining[box_iou(boxes[current], boxes[remaining]) <= iou_threshold]

    left, top = meta["padding"]
    ratio = float(meta["ratio"])
    width, height = meta["original_size"]
    detections = []
    for index in keep:
        box = boxes[index].copy()
        box[[0, 2]] = (box[[0, 2]] - left) / ratio
        box[[1, 3]] = (box[[1, 3]] - top) / ratio
        box[[0, 2]] = np.clip(box[[0, 2]], 0, width)
        box[[1, 3]] = np.clip(box[[1, 3]], 0, height)
        detections.append(
            {
                "box": [round(float(value), 3) for value in box],
                "score": round(float(scores[index]), 6),
                "class_id": 0,
                "class_name": "red_ball",
            }
        )
    return detections


def save_annotated(image: Image.Image, detections: list[dict[str, Any]], path: Path) -> None:
    annotated = image.convert("RGB")
    draw = ImageDraw.Draw(annotated)
    for detection in detections:
        box = tuple(detection["box"])
        draw.rectangle(box, outline=(255, 40, 40), width=3)
        draw.text(
            (box[0], max(0, box[1] - 12)), f"red_ball {detection['score']:.3f}", fill=(255, 40, 40)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    annotated.save(path)


def main() -> int:
    args = parse_args()
    if args.iterations < 1 or args.warmup < 0:
        raise ValueError("iterations must be positive and warmup must be non-negative")

    # ACLLite is supplied by CANN on the Orange Pi, so imports remain local to
    # main and preprocessing/postprocessing can be tested on a development PC.
    from acllite_model import AclLiteModel
    from acllite_resource import AclLiteResource

    source = Image.open(args.image)
    tensor, meta = letterbox(source, args.image_size)
    resource = AclLiteResource(args.device)
    model = None
    timings: list[float] = []
    output = None
    try:
        resource.init()
        model = AclLiteModel(str(args.model))
        for _ in range(args.warmup):
            model.execute([tensor])
        for _ in range(args.iterations):
            started = time.perf_counter()
            output = model.execute([tensor])[0]
            timings.append((time.perf_counter() - started) * 1000)
    finally:
        if model is not None:
            model.destroy()
            del model
        del resource
        gc.collect()

    assert output is not None
    detections = decode_detections(output, meta, args.confidence, args.iou)
    if args.annotated:
        save_annotated(source, detections, args.annotated)
    report = {
        "model": str(args.model),
        "image": str(args.image),
        "input_shape": list(tensor.shape),
        "output_shape": list(output.shape),
        "confidence": args.confidence,
        "iou": args.iou,
        "detections": detections,
        "timing_ms": {
            "iterations": args.iterations,
            "values": [round(value, 3) for value in timings],
            "mean": round(sum(timings) / len(timings), 3),
            "min": round(min(timings), 3),
            "max": round(max(timings), 3),
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
