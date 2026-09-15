from __future__ import annotations

import gc
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .detector import DetectorConfig
from .model import Detection

DEFAULT_CONFIDENCE = 0.053634531795978546


@dataclass(frozen=True, slots=True)
class AscendYoloConfig:
    enabled: bool = False
    model: str = ""
    image_size: int = 640
    confidence: float = DEFAULT_CONFIDENCE
    iou: float = 0.45
    device: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("npu enabled must be a boolean")
        if self.enabled and not self.model.strip():
            raise ValueError("npu model path is required when enabled")
        if self.image_size <= 0:
            raise ValueError("npu image size must be positive")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("npu confidence must be between 0 and 1")
        if not math.isfinite(self.iou) or not 0 < self.iou <= 1:
            raise ValueError("npu iou must be in (0, 1]")
        if self.device < 0:
            raise ValueError("npu device must be non-negative")


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
    return tensor, {
        "original_size": (width, height),
        "ratio": ratio,
        "padding": (left, top),
    }


def _box_iou(one: np.ndarray, many: np.ndarray) -> np.ndarray:
    x1 = np.maximum(one[0], many[:, 0])
    y1 = np.maximum(one[1], many[:, 1])
    x2 = np.minimum(one[2], many[:, 2])
    y2 = np.minimum(one[3], many[:, 3])
    intersection = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    one_area = max(0.0, float(one[2] - one[0])) * max(0.0, float(one[3] - one[1]))
    many_area = np.maximum(0.0, many[:, 2] - many[:, 0]) * np.maximum(
        0.0, many[:, 3] - many[:, 1]
    )
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
        order = remaining[_box_iou(boxes[current], boxes[remaining]) <= iou_threshold]

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


class AscendYoloDetector:
    """Long-lived ACLLite YOLO detector for the Orange Pi Ascend NPU."""

    def __init__(self, npu: AscendYoloConfig, temporal: DetectorConfig) -> None:
        if not npu.enabled:
            raise ValueError("Ascend YOLO detector requires npu.enabled = true")
        model_path = Path(npu.model).expanduser()
        if not model_path.is_file():
            raise FileNotFoundError(f"Ascend OM model not found: {model_path}")
        # These modules are provided by the Orange Pi CANN/ACLLite installation.
        # Import only when this backend is selected so desktop LAB mode remains portable.
        from acllite_model import AclLiteModel
        from acllite_resource import AclLiteResource

        self.npu_config = npu
        self.config = temporal
        self._lock = threading.Lock()
        self._hit_streak = 0
        self._miss_streak = 0
        self._confirmed = False
        self._closed = False
        self._resource = AclLiteResource(npu.device)
        self._resource.init()
        try:
            self._model = AclLiteModel(str(model_path))
        except BaseException:
            self._resource.destroy()
            raise

    def update_config(self, config: DetectorConfig) -> None:
        with self._lock:
            self.config = config
            self._hit_streak = self._miss_streak = 0
            self._confirmed = False

    def _update_confirmation(self, candidate: bool) -> None:
        if candidate:
            self._hit_streak += 1
            self._miss_streak = 0
            if self._hit_streak >= self.config.confirmation_frames:
                self._confirmed = True
        else:
            self._miss_streak += 1
            self._hit_streak = 0
            if self._miss_streak >= self.config.release_frames:
                self._confirmed = False

    def process(self, frame: np.ndarray) -> Detection:
        if frame is None or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("frame must be a BGR image")
        height, width = frame.shape[:2]
        with self._lock:
            if self._closed:
                raise RuntimeError("Ascend YOLO detector is closed")
            rgb = Image.fromarray(frame[:, :, ::-1].copy(), "RGB")
            tensor, meta = letterbox(rgb, self.npu_config.image_size)
            output = self._model.execute([tensor])[0]
            candidates = decode_detections(
                output,
                meta,
                self.npu_config.confidence,
                self.npu_config.iou,
            )
            candidate = bool(candidates)
            self._update_confirmation(candidate)
            if not candidate:
                return Detection(
                    False,
                    self._confirmed,
                    None,
                    None,
                    None,
                    width,
                    height,
                    control_confirmed=False,
                )
            best = candidates[0]
            x1, y1, x2, y2 = best["box"]
            box_width = max(0.0, x2 - x1)
            box_height = max(0.0, y2 - y1)
            return Detection(
                candidate_detected=True,
                detected=self._confirmed,
                center_x=round((x1 + x2) / 2, 2),
                center_y=round((y1 + y2) / 2, 2),
                radius=round((box_width + box_height) / 4, 2),
                frame_width=width,
                frame_height=height,
                area=round(box_width * box_height, 2),
                aspect_ratio=round(box_width / box_height, 4) if box_height else 0.0,
                score=float(best["score"]),
                control_confirmed=self._hit_streak >= self.config.confirmation_frames,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            model, resource = self._model, self._resource
            self._model = self._resource = None
        model.destroy()
        resource.destroy()
        gc.collect()

