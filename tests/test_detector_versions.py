import hashlib
import json
import runpy
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
import pytest

from hcirobot.detector import DetectorConfig
from hcirobot.detector_profile import load_detector
from hcirobot.edge_detector import FeatureExtractor, proposal_boxes
from hcirobot.fast_features import BatchFeatureExtractor
from hcirobot.hybrid_detector import HybridBallDetector


@pytest.mark.parametrize('version', [1, 2])
def test_batch_features_match_reference_with_context_and_image_edges(version):
    image = np.random.default_rng(27).integers(0, 256, (120, 160, 3), dtype=np.uint8)
    boxes = [(0, 0, 30, 35), (50, 90, 100, 120), (145, 10, 160, 55),
             (5, 10, 130, 80), (25, 25, 30, 29)]
    expected = np.stack([FeatureExtractor(version).extract(image, box) for box in boxes])
    actual = BatchFeatureExtractor(version).extract_many(image, boxes)
    np.testing.assert_allclose(actual, expected, atol=1e-7, rtol=1e-6)
    np.testing.assert_array_equal(actual[:, 324:], expected[:, 324:])
    for count in (0, 1, 3):
        small = BatchFeatureExtractor(version).extract_many(image, boxes[:count])
        np.testing.assert_array_equal(small, expected[:count])


def test_fast_proposals_preserve_order_and_deduplication():
    frame = np.full((480, 640, 3), 40, np.uint8)
    rng = np.random.default_rng(22)
    for _ in range(200):
        x, y = rng.integers([0, 0], [640, 480])
        cv2.circle(frame, (int(x), int(y)), int(rng.integers(2, 20)), (40, 60, 200), -1)
    assert proposal_boxes(frame, fast=True) == proposal_boxes(frame)


def test_enclosing_geometry_only_uses_verified_overlapping_boxes():
    small = ((50, 50, 60, 60), 2.)
    large = ((45, 45, 65, 65), .1)
    unrelated = ((100, 100, 140, 140), 1.)
    rerank = HybridBallDetector._prefer_enclosing_positive
    assert rerank([small, unrelated]) == [small, unrelated]
    assert rerank([small, unrelated, large])[0] == large
    assert rerank([]) == []


def test_expanded_geometry_accepts_visible_cap_and_large_ball_only_when_enabled():
    frame = np.full((480, 640, 3), 40, np.uint8)
    cv2.rectangle(frame, (200, 468), (300, 479), (40, 60, 200), -1)
    assert not proposal_boxes(frame)
    assert proposal_boxes(frame, fast=True, expanded_geometry=True)
    frame[:] = 40
    cv2.circle(frame, (320, 240), 180, (40, 60, 200), -1)
    assert not proposal_boxes(frame)
    assert proposal_boxes(frame, expanded_geometry=True)


def test_profile_loads_frozen_strategy_and_checks_weight_hash(tmp_path):
    weights = tmp_path / 'model.npz'
    np.savez(weights, feature_version=1, weights=np.zeros(406), bias=1., threshold=0.)
    profile = {'schema_version': 1, 'id': 'field-test', 'model': 'model.npz',
                   'model_sha256': hashlib.sha256(weights.read_bytes()).hexdigest(),
                   'strategy': 'hybrid', 'options': {'fast_backend': True, 'expanded_geometry': True,
                                                   'recent_hit_recovery_seconds': 0},
                   'detector_config': asdict(DetectorConfig(confirmation_frames=4))}
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(profile), encoding='utf-8')
    detector = load_detector(path, DetectorConfig())
    assert isinstance(detector, HybridBallDetector)
    assert detector.fast_backend and detector.expanded_geometry
    assert detector.recent_hit_recovery_seconds == 0
    assert detector.config.confirmation_frames == 4
    assert detector.profile_id == 'field-test'
    weights.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='checksum'):
        load_detector(path, DetectorConfig())


def test_training_refuses_to_overwrite_any_existing_model(tmp_path):
    functions = runpy.run_path(str(Path(__file__).parents[1] / 'tools/train_edge_detector.py'))
    model = tmp_path / 'kept.npz'
    model.write_bytes(b'original')
    with pytest.raises(FileExistsError, match='overwrite'):
        functions['fit_model'](np.array([[-1], [1]]), np.array([-1, 1]), model)
    assert model.read_bytes() == b'original'
