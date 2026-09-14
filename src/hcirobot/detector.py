from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from .model import Detection


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    # Earlier 200-frame calibration (artifacts/tonypi-video): the ball reads
    # L 111-182, A 133-190, B 115-140 while background clutter stays at A <= ~138, so the
    # A channel is the discriminator. Wide L/B bounds keep shaded ball edges (the ball is
    # only ~25 px wide) from being clipped into non-circular slivers; the previous narrow
    # defaults (55,145,118)-(190,195,150) clipped ~40% of the ball and produced zero
    # candidates on real frames.
    # 2026-09-14 capture replay (artifacts/lab-tuning-20260914): A >= 136 separates
    # the near ball from warm background connected at A >= 132.
    # The strongly-red core, shape and temporal gates remain necessary.
    lab_min: tuple[int, int, int] = (30, 136, 100)
    lab_max: tuple[int, int, int] = (220, 215, 150)
    processing_width: int = 640
    processing_height: int = 480
    gaussian_blur_kernel: int = 3
    morphology_kernel: int = 3
    minimum_contour_area: float = 50.0
    # A rasterized ~25 px disk scores circularity ~0.6-0.75 even when perfect, so 0.65
    # rejected real balls while 0.60 kept them across all 200 captured frames with no
    # background false picks.
    minimum_circularity: float = 0.60
    # Second gate against warm wood/foam textures (measured A <= ~147 everywhere in the
    # captured scene): a candidate blob must contain at least this fraction of strongly
    # red pixels (A >= core_a_min). The real ball scores ~0.6; set the fraction to 0 to
    # disable the gate.
    core_a_min: int = 160
    minimum_core_fraction: float = 0.15
    minimum_aspect_ratio: float = 0.60
    maximum_aspect_ratio: float = 1.67
    confirmation_frames: int = 3
    release_frames: int = 3

    def __post_init__(self) -> None:
        if len(self.lab_min) != 3 or len(self.lab_max) != 3:
            raise ValueError("LAB bounds must have three channels")
        if any(not 0 <= value <= 255 for value in (*self.lab_min, *self.lab_max)):
            raise ValueError("LAB bounds must be between 0 and 255")
        if any(low > high for low, high in zip(self.lab_min, self.lab_max, strict=True)):
            raise ValueError("each LAB lower bound must not exceed its upper bound")
        if self.processing_width <= 0 or self.processing_height <= 0:
            raise ValueError("processing dimensions must be positive")
        if self.minimum_contour_area < 0:
            raise ValueError("minimum contour area must be non-negative")
        if not 0 <= self.minimum_circularity <= 1:
            raise ValueError("minimum circularity must be between 0 and 1")
        if not 0 <= self.core_a_min <= 255:
            raise ValueError("core A minimum must be between 0 and 255")
        if self.core_a_min > self.lab_max[1]:
            raise ValueError("core A minimum must not exceed the A upper bound")
        if not 0 <= self.minimum_core_fraction <= 1:
            raise ValueError("minimum core fraction must be between 0 and 1")
        if not 0 < self.minimum_aspect_ratio <= self.maximum_aspect_ratio:
            raise ValueError("aspect ratio bounds are invalid")
        if self.confirmation_frames < 1 or self.release_frames < 1:
            raise ValueError("confirmation and release frames must be positive")


