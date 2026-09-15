import runpy
from pathlib import Path

import cv2
import numpy as np
import pytest

from hcirobot.detector import DetectorConfig
from hcirobot.edge_detector import (
    FEATURE_VERSION,
    EdgeBallDetector,
    FeatureExtractor,
    box_iou,
    proposal_boxes,
)


def constant_positive_model(path: Path, version: int = FEATURE_VERSION) -> Path:
    size = FeatureExtractor(version=version).extract(
        np.zeros((32, 32, 3), np.uint8), (0, 0, 32, 32)
    ).size
    np.savez(path, weights=np.zeros(size, np.float32), bias=np.float32(1),
             threshold=np.float32(0), feature_version=np.int32(version))
    return path


def test_edge_proposals_include_bottom_clipped_ball() -> None:
    frame = np.full((480, 640, 3), 40, np.uint8)
    cv2.circle(frame, (320, 480), 35, (50, 70, 210), -1)
    boxes = proposal_boxes(frame)
    assert any(box_iou(box, (285, 445, 356, 480)) > .8 for box in boxes)


@pytest.mark.parametrize("version", [1, 2])
def test_edge_model_preserves_confirmation_and_current_target_safety(
    tmp_path: Path, version: int
) -> None:
    detector = EdgeBallDetector(
        constant_positive_model(tmp_path / "model.npz", version), DetectorConfig()
    )
    assert detector.feature_version == version
    assert detector.extractor.version == version
    frame = np.full((480, 640, 3), 40, np.uint8)
    cv2.circle(frame, (320, 240), 30, (50, 70, 210), -1)
    hits = [detector.process(frame) for _ in range(3)]
    assert [hit.has_current_target for hit in hits] == [False, False, True]
    assert hits[-1].center_x == pytest.approx(320, abs=1)
    blank = np.full_like(frame, 40)
    miss = detector.process(blank)
    assert miss.detected and not miss.has_current_target and miss.center_x is None
    reacquired = detector.process(frame)
    assert reacquired.detected and not reacquired.has_current_target
    misses = [detector.process(blank) for _ in range(3)]
    assert [miss.detected for miss in misses] == [True, True, False]
    detector.process(frame)
    detector.update_config(DetectorConfig())
    assert not detector.process(frame).detected


def test_feature_v2_is_exactly_v1_without_color_quantiles() -> None:
    frame = np.random.default_rng(20260914).integers(0, 256, (48, 64, 3), dtype=np.uint8)
    box = (8, 6, 50, 43)
    original = FeatureExtractor().extract(frame, box)
    explicit_v1 = FeatureExtractor(version=1).extract(frame, box)
    v2 = FeatureExtractor(version=2).extract(frame, box)
    assert FEATURE_VERSION == 1  # Existing callers and model files keep the original default.
    assert original.shape == (406,)
    assert v2.shape == (388,)
    np.testing.assert_array_equal(original, explicit_v1)
    # 324 HOG + 12 means/std precede the 18 quantiles; grid/geometry follow them.
    np.testing.assert_array_equal(v2, np.concatenate((original[:336], original[354:])))


def test_feature_v2_does_not_compute_quantiles(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("version 2 must not compute quantiles")

    monkeypatch.setattr(np, "quantile", forbidden)
    result = FeatureExtractor(version=2).extract(
        np.zeros((32, 32, 3), np.uint8), (0, 0, 32, 32)
    )
    assert result.shape == (388,)
    assert np.isfinite(result).all()


@pytest.mark.parametrize("version", [0, 3, -1])
def test_edge_model_rejects_unsupported_feature_version(tmp_path: Path, version: int) -> None:
    with pytest.raises(ValueError, match="unsupported.*version"):
        FeatureExtractor(version=version)
    path = tmp_path / "unsupported.npz"
    np.savez(path, weights=np.zeros(406), bias=0, threshold=0, feature_version=version)
    with pytest.raises(ValueError, match="unsupported.*version"):
        EdgeBallDetector(path, DetectorConfig())


def test_edge_model_rejects_missing_or_invalid_weights(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="missing.npz"):
        EdgeBallDetector(tmp_path / "missing.npz", DetectorConfig())
    bad = tmp_path / "bad.npz"
    np.savez(bad, weights=np.array([np.nan]), bias=0, threshold=0,
             feature_version=FEATURE_VERSION)
    with pytest.raises(ValueError, match="weights"):
        EdgeBallDetector(bad, DetectorConfig())


@pytest.mark.parametrize("version", [1, 2])
def test_linear_svm_fits_known_separable_data_and_exports_signed_margin(
    tmp_path: Path, version: int
) -> None:
    functions = runpy.run_path(str(Path(__file__).parents[1] / "tools/train_edge_detector.py"))
    x = np.asarray([[-3, 0], [-2, 1], [-2, -1], [2, 0], [3, 1], [3, -1]], np.float32)
    y = np.asarray([-1, -1, -1, 1, 1, 1], np.int32)
    model_path = tmp_path / "trained.npz"
    report = functions["fit_model"](x, y, model_path, feature_version=version)
    with np.load(model_path, allow_pickle=False) as model:
        margin = x @ model["weights"] + model["bias"]
        assert np.all(margin * y > .5)
        assert model["weights"][0] > 0
        assert float(model["threshold"]) == 0
        assert int(model["feature_version"]) == version
    assert report["solver_converged"]
    assert report["feature_version"] == version


def test_linear_svm_rejects_non_positive_epoch_limit(tmp_path: Path) -> None:
    functions = runpy.run_path(str(Path(__file__).parents[1] / "tools/train_edge_detector.py"))
    with pytest.raises(ValueError, match="max_epochs must be positive"):
        functions["fit_model"](
            np.asarray([[-1], [1]], np.float32),
            np.asarray([-1, 1], np.int32),
            tmp_path / "trained.npz",
            max_epochs=0,
        )


def test_training_rejects_identical_images_across_splits(tmp_path: Path) -> None:
    import json

    functions = runpy.run_path(str(Path(__file__).parents[1] / "tools/train_edge_detector.py"))
    capture = tmp_path / "capture"
    capture.mkdir()
    image = np.zeros((48, 64, 3), np.uint8)
    cv2.imwrite(str(capture / "frame.png"), image)
    annotations = tmp_path / "annotations.json"
    annotations.write_text(json.dumps([
        {"capture": "capture", "frame": "frame.png", "box": None, "split": split}
        for split in ("train", "test")
    ]), encoding="utf-8")
    with pytest.raises(ValueError, match="identical image content"):
        functions["load_samples"](annotations, tmp_path)
