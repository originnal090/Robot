from dataclasses import replace
from types import SimpleNamespace

from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.gui import RobotControlApp, _default_tuning
from hcirobot.gui_model import SessionState


def test_gui_tuning_preserves_motion_strategy_and_pulse_parameters():
    initial = ControllerConfig(
        approach_mode="normal", slow_radius_ratio=0.06,
        turn_seconds_near=0.20, turn_seconds_far=0.35,
        near_lateral_enabled=True, arrival_action="",
    )
    values = _default_tuning()
    values["align_enter_error"] = "0.07"
    logs = []
    app = SimpleNamespace(
        model=SimpleNamespace(session_state=SessionState.RUNNING, append_log=logs.append),
        active_controller=VisualApproachController(initial),
        active_detector=RedBallDetector(DetectorConfig()),
        _tuning_int=lambda key: int(values[key]),
        _tuning_float=lambda key: float(values[key]),
        _refresh_view=lambda: None,
    )
    RobotControlApp._apply_params(app)
    assert app.active_controller.config == replace(initial, align_enter_error=0.07)
    assert logs == ["检测/控制参数已热更新"]
