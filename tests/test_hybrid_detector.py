from pathlib import Path

import cv2
import numpy as np
import pytest

from hcirobot import hybrid_detector
from hcirobot.detector import DetectorConfig
from hcirobot.edge_detector import FeatureExtractor, box_iou
from hcirobot.hybrid_detector import HybridBallDetector


@pytest.fixture
def detector(tmp_path: Path) -> HybridBallDetector:
    size = FeatureExtractor().extract(np.zeros((32, 32, 3), np.uint8), (0, 0, 32, 32)).size
    path = tmp_path / 'model.npz'
    np.savez(path, weights=np.zeros(size, np.float32), bias=1., threshold=0., feature_version=1)
    return HybridBallDetector(path, DetectorConfig())


def ball_frame(center: tuple[int, int] = (320, 240)) -> np.ndarray:
    image = np.full((480, 640, 3), 40, np.uint8)
    color = cv2.cvtColor(np.array([[[140, 180, 125]]], np.uint8), cv2.COLOR_LAB2BGR)[0, 0]
    cv2.circle(image, center, 25, tuple(int(value) for value in color), -1)
    return image


def test_roi_uses_full_frame_features_and_same_frame_recovery(detector, monkeypatch) -> None:
    frame = ball_frame()
    first = detector.predict_boxes(frame, now=0.)
    original_extract = detector.extractor.extract
    calls = []

    def extract(full_frame, box):
        calls.append((full_frame.shape, box))
        return original_extract(full_frame, box)

    monkeypatch.setattr(detector.extractor, 'extract', extract)
    second = detector.predict_boxes(frame, now=.1)
    assert detector.last_stats['route'] == 'roi_lab'
    assert second[0][0] == first[0][0]
    assert calls == [(frame.shape, first[0][0])]
    moved = detector.predict_boxes(ball_frame((500, 350)), now=.2)
    assert detector.last_stats['full_fallback'] is True
    assert detector.last_stats['route'] == 'full_lab'
    assert box_iou(moved[0][0], (475, 325, 526, 376)) > .85


def test_roi_artificial_boundary_forces_full_image_recovery(detector) -> None:
    detector.predict_boxes(ball_frame(), now=0.)
    # Right ROI border is around x=399; shifted ball intersects that border.
    boxes = detector.predict_boxes(ball_frame((395, 240)), now=.1)
    assert detector.last_stats['full_fallback'] is True
    assert detector.last_stats['route'] == 'full_lab'
    assert box_iou(boxes[0][0], (370, 215, 421, 266)) > .85


def test_bottom_clipping_is_valid_and_roi_is_clamped(detector) -> None:
    frame = ball_frame((635, 475))
    first = detector.predict_boxes(frame, now=0.)
    assert first and first[0][0][2:] == (640, 480)
    second = detector.predict_boxes(frame, now=.1)
    assert second[0][0] == first[0][0]
    assert detector.last_stats['route'] == 'roi_lab'
    assert detector.last_stats['roi'][2:] == (640, 480)
    features = detector.extractor.extract(frame, second[0][0])
    assert features[-2:].tolist() == [1., 1.]


def test_wide_search_cooldown_never_reuses_old_positive(detector, monkeypatch) -> None:
    frame = ball_frame()
    monkeypatch.setattr(detector, '_lab_boxes', lambda *_: [])
    calls = []

    def wide(_):
        calls.append(True)
        return [(295, 215, 346, 266)]

    monkeypatch.setattr(hybrid_detector, 'proposal_boxes', wide)
    hits = [detector.process(frame, now=stamp) for stamp in (0., .1, .2, .3)]
    assert [hit.candidate_detected for hit in hits] == [True, False, False, True]
    assert len(calls) == 2
    assert not any(hit.has_current_target for hit in hits)
    assert hits[1].center_x is None


def test_loss_clears_control_target_and_reacquisition_needs_fresh_streak(detector) -> None:
    frame = ball_frame()
    assert [detector.process(frame, now=i / 10).has_current_target
            for i in range(3)] == [False, False, True]
    miss = detector.process(np.full_like(frame, 40), now=.3)
    assert miss.detected and not miss.has_current_target and miss.center_x is None
    assert detector.last_predictions == []
    assert detector._tracked_box is None
    reacquired = detector.process(frame, now=.4)
    assert reacquired.candidate_detected and not reacquired.has_current_target
    assert detector.last_stats['route'] == 'full_lab'
    detector.update_config(DetectorConfig())
    assert not detector.process(frame, now=.5).detected


