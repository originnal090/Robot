"""Opt-in batch implementation of the existing features, including v1 quantiles."""
from __future__ import annotations

import cv2
import numpy as np

from .edge_detector import Box, FeatureExtractor


class BatchFeatureExtractor(FeatureExtractor):
    def extract_many(self, frame: np.ndarray, boxes: list[Box]) -> np.ndarray:
        if not boxes:
            return np.empty((0, 406 if self.version == 1 else 388), np.float32)
        if len(boxes) < 4:
            return np.stack([self.extract(frame, box) for box in boxes])
        patches, geometry = [], []
        for x1, y1, x2, y2 in boxes:
            w, h = x2 - x1, y2 - y1
            if w <= 0 or h <= 0:
                raise ValueError('box must have positive dimensions')
            px, py = max(1, round(w * .15)), max(1, round(h * .15))
            crop = frame[max(0, y1-py):min(frame.shape[0], y2+py),
                         max(0, x1-px):min(frame.shape[1], x2+px)]
            if not crop.size:
                raise ValueError('box must intersect the frame')
            patches.append(cv2.resize(crop, (32, 32), interpolation=cv2.INTER_AREA))
            geometry.append([min(w/h, 5)/5, min(h/w, 5)/5,
                             float(y2 >= frame.shape[0]),
                             float(x1 <= 0 or x2 >= frame.shape[1])])
        # Convert together, but compute Sobel separately to preserve each patch's
        # reflected border. Stacking gray images before Sobel would change HOG.
        strip = np.concatenate(patches, axis=0)
        labs = cv2.cvtColor(strip, cv2.COLOR_BGR2LAB).reshape(-1, 32, 32, 3)
        labs = labs.astype(np.float32) / 255
        hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV).reshape(-1, 32, 32, 3)
        color = np.concatenate((labs, hsv.astype(np.float32) / 255), axis=3)
        color = color.reshape(-1, 1024, 6)
        histograms = []
        cells = (np.arange(32)[:, None] // 8) * 4 + np.arange(32)[None, :] // 8
        gray_patches = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY).reshape(-1, 32, 32)
        for gray in gray_patches:
            gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=1)
            gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=1)
            magnitude, angle = cv2.cartToPolar(gx, gy, angleInDegrees=True)
            bins = ((angle % 180) / 20).astype(np.int32)
            histograms.append(np.bincount((cells * 9 + bins).ravel(),
                                         weights=magnitude.ravel(), minlength=144).reshape(4, 4, 9))
        hist = np.stack(histograms)
        blocks = np.stack([hist[:, y:y+2, x:x+2].reshape(len(boxes), 36)
                           for y in range(3) for x in range(3)], axis=1)
        blocks /= np.sqrt(np.sum(blocks * blocks, axis=2, keepdims=True) + 1e-6)
        blocks = np.minimum(blocks, .2)
        blocks /= np.sqrt(np.sum(blocks * blocks, axis=2, keepdims=True) + 1e-6)
        parts = [blocks.reshape(len(boxes), 324).astype(np.float32),
                 color.mean(axis=1), color.std(axis=1)]
        if self.version == 1:
            parts.append(np.quantile(color, [.1, .5, .9], axis=1)
                         .transpose(1, 0, 2).reshape(len(boxes), 18))
        parts.extend([np.stack([cv2.resize(lab, (4, 4), interpolation=cv2.INTER_AREA).ravel()
                                for lab in labs]), np.asarray(geometry)])
        return np.concatenate(parts, axis=1).astype(np.float32)
