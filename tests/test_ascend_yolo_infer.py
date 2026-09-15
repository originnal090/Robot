from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
from PIL import Image

SCRIPT = Path(__file__).parents[1] / "tools" / "ascend_yolo_infer.py"
SPEC = importlib.util.spec_from_file_location("ascend_yolo_infer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_letterbox_shape_and_padding() -> None:
    tensor, meta = MODULE.letterbox(Image.new("RGB", (640, 480), (255, 0, 0)), 640)
    assert tensor.shape == (1, 3, 640, 640)
    assert tensor.dtype == np.float32
    assert meta == {"original_size": (640, 480), "ratio": 1.0, "padding": (0, 80)}
    np.testing.assert_allclose(tensor[0, :, 0, 0], np.full(3, 114 / 255))
    np.testing.assert_allclose(tensor[0, :, 80, 0], (1.0, 0.0, 0.0))


def test_decode_and_nms_restore_original_coordinates() -> None:
    output = np.zeros((1, 5, 3), dtype=np.float32)
    output[0, :, 0] = (100, 180, 40, 20, 0.9)
    output[0, :, 1] = (102, 180, 40, 20, 0.8)
    output[0, :, 2] = (300, 380, 20, 20, 0.2)
    detections = MODULE.decode_detections(
        output,
        {"original_size": (640, 480), "ratio": 1.0, "padding": (0, 80)},
        confidence=0.5,
        iou_threshold=0.45,
    )
    assert detections == [
        {
            "box": [80.0, 90.0, 120.0, 110.0],
            "score": 0.9,
            "class_id": 0,
            "class_name": "red_ball",
        }
    ]
