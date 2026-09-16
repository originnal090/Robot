from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path
from typing import ClassVar

import numpy as np
from PIL import Image

from hcirobot import ascend_yolo
from hcirobot.ascend_yolo import AscendYoloConfig, AscendYoloDetector
from hcirobot.detector import DetectorConfig

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


def test_bottom_clipped_yolo_box_uses_horizontal_radius(monkeypatch) -> None:
    class Model:
        @staticmethod
        def execute(_inputs):
            return [np.zeros((1, 5, 1), dtype=np.float32)]

    detector = object.__new__(AscendYoloDetector)
    detector.npu_config = AscendYoloConfig(enabled=True, model="unused.om")
    detector.config = DetectorConfig(confirmation_frames=1)
    detector._lock = threading.Lock()
    detector._hit_streak = detector._miss_streak = 0
    detector._confirmed = False
    detector._closed = False
    detector._model = Model()
    monkeypatch.setattr(
        ascend_yolo,
        "decode_detections",
        lambda *_args, **_kwargs: [{"box": [220.0, 430.0, 420.0, 480.0], "score": 0.9}],
    )

    detection = detector.process(np.zeros((480, 640, 3), dtype=np.uint8))

    assert detection.bottom_clipped is True
    assert detection.bottom_ratio == 1.0
    assert detection.radius == 100.0
    assert detection.radius_ratio == 100 / 480


def test_detector_close_accepts_acllite_resource_without_public_destroy() -> None:
    class Model:
        destroyed = 0

        def destroy(self):
            self.destroyed += 1

    detector = object.__new__(AscendYoloDetector)
    detector._lock = threading.Lock()
    detector._closed = False
    detector._model = Model()
    detector._resource = object()
    model = detector._model

    detector.close()
    detector.close()

    assert model.destroyed == 1


def test_detectors_share_process_acl_runtime_between_sessions(tmp_path, monkeypatch) -> None:
    current_context = threading.local()

    class Resource:
        instances = 0
        initializations = 0

        def __init__(self, device):
            type(self).instances += 1
            self.device = device
            self.context = object()

        def init(self):
            type(self).initializations += 1

    class Runtime:
        contexts: ClassVar[list] = []

        @classmethod
        def set_context(cls, context):
            current_context.value = context
            cls.contexts.append(context)
            return 0

    class Model:
        instances: ClassVar[list] = []

        def __init__(self, path):
            assert current_context.value is ascend_yolo._acl_resource.context
            self.path = path
            self.destroyed = 0
            type(self).instances.append(self)

        def destroy(self):
            self.destroyed += 1

    resource_module = types.ModuleType("acllite_resource")
    resource_module.AclLiteResource = Resource
    model_module = types.ModuleType("acllite_model")
    model_module.AclLiteModel = Model
    acl_module = types.ModuleType("acl")
    acl_module.rt = Runtime
    monkeypatch.setitem(sys.modules, "acllite_resource", resource_module)
    monkeypatch.setitem(sys.modules, "acllite_model", model_module)
    monkeypatch.setitem(sys.modules, "acl", acl_module)
    monkeypatch.setattr(ascend_yolo, "_acl_resource", None)
    monkeypatch.setattr(ascend_yolo, "_acl_resource_device", None)
    model_path = tmp_path / "model.om"
    model_path.write_bytes(b"test")
    npu = AscendYoloConfig(enabled=True, model=str(model_path))

    failures = []

    def run_session():
        try:
            detector = AscendYoloDetector(npu, DetectorConfig())
            detector.close()
        except BaseException as exc:  # noqa: BLE001 - propagate worker failures below.
            failures.append(exc)

    first = threading.Thread(target=run_session)
    first.start()
    first.join()
    resource = ascend_yolo._acl_resource
    second = threading.Thread(target=run_session)
    second.start()
    second.join()

    assert failures == []
    assert Resource.instances == 1
    assert Resource.initializations == 1
    assert Runtime.contexts == [resource.context, resource.context]
    assert [model.destroyed for model in Model.instances] == [1, 1]