class RedBallDetector:
    """LAB/contour detector adapted from the course Orange Pi example."""

    def __init__(self, config: DetectorConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._lab_min = np.asarray(config.lab_min, dtype=np.uint8)
        self._lab_max = np.asarray(config.lab_max, dtype=np.uint8)
        self._core_lab_min = self._core_bounds(config)
        self._hit_streak = 0
        self._miss_streak = 0
        self._confirmed = False

    @staticmethod
    def _core_bounds(config: DetectorConfig) -> np.ndarray:
        return np.asarray(
            (config.lab_min[0], max(config.lab_min[1], config.core_a_min), config.lab_min[2]),
            dtype=np.uint8,
        )

    def update_config(self, config: DetectorConfig) -> None:
        """Hot-swap detector parameters (thread safe, for GUI runtime tuning).

        Resets the hit/miss streaks and the confirmed flag so the detector
        re-grounds its temporal state under the new thresholds instead of
        carrying a confirmation earned by the old ones.
        """
        with self._lock:
            self.config = config
            self._lab_min = np.asarray(config.lab_min, dtype=np.uint8)
            self._lab_max = np.asarray(config.lab_max, dtype=np.uint8)
            self._core_lab_min = self._core_bounds(config)
            self._hit_streak = 0
            self._miss_streak = 0
            self._confirmed = False

    @staticmethod
    def _odd(value: int) -> int:
        value = max(1, int(value))
        return value if value % 2 else value + 1

    def _update_confirmation(self, candidate: bool) -> None:
        if candidate:
            self._hit_streak += 1
            self._miss_streak = 0
            if self._hit_streak >= max(1, self.config.confirmation_frames):
                self._confirmed = True
        else:
            self._miss_streak += 1
            self._hit_streak = 0
            if self._miss_streak >= max(1, self.config.release_frames):
                self._confirmed = False

    def _has_core_support(self, contour: object, core_mask: NDArray[np.uint8], area: float) -> bool:
        """Require strongly red pixels inside the blob (rejects warm wood/foam texture)."""
        fraction = self.config.minimum_core_fraction
        if fraction <= 0:
            return True
        # Count only the candidate ROI. On Orange Pi this avoids allocating and
        # scanning a full 640x480 mask for every small background contour.
        x, y, width, height = cv2.boundingRect(contour)
        blob = np.zeros((height, width), dtype=np.uint8)
        cv2.drawContours(blob, [contour], -1, 255, -1, offset=(-x, -y))
        core_roi = core_mask[y : y + height, x : x + width]
        core_pixels = cv2.countNonZero(cv2.bitwise_and(blob, core_roi))
        return core_pixels >= fraction * area

    def process(self, frame: NDArray[np.uint8]) -> Detection:
        # Hold the lock for the whole pipeline so update_config() cannot swap
        # thresholds or temporal state mid-frame.
        with self._lock:
            return self._process_locked(frame)

    def _process_locked(self, frame: NDArray[np.uint8]) -> Detection:
        if frame is None or frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("frame must be a BGR image")
        original_height, original_width = frame.shape[:2]
        if original_width <= 0 or original_height <= 0:
            raise ValueError("frame dimensions must be positive")

        size = (self.config.processing_width, self.config.processing_height)
        resized = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        blur = self._odd(self.config.gaussian_blur_kernel)
        if blur > 1:
            resized = cv2.GaussianBlur(resized, (blur, blur), 0)
        lab = cv2.cvtColor(resized, cv2.COLOR_BGR2LAB)
        mask = cv2.inRange(lab, self._lab_min, self._lab_max)
        core_mask = cv2.inRange(lab, self._core_lab_min, self._lab_max)
        kernel_size = max(1, self.config.morphology_kernel)
        kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        core_mask = cv2.morphologyEx(core_mask, cv2.MORPH_OPEN, kernel)
        core_mask = cv2.morphologyEx(core_mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best: tuple[float, object, float, float, float] | None = None
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < self.config.minimum_contour_area:
                continue
            perimeter = float(cv2.arcLength(contour, True))
            if perimeter <= 0:
                continue
            circularity = 4.0 * math.pi * area / (perimeter * perimeter)
            _, _, width, height = cv2.boundingRect(contour)
            if height <= 0:
                continue
            aspect_ratio = width / height
            if circularity < self.config.minimum_circularity:
                continue
            if (
                not self.config.minimum_aspect_ratio
                <= aspect_ratio
                <= self.config.maximum_aspect_ratio
            ):
                continue
            if not self._has_core_support(contour, core_mask, area):
                continue
            score = area * circularity
            if best is None or score > best[0]:
                best = (score, contour, area, circularity, aspect_ratio)

        candidate = best is not None
        self._update_confirmation(candidate)
        if best is None:
            return Detection(
                False,
                self._confirmed,
                None,
                None,
                None,
                original_width,
                original_height,
                control_confirmed=False,
            )

        score, contour, area, circularity, aspect_ratio = best
        (center_x, center_y), radius = cv2.minEnclosingCircle(contour)
        scale_x = original_width / self.config.processing_width
        scale_y = original_height / self.config.processing_height
        return Detection(
            candidate_detected=True,
            detected=self._confirmed,
            center_x=round(center_x * scale_x, 2),
            center_y=round(center_y * scale_y, 2),
            radius=round(radius * (scale_x + scale_y) / 2.0, 2),
            frame_width=original_width,
            frame_height=original_height,
            area=round(area, 2),
            circularity=round(circularity, 4),
            aspect_ratio=round(aspect_ratio, 4),
            score=round(score, 2),
            control_confirmed=self._hit_streak >= self.config.confirmation_frames,
        )
