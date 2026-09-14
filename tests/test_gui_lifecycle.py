"""Session and render regressions that do not require a desktop display."""

import threading
from unittest.mock import Mock

import numpy as np

from hcirobot.app import RuntimeEvent
from hcirobot.gui import RobotControlApp
from hcirobot.gui_model import GuiModel, SessionState


def test_failure_cannot_restart_or_reset_until_worker_exits():
    app = RobotControlApp.__new__(RobotControlApp)
    app.model = GuiModel()
    app.model.fail("connection lost")
    app._closing = False
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(5))
    app.session_thread = worker
    worker.start()
    try:
        app._start_session()
        app._reset()
        assert app.session_thread is worker
        assert app.model.session_state is SessionState.FAILED
    finally:
        release.set()
        worker.join(2)
    assert not app._worker_alive()


def test_old_worker_error_and_started_do_not_affect_new_session():
    app = RobotControlApp.__new__(RobotControlApp)
    app.model = GuiModel()
    app.model.begin_start()
    app.model.session_id = 2
    app._handle_event(RuntimeEvent("error", "late cleanup error", session_id=1))
    app._handle_event(RuntimeEvent("started", session_id=1))
    assert app.model.session_id == 2
    assert app.model.session_state is SessionState.STARTING
    assert not app.model.fault


def test_resize_renders_displayed_frame_after_mailbox_was_consumed():
    app = RobotControlApp.__new__(RobotControlApp)
    app.model = GuiModel()
    app.model.begin_start()
    app.model.apply_event(RuntimeEvent("started", session_id=1))
    event = RuntimeEvent("frame", frame=np.zeros((8, 8, 3), np.uint8), session_id=1)
    app.latest_frame = None
    app._displayed_frame = event
    app._render_frame = Mock()
    app._render_latest()
    app._render_frame.assert_called_once_with(event)


def test_clear_frame_view_releases_displayed_and_pending_images():
    app = RobotControlApp.__new__(RobotControlApp)
    app._frame_lock = threading.Lock()
    app.video_label = Mock()
    app.latest_frame = app._displayed_frame = RuntimeEvent("frame")
    app._photo = object()
    app._clear_frame_view()
    assert app.latest_frame is None
    assert app._displayed_frame is None
    assert app._photo is None
