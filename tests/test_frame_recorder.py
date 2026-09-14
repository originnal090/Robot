from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from hcirobot.app import RuntimeEvent, SessionControl, run_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.frame_recorder import FrameRecorder
from hcirobot.robot import RecordingRobot
from hcirobot.video import SyntheticBallSource, SyntheticConfig


def wait_until(condition, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def frame_with(value: int) -> np.ndarray:
    array = np.zeros((48, 64, 3), dtype=np.uint8)
    array[:, :, 2] = value  # distinct BGR blue channel per frame
    return array


def test_recorder_saves_all_offered_frames_in_order(tmp_path: Path) -> None:
    recorder = FrameRecorder(root=tmp_path)
    directory = recorder.start_session()
    assert directory.is_dir()
    for value in range(1, 6):
        recorder.offer(frame_with(value))
    saved, dropped, path = recorder.stop_session()
    assert (saved, dropped) == (5, 0)
    files = sorted(Path(path).glob("frame-*.png"))
    assert [f.name for f in files] == [f"frame-{index:05d}.png" for index in range(1, 6)]
    import cv2

    for file, value in zip(files, range(1, 6), strict=True):
        image = cv2.imread(str(file))
        assert image is not None and int(image[0, 0, 2]) == value


def test_recorder_offer_never_blocks_and_counts_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_imwrite(_path, _image):
        time.sleep(0.02)
        return True

    monkeypatch.setattr("hcirobot.frame_recorder.cv2.imwrite", slow_imwrite)
    recorder = FrameRecorder(queue_size=2, root=tmp_path)
    recorder.start_session()
    for value in range(20):
        recorder.offer(frame_with(value))  # must return immediately even when full
    saved, dropped, _path = recorder.stop_session()
    assert saved + dropped == 20
    assert dropped > 0  # the tiny queue could not absorb 20 frames at 50 Hz writes


def test_recorder_survives_write_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hcirobot.frame_recorder.cv2.imwrite", lambda _p, _i: False)
    messages: list[str] = []
    recorder = FrameRecorder(root=tmp_path, on_log=messages.append)
    recorder.start_session()
    for value in range(30):
        recorder.offer(frame_with(value))
    saved, _dropped, _path = recorder.stop_session()
    assert saved == 0
    assert any("写入连续失败" in message for message in messages)
    assert recorder.snapshot()[0] is False  # cleanly stopped, not hung


def test_recorder_second_session_writes_to_new_directory(tmp_path: Path) -> None:
    recorder = FrameRecorder(root=tmp_path / "caps")
    first = recorder.start_session()
    recorder.offer(frame_with(1))
    recorder.stop_session()
    second = recorder.start_session()
    recorder.offer(frame_with(2))
    saved, _dropped, _path = recorder.stop_session()
    assert first != second
    assert saved == 1  # counters reset between sessions
    assert len(list(second.glob("frame-*.png"))) == 1


def test_run_loop_stops_recorder_when_session_ends(tmp_path: Path) -> None:
    recorder = FrameRecorder(root=tmp_path)
    session = SessionControl()
    robot = RecordingRobot()

    def sink(event: RuntimeEvent) -> None:
        if event.kind == "frame" and event.frame_count == 1 and not recorder.snapshot()[0]:
            recorder.start_session()  # simulate the operator clicking mid-session
        elif event.kind == "frame" and event.frame_count >= 12:
            session.request_stop()

    result = run_loop(
        SyntheticBallSource(SyntheticConfig(fps=100.0, realtime=True)),
        RedBallDetector(DetectorConfig()),
        VisualApproachController(ControllerConfig()),
        robot,
        armed=False,
        session=session,
        event_sink=sink,
        frame_recorder=recorder,
    )
    assert result.termination == "stop_requested"
    assert not recorder.snapshot()[0]  # capture thread exit auto-stopped recording
    _active, saved, _dropped, path = recorder.snapshot()
    assert saved > 0
    assert len(list(Path(path).glob("frame-*.png"))) == saved


def test_gui_model_logs_capture_events() -> None:
    from hcirobot.gui_model import GuiModel

    model = GuiModel()
    model.apply_event(RuntimeEvent("capture", "样本采集结束：10 帧"))
    assert any("样本采集" in message for message in model.logs)
