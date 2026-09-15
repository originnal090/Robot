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

from PIL import Image, ImageDraw

from hcirobot.ascend_yolo import DEFAULT_CONFIDENCE, decode_detections, letterbox


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
