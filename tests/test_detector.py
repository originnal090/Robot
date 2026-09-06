from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from hcirobot.detector import DetectorConfig, RedBallDetector

FIXTURES = Path(__file__).parent / "fixtures"

# Real TonyPi frames captured from the MJPEG stream (640x480), with the ball center
# and radius measured under the shipped DetectorConfig defaults.
REAL_FRAME_CASES = [
    ("frame-0050.png", 242.14, 247.00, 17.12),  # close scene, ball on stacked boxes
    ("frame-0140.png", 307.43, 248.00, 17.99),  # small, motion-blurred ball
    ("frame-0180.png", 263.76, 247.32, 17.82),  # scene after the robot moved back
]


def ball_frame(x: int = 320, radius: int = 40) -> np.ndarray:
    frame = np.full((480, 640, 3), 32, dtype=np.uint8)
    lab = np.array([[[120, 170, 135]]], dtype=np.uint8)
    color = tuple(int(value) for value in cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)[0, 0])
    cv2.circle(frame, (x, 240), radius, color, -1, lineType=cv2.LINE_8)
    return frame


def test_lab_test_color_round_trips_inside_course_threshold() -> None:
    lab = np.array([[[120, 170, 135]]], dtype=np.uint8)
    bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    round_trip = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)[0, 0]
    config = DetectorConfig()
    assert np.all(round_trip >= np.array(config.lab_min))
    assert np.all(round_trip <= np.array(config.lab_max))


def test_detector_requires_three_consecutive_hits_and_releases_after_three_misses() -> None:
    detector = RedBallDetector(DetectorConfig())
    results = [detector.process(ball_frame()) for _ in range(3)]
    assert [result.detected for result in results] == [False, False, True]
    assert results[-1].center_x == pytest.approx(320, abs=2)
    assert results[-1].radius == pytest.approx(40, abs=3)

    empty = np.full((480, 640, 3), 32, dtype=np.uint8)
    misses = [detector.process(empty) for _ in range(3)]
    assert [result.detected for result in misses] == [True, True, False]
    assert all(not result.candidate_detected for result in misses)


def test_new_candidate_after_loss_must_be_confirmed_again() -> None:
    detector = RedBallDetector(DetectorConfig())
    for _ in range(3):
        assert detector.process(ball_frame()).candidate_detected
    empty = np.full((480, 640, 3), 32, dtype=np.uint8)
    detector.process(empty)
    detector.process(empty)

    new_candidate = detector.process(ball_frame(x=500))
    assert new_candidate.detected
    assert not new_candidate.control_confirmed
    assert not new_candidate.has_current_target


def test_detector_rejects_tiny_candidate() -> None:
    detector = RedBallDetector(DetectorConfig(confirmation_frames=1))
    result = detector.process(ball_frame(radius=3))
    assert not result.candidate_detected
    assert not result.detected


def test_detector_rejects_blob_without_strong_red_core() -> None:
    # A blob that passes the shape filters but contains no strongly red pixels
    # (core A >= 160) must be rejected as background texture.
    frame = np.full((480, 640, 3), 32, dtype=np.uint8)
    lab = np.array([[[120, 150, 135]]], dtype=np.uint8)  # inside lab bounds, below core_a_min
    color = tuple(int(value) for value in cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)[0, 0])
    cv2.circle(frame, (320, 240), 40, color, -1, lineType=cv2.LINE_8)

    detector = RedBallDetector(DetectorConfig(confirmation_frames=1))
    result = detector.process(frame)
    assert not result.candidate_detected

    relaxed = RedBallDetector(DetectorConfig(confirmation_frames=1, minimum_core_fraction=0.0))
    assert relaxed.process(frame).candidate_detected


def test_core_a_min_above_lab_a_max_is_rejected() -> None:
    with pytest.raises(ValueError, match="core A minimum"):
        DetectorConfig(core_a_min=250)


def test_real_robot_frames_detect_the_ball_stably() -> None:
    detector = RedBallDetector(DetectorConfig())
    for name, center_x, center_y, radius in REAL_FRAME_CASES:
        frame = cv2.imread(str(FIXTURES / name))
        assert frame is not None, name
        detections = [detector.process(frame) for _ in range(3)]
        assert all(item.candidate_detected for item in detections), name
        final = detections[-1]
        assert final.detected and final.has_current_target, name
        assert final.center_x == pytest.approx(center_x, abs=3)
        assert final.center_y == pytest.approx(center_y, abs=3)
        assert final.radius == pytest.approx(radius, abs=3)
        assert final.circularity >= DetectorConfig().minimum_circularity


def test_real_background_without_ball_yields_no_candidate() -> None:
    frame = cv2.imread(str(FIXTURES / "frame-0180.png"))
    assert frame is not None
    crops = [
        frame[0:200, 320:640],  # right foam wall
        frame[0:480, 0:180],  # left wooden wall
        frame[300:480, 0:640],  # wooden floor
    ]
    for index, crop in enumerate(crops):
        detector = RedBallDetector(DetectorConfig())
        scaled = cv2.resize(crop, (640, 480), interpolation=cv2.INTER_AREA)
        results = [detector.process(scaled) for _ in range(4)]
        assert all(not result.candidate_detected for result in results), f"crop {index}"


def test_update_config_resets_streaks_and_applies_new_parameters() -> None:
    detector = RedBallDetector(DetectorConfig())
    detector.process(ball_frame())
    detector.process(ball_frame())

    # An unreachable core threshold takes effect immediately on the next frame.
    detector.update_config(replace(DetectorConfig(), core_a_min=DetectorConfig().lab_max[1]))
    assert not detector.process(ball_frame()).candidate_detected

    # Temporal state was reset: confirmation_frames=1 confirms on the first hit.
    detector.update_config(DetectorConfig(confirmation_frames=1))
    relaxed = detector.process(ball_frame())
    assert relaxed.candidate_detected and relaxed.detected

    # Back to defaults the streak restarts from zero.
    detector.update_config(DetectorConfig())
    results = [detector.process(ball_frame()) for _ in range(3)]
    assert [result.detected for result in results] == [False, False, True]


def test_update_config_is_thread_safe_under_concurrent_processing() -> None:
    detector = RedBallDetector(DetectorConfig())
    stop = threading.Event()

    def churn() -> None:
        while not stop.is_set():
            detector.update_config(DetectorConfig())
            detector.update_config(DetectorConfig(confirmation_frames=2))

    worker = threading.Thread(target=churn)
    worker.start()
    try:
        for _ in range(40):
            assert detector.process(ball_frame()).candidate_detected
    finally:
        stop.set()
        worker.join()
    assert detector.config.confirmation_frames in (2, 3)
