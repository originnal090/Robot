"""Small CPU-only red-ball proposal classifier; no torch, NPU or pickle required."""
from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np

from .detector import DetectorConfig
from .model import Detection

Box = tuple[int, int, int, int]
FEATURE_VERSION = 1
SUPPORTED_FEATURE_VERSIONS = (1, 2)


def box_iou(a: Box, b: Box) -> float:
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap
    return overlap / union if union > 0 else 0.0


def proposal_boxes(frame: np.ndarray, *, fast: bool = False,
                   expanded_geometry: bool = False) -> list[Box]:
    """Loose red proposals: intentionally no circularity or bottom-edge rejection."""
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    masks = [cv2.inRange(lab, (15, a, 95), (255, 255, 185)) for a in (138, 150, 165)]
    masks.append(cv2.bitwise_or(
        cv2.inRange(hsv, (0, 45, 25), (13, 255, 255)),
        cv2.inRange(hsv, (165, 45, 25), (179, 255, 255)),
    ))
    candidates: list[Box] = []
    accepted = np.empty((256, 4), np.int32) if fast else None
    for mask in masks:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            maximum_area = .65 if expanded_geometry else .35
            if w < 5 or h < 4 or w * h < 35 or w * h > frame.shape[0] * frame.shape[1] * maximum_area:
                continue
            cap = expanded_geometry and y + h == frame.shape[0]
            if not .2 <= w / h <= (12 if cap else 5):
                continue
            box = (x, y, x + w, y + h)
            if fast and candidates:
                others = accepted[:len(candidates)]
                overlap = np.maximum(0, np.minimum(others[:, 2:], box[2:])
                                     - np.maximum(others[:, :2], box[:2])).prod(axis=1)
                union = w * h + (others[:, 2:] - others[:, :2]).prod(axis=1) - overlap
                duplicate = bool(np.any(overlap > .8 * union))
            else:
                duplicate = any(box_iou(box, other) > .8 for other in candidates)
            if not duplicate:
                if fast:
                    if len(candidates) == len(accepted):
                        accepted = np.concatenate((accepted, np.empty_like(accepted)))
                    accepted[len(candidates)] = box
                candidates.append(box)
    # Bound work on cluttered frames. Stable area ordering also makes training reproducible.
    return sorted(candidates, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)[:128]


