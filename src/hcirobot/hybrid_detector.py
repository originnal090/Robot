"""Experimental LAB-first SVM verification with ROI tracking and timed recovery.

No runtime selection is changed by this module. ``now`` allows capture replay at a
known frame rate instead of letting benchmark execution speed alter the schedule.
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from .detector import DetectorConfig
from .edge_detector import Box, EdgeBallDetector, box_iou, proposal_boxes
from .model import Detection


class HybridBallDetector(EdgeBallDetector):
    """Verify up to three LAB boxes; search widely only when fast paths fail."""

    recovery_interval = .3
    full_refresh_interval = .5
    max_lab_candidates = 3
    roi_scale = 3.0

    def __init__(self, model_path: str | Path, config: DetectorConfig, *,
                 recent_hit_recovery_seconds: float = .3,
                 reuse_frame_scores: bool = True,
                 fast_backend: bool = False,
                 expanded_geometry: bool = False) -> None:
        if not np.isfinite(recent_hit_recovery_seconds) or recent_hit_recovery_seconds < 0:
            raise ValueError('recent hit recovery seconds must be finite and nonnegative')
        self.recent_hit_recovery_seconds = recent_hit_recovery_seconds
        self.reuse_frame_scores = reuse_frame_scores
        self.fast_backend = fast_backend
        self.expanded_geometry = expanded_geometry
        super().__init__(model_path, config)
        if fast_backend:
            from .fast_features import BatchFeatureExtractor
            self.extractor = BatchFeatureExtractor(self.feature_version)
        self._reset_tracking()

    def _reset_tracking(self) -> None:
        self._hit_streak = self._miss_streak = 0
        self._confirmed = False
        self._tracked_box: Box | None = None
        self._image_shape: tuple[int, int] | None = None
        self._last_now: float | None = None
        self._last_wide = float('-inf')
        self._last_full = float('-inf')
        self._last_reliable_hit = float('-inf')
        self._reliable_box: Box | None = None
        self._frame_scores: dict[Box, float] = {}
        self.last_predictions: list[tuple[Box, float]] = []
        self.last_stats: dict[str, object] = {}

    def update_config(self, config: DetectorConfig) -> None:
        with self._lock:
            self.config = config
            self._hit_streak = self._miss_streak = 0
            self._confirmed = False
            self._reset_tracking()

    @staticmethod
    def _validate_frame(frame: np.ndarray) -> None:
        if frame is None or frame.ndim != 3 or frame.shape[2] != 3 or not frame.size:
            raise ValueError("frame must be a nonempty BGR image")

    def _roi(self, frame: np.ndarray) -> Box:
        assert self._tracked_box is not None
        x1, y1, x2, y2 = self._tracked_box
        # Keep a useful margin even for very small distant balls.
        mx = max(16, round((x2 - x1) * (self.roi_scale - 1) / 2))
        my = max(16, round((y2 - y1) * (self.roi_scale - 1) / 2))
        return (max(0, x1 - mx), max(0, y1 - my),
                min(frame.shape[1], x2 + mx), min(frame.shape[0], y2 + my))

    def _lab_boxes(self, frame: np.ndarray, roi: Box | None) -> list[Box]:
        height, width = frame.shape[:2]
        rx1, ry1, rx2, ry2 = roi or (0, 0, width, height)
        crop = frame[ry1:ry2, rx1:rx2]
        blur = max(1, self.config.gaussian_blur_kernel) | 1
        if blur > 1:
            crop = cv2.GaussianBlur(crop, (blur, blur), 0)
        lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB)
        mask = cv2.inRange(lab, self.config.lab_min, self.config.lab_max)
        size = max(1, self.config.morphology_kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((size, size), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        ranked: list[tuple[float, Box]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            cap = self.expanded_geometry and y + ry1 + h == height
            maximum_area = .65 if self.expanded_geometry else .35
            if (w < 5 or h < 4 or area < self.config.minimum_contour_area * .5
                    or w * h > width * height * maximum_area
                    or not .2 <= w / h <= (12 if cap else 5)):
                continue
            # A contour cut by a search ROI is incomplete. Recover using the full
            # image instead of feeding fake image-edge geometry to the classifier.
            if ((x == 0 and rx1 > 0) or (y == 0 and ry1 > 0)
                    or (x + w >= rx2 - rx1 and rx2 < width)
                    or (y + h >= ry2 - ry1 and ry2 < height)):
                continue
            perimeter = cv2.arcLength(contour, True)
            circularity = 4 * np.pi * area / max(perimeter * perimeter, 1)
            # Relaxed ranking only: clipped balls are not rejected by circularity,
            # aspect-ratio settings, or the strong-red-core gate of the LAB detector.
            rank = area * max(.2, circularity)
            ranked.append((rank, (x + rx1, y + ry1, x + rx1 + w, y + ry1 + h)))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [box for _, box in ranked[:self.max_lab_candidates]]

    def _classify(self, frame: np.ndarray, boxes: list[Box]) -> list[tuple[Box, float]]:
        if not boxes:
            return []
        # Always extract using full-frame coordinates: context and edge features
        # must be identical to the model's training representation.
        pending = list(dict.fromkeys(box for box in boxes if box not in self._frame_scores))
        if not self.reuse_frame_scores:
            pending = boxes
        self.last_stats['classified_count'] += len(pending)
        self.last_stats['score_cache_hits'] += len(boxes) - len(pending)
        if pending:
            features = self.extractor.extract_many(frame, pending) if self.fast_backend else (
                np.stack([self.extractor.extract(frame, box) for box in pending]))
            scores = features @ self.weights + self.bias
            self._frame_scores.update(zip(pending, map(float, scores), strict=True))
        scores = [self._frame_scores[box] for box in boxes]
        return sorted(((box, float(score)) for box, score in zip(boxes, scores, strict=True)
                       if score >= self.threshold), key=lambda item: item[1], reverse=True)

    def predict_boxes(self, frame: np.ndarray, *, now: float | None = None
                      ) -> list[tuple[Box, float]]:
        """Stateful search at supplied image size; does not update confirmation."""
        with self._lock:
            return self._predict_locked(frame, now=now)

    def _predict_locked(self, frame: np.ndarray, *, now: float | None) -> list[tuple[Box, float]]:
        self._validate_frame(frame)
        stamp = time.monotonic() if now is None else float(now)
        if not np.isfinite(stamp):
            raise ValueError('now must be finite')
        shape = frame.shape[:2]
        if self._image_shape != shape or (self._last_now is not None and stamp < self._last_now):
            self._reset_tracking()
        self._image_shape, self._last_now = shape, stamp
        self._frame_scores = {}  # Never reuse a prior image's classification.
        self.last_stats = {'route': 'miss', 'classified_count': 0, 'roi': None,
                           'full_fallback': False, 'wide_search': False,
                           'urgent_recovery': False, 'score_cache_hits': 0}
        predictions: list[tuple[Box, float]] = []
        use_roi = (self._tracked_box is not None
                   and stamp - self._last_full < self.full_refresh_interval)
        if use_roi:
            roi = self._roi(frame)
            self.last_stats['roi'] = roi
            predictions = self._classify(frame, self._lab_boxes(frame, roi))
            if predictions:
                self.last_stats['route'] = 'roi_lab'
        if not predictions:
            self.last_stats['full_fallback'] = use_roi
            self._last_full = stamp
            predictions = self._classify(frame, self._lab_boxes(frame, None))
            if predictions:
                self.last_stats['route'] = 'full_lab'
        recent_reliable_hit = (self.recent_hit_recovery_seconds > 0
                               and stamp - self._last_reliable_hit
                               <= self.recent_hit_recovery_seconds + 1e-9)
        if not predictions and (recent_reliable_hit or
                                stamp - self._last_wide >= self.recovery_interval - 1e-9):
            self._last_wide = stamp
            self.last_stats['wide_search'] = True
            self.last_stats['urgent_recovery'] = recent_reliable_hit
            boxes = proposal_boxes(frame, fast=self.fast_backend,
                                   expanded_geometry=self.expanded_geometry) if (
                self.fast_backend or self.expanded_geometry) else proposal_boxes(frame)
            predictions = self._classify(frame, boxes)
            if self.expanded_geometry:
                predictions = self._prefer_enclosing_positive(predictions)
            self.last_stats['route'] = 'wide_svm' if predictions else 'wide_miss'
        if predictions and (self.last_stats['route'] in ('roi_lab', 'full_lab') or
                            (recent_reliable_hit and self._reliable_box is not None
                             and box_iou(predictions[0][0], self._reliable_box) >= .1)):
            # A fast-path verified target seeds recovery. A spatially overlapping
            # wide detection can continue it. An isolated wide positive cannot
            # promote itself into high-rate search over background clutter.
            self._last_reliable_hit = stamp
            self._reliable_box = predictions[0][0]
        self._tracked_box = predictions[0][0] if predictions else None
        self.last_predictions = predictions
        return predictions

    @staticmethod
    def _prefer_enclosing_positive(predictions: list[tuple[Box, float]]
                                   ) -> list[tuple[Box, float]]:
        """Try a verified enclosing ball region instead of its small internal patch.

        Experimental: only already-positive boxes can replace the top prediction.
        Never invent an unseen full sphere behind the image boundary.
        """
        if not predictions:
            return predictions
        small = predictions[0][0]
        small_area = (small[2] - small[0]) * (small[3] - small[1])
        eligible = []
        for item in predictions:
            box = item[0]
            area = (box[2] - box[0]) * (box[3] - box[1])
            overlap = max(0, min(box[2], small[2]) - max(box[0], small[0])) * max(
                0, min(box[3], small[3]) - max(box[1], small[1]))
            if 1.4 <= area / small_area <= 8 and overlap >= small_area * .9:
                eligible.append((area, item))
        if not eligible:
            return predictions
        selected = max(eligible, key=lambda item: item[0])[1]
        return [selected] + [item for item in predictions if item != selected]

    def process(self, frame: np.ndarray, *, now: float | None = None) -> Detection:
        with self._lock:
            self._validate_frame(frame)
            height, width = frame.shape[:2]
            resized = cv2.resize(frame, (self.config.processing_width, self.config.processing_height),
                                 interpolation=cv2.INTER_AREA)
            candidates = self._predict_locked(resized, now=now)
            if not candidates:
                self._hit_streak = 0
                self._miss_streak += 1
                if self._miss_streak >= self.config.release_frames:
                    self._confirmed = False
                return Detection(False, self._confirmed, None, None, None, width, height,
                                 control_confirmed=False)
            self._hit_streak += 1
            self._miss_streak = 0
            if self._hit_streak >= self.config.confirmation_frames:
                self._confirmed = True
            (x1, y1, x2, y2), score = candidates[0]
            sx, sy = width / resized.shape[1], height / resized.shape[0]
            return Detection(
                True, self._confirmed, (x1 + x2) * sx / 2, (y1 + y2) * sy / 2,
                max((x2-x1)*sx, (y2-y1)*sy) / 2, width, height,
                area=float((x2-x1)*(y2-y1)), aspect_ratio=(x2-x1)/(y2-y1), score=score,
                control_confirmed=self._hit_streak >= self.config.confirmation_frames,
            )
