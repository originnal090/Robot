from __future__ import annotations

import importlib.util
from pathlib import Path

import cv2
import numpy as np

SCRIPT = Path(__file__).parents[1] / "tools" / "prepare_yolo_v3_dataset.py"
SPEC = importlib.util.spec_from_file_location("prepare_yolo_v3_dataset", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_sample_indices_include_last_without_duplicates() -> None:
    assert MODULE.sample_indices(0, 5) == []
    assert MODULE.sample_indices(1, 5) == [0]
    assert MODULE.sample_indices(11, 5) == [0, 5, 10]
    assert MODULE.sample_indices(12, 5) == [0, 5, 10, 11]


def test_red_pixel_ratio_is_limited_to_candidate_box() -> None:
    image = np.zeros((100, 100, 3), dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (59, 59), (0, 0, 255), -1)

    assert MODULE.red_pixel_ratio(image, [20, 20, 60, 60]) == 1.0
    assert MODULE.red_pixel_ratio(image, [60, 60, 100, 100]) == 0.0


def test_teacher_label_policy_excludes_ambiguous_frames() -> None:
    classify = MODULE.classify_teacher_label
    options = {
        "positive_confidence": 0.25,
        "negative_confidence": 0.03,
        "minimum_red_ratio": 0.12,
    }
    assert classify(0.9, 0.5, **options) == "positive"
    assert classify(0.9, 0.05, **options) == "ambiguous"
    assert classify(0.01, 0.0, **options) == "negative"
    assert classify(0.1, 0.5, **options) == "ambiguous"