def test_periodic_full_lab_refresh_and_candidate_budget(detector) -> None:
    frame = ball_frame()
    for center in [(100, 100), (200, 100), (400, 100), (500, 100)]:
        other = ball_frame(center)
        frame = np.maximum(frame, other)
    detector.predict_boxes(frame, now=0.)
    assert detector.last_stats['classified_count'] == 3
    detector.predict_boxes(frame, now=.1)
    assert detector.last_stats['route'] == 'roi_lab'
    detector.predict_boxes(frame, now=.5)
    assert detector.last_stats['route'] == 'full_lab'
    assert detector.last_stats['classified_count'] == 3


def test_predict_rejects_invalid_input_and_resets_schedule_on_time_rewind(detector) -> None:
    with pytest.raises(ValueError, match='BGR'):
        detector.predict_boxes(np.zeros((20, 20), np.uint8), now=0.)
    with pytest.raises(ValueError, match='finite'):
        detector.predict_boxes(ball_frame(), now=float('nan'))
    detector.predict_boxes(ball_frame(), now=10.)
    detector.predict_boxes(ball_frame(), now=0.)
    assert detector.last_stats['route'] == 'full_lab'
    for stamp in (.1, .2, .3):
        hit = detector.process(ball_frame(), now=stamp)
    assert hit.has_current_target
    assert not detector.process(ball_frame(), now=0.).has_current_target


def test_recent_verified_target_gets_fresh_wide_recovery_then_expires(detector, monkeypatch):
    frame = ball_frame()
    for i in range(3):
        result = detector.process(frame, now=i / 10)
    assert result.has_current_target
    monkeypatch.setattr(detector, '_lab_boxes', lambda *_: [])
    calls = []
    available = True

    def wide(_):
        calls.append(True)
        return [(295, 215, 346, 266)] if available else []

    monkeypatch.setattr(hybrid_detector, 'proposal_boxes', wide)
    for stamp in (.3, .4, .5, .6):
        result = detector.process(frame, now=stamp)
        assert result.has_current_target
        assert detector.last_stats['urgent_recovery']
    assert len(calls) == 4
    available = False
    for stamp in (.7, .8, .9):
        result = detector.process(frame, now=stamp)
        assert not result.candidate_detected and not result.has_current_target
        assert result.center_x is None
    assert len(calls) == 7
    detector.process(frame, now=1.0)
    assert not detector.last_stats['wide_search']
    assert len(calls) == 7
    detector.process(frame, now=1.2)
    assert detector.last_stats['wide_search']
    assert not detector.last_stats['urgent_recovery']


def test_wide_box_far_from_reliable_target_cannot_extend_fast_recovery(detector, monkeypatch):
    frame = ball_frame()
    detector.process(frame, now=0.)
    monkeypatch.setattr(detector, '_lab_boxes', lambda *_: [])
    monkeypatch.setattr(hybrid_detector, 'proposal_boxes', lambda _: [(500, 350, 550, 400)])
    for stamp in (.1, .2, .3):
        detector.process(frame, now=stamp)
        assert detector.last_stats['urgent_recovery']
    detector.process(frame, now=.4)
    assert not detector.last_stats['wide_search']
    assert not detector.last_predictions


def test_negative_scores_cached_within_frame_but_never_across_frames(detector, monkeypatch):
    frame = ball_frame()
    box = (295, 215, 346, 266)
    detector.bias = -1.
    monkeypatch.setattr(detector, '_lab_boxes', lambda *_: [box])
    monkeypatch.setattr(hybrid_detector, 'proposal_boxes', lambda _: [box])
    original_extract = detector.extractor.extract
    calls = []

    def extract(image, candidate):
        calls.append(candidate)
        return original_extract(image, candidate)

    monkeypatch.setattr(detector.extractor, 'extract', extract)
    assert not detector.process(frame, now=0.).candidate_detected
    assert len(calls) == 1
    assert detector.last_stats['score_cache_hits'] == 1
    assert detector.last_stats['classified_count'] == 1
    assert not detector.process(frame, now=.1).candidate_detected
    assert len(calls) == 2
    assert detector.last_stats['score_cache_hits'] == 0


def test_legacy_recovery_can_be_reproduced(detector, monkeypatch):
    detector.recent_hit_recovery_seconds = 0
    detector.reuse_frame_scores = False
    frame = ball_frame()
    detector.process(frame, now=0.)
    monkeypatch.setattr(detector, '_lab_boxes', lambda *_: [])
    monkeypatch.setattr(hybrid_detector, 'proposal_boxes', lambda _: [(295, 215, 346, 266)])
    assert detector.process(frame, now=.1).candidate_detected
    assert not detector.process(frame, now=.2).candidate_detected
    assert not detector.last_stats['urgent_recovery']