class FeatureExtractor:
    """Version 2 is a controlled ablation: version 1 minus 18 color quantiles."""

    def __init__(self, version: int = FEATURE_VERSION) -> None:
        if version not in SUPPORTED_FEATURE_VERSIONS:
            raise ValueError(f"unsupported edge model feature version: {version}")
        self.version = version

    @staticmethod
    def _hog(gray: np.ndarray) -> np.ndarray:
        # OpenCV 5 wheels may omit objdetect/HOGDescriptor; use portable Sobel + NumPy.
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=1)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=1)
        magnitude, angle = cv2.cartToPolar(gx, gy, angleInDegrees=True)
        bins = ((angle % 180) / 20).astype(np.int32)
        cells = (np.arange(32)[:, None] // 8) * 4 + np.arange(32)[None, :] // 8
        histogram = np.bincount((cells * 9 + bins).ravel(), weights=magnitude.ravel(),
                                minlength=144).reshape(4, 4, 9)
        blocks = []
        for y in range(3):
            for x in range(3):
                block = histogram[y:y+2, x:x+2].ravel()
                block = block / np.sqrt(np.dot(block, block) + 1e-6)
                block = np.minimum(block, .2)
                blocks.append(block / np.sqrt(np.dot(block, block) + 1e-6))
        return np.concatenate(blocks).astype(np.float32)

    def extract(self, frame: np.ndarray, box: Box) -> np.ndarray:
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        if w <= 0 or h <= 0:
            raise ValueError("box must have positive dimensions")
        # Include a small border: the surrounding edge helps separate a ball from fabric.
        px, py = max(1, round(w * .15)), max(1, round(h * .15))
        crop = frame[max(0, y1-py):min(frame.shape[0], y2+py),
                     max(0, x1-px):min(frame.shape[1], x2+px)]
        if not crop.size:
            raise ValueError("box must intersect the frame")
        patch = cv2.resize(crop, (32, 32), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        hog = self._hog(gray)
        lab = cv2.cvtColor(patch, cv2.COLOR_BGR2LAB).astype(np.float32) / 255
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).astype(np.float32) / 255
        color = np.concatenate((lab, hsv), axis=2)
        statistics_parts = [color.mean((0, 1)), color.std((0, 1))]
        if self.version == 1:
            statistics_parts.append(np.quantile(color, [.1, .5, .9], axis=(0, 1)).ravel())
        statistics = np.concatenate(statistics_parts)
        cells = cv2.resize(lab, (4, 4), interpolation=cv2.INTER_AREA).ravel()
        geometry = [min(w / h, 5) / 5, min(h / w, 5) / 5,
                    float(y2 >= frame.shape[0]), float(x1 <= 0 or x2 >= frame.shape[1])]
        return np.concatenate((hog, statistics, cells, geometry)).astype(np.float32)


class EdgeBallDetector:
    """Drop-in Detection producer using exported linear SVM weights."""

    def __init__(self, model_path: str | Path, config: DetectorConfig) -> None:
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"edge model does not exist: {path}")
        with np.load(path, allow_pickle=False) as model:
            self.feature_version = int(model["feature_version"])
            self.extractor = FeatureExtractor(version=self.feature_version)
            self.weights = model["weights"].astype(np.float32)
            self.bias = float(model["bias"])
            self.threshold = float(model["threshold"])
        expected = self.extractor.extract(np.zeros((32, 32, 3), np.uint8), (0, 0, 32, 32)).size
        if self.weights.shape != (expected,) or not np.isfinite(self.weights).all():
            raise ValueError("invalid edge model weights")
        if not np.isfinite([self.bias, self.threshold]).all():
            raise ValueError("invalid edge model decision values")
        self.config = config
        self._lock = threading.Lock()
        self._hit_streak = self._miss_streak = 0
        self._confirmed = False

    def update_config(self, config: DetectorConfig) -> None:
        with self._lock:
            self.config = config
            self._hit_streak = self._miss_streak = 0
            self._confirmed = False

    def predict_boxes(self, frame: np.ndarray) -> list[tuple[Box, float]]:
        """Classify at the supplied image size (no temporal filtering)."""
        boxes = proposal_boxes(frame)
        if not boxes:
            return []
        features = np.stack([self.extractor.extract(frame, box) for box in boxes])
        scores = features @ self.weights + self.bias
        return sorted(((box, float(score)) for box, score in zip(boxes, scores, strict=True)
                       if score >= self.threshold), key=lambda item: item[1], reverse=True)

    def process(self, frame: np.ndarray) -> Detection:
        with self._lock:
            if frame is None or frame.ndim != 3 or frame.shape[2] != 3 or not frame.size:
                raise ValueError("frame must be a nonempty BGR image")
            height, width = frame.shape[:2]
            resized = cv2.resize(frame, (self.config.processing_width, self.config.processing_height),
                                 interpolation=cv2.INTER_AREA)
            candidates = self.predict_boxes(resized)
            if candidates:
                self._hit_streak += 1
                self._miss_streak = 0
                if self._hit_streak >= self.config.confirmation_frames:
                    self._confirmed = True
            else:
                self._hit_streak = 0
                self._miss_streak += 1
                if self._miss_streak >= self.config.release_frames:
                    self._confirmed = False
                return Detection(False, self._confirmed, None, None, None, width, height,
                                 control_confirmed=False)
            (x1, y1, x2, y2), score = candidates[0]
            sx, sy = width / resized.shape[1], height / resized.shape[0]
            return Detection(
                True, self._confirmed, (x1 + x2) * sx / 2, (y1 + y2) * sy / 2,
                max((x2-x1)*sx, (y2-y1)*sy) / 2, width, height,
                area=float((x2-x1)*(y2-y1)), aspect_ratio=(x2-x1)/(y2-y1), score=score,
                control_confirmed=self._hit_streak >= self.config.confirmation_frames,
            )
