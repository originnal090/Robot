from __future__ import annotations

import json
import socket

from hcirobot.app import run_loop
from hcirobot.controller import ControllerConfig, VisualApproachController
from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.robot import RecordingRobot
from hcirobot.unity_udp import UnityStatusConfig, UnityStatusPublisher
from hcirobot.video import SyntheticBallSource, SyntheticConfig


class StepClock:
    def __init__(self, step: float = 0.1) -> None:
        self.value = -step
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def test_run_loop_streams_schema_v1_over_real_udp_socket() -> None:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(1.0)
    publisher = UnityStatusPublisher(
        UnityStatusConfig(
            enabled=True, host="127.0.0.1", port=receiver.getsockname()[1], frame_rate_hz=1000.0
        )
    )
    try:
        result = run_loop(
            # Realtime pacing: with latest-wins frame dropping, an unpaced
            # synthetic source exhausts itself before 3 frames get processed.
            SyntheticBallSource(SyntheticConfig(fps=100.0, realtime=True)),
            RedBallDetector(DetectorConfig()),
            VisualApproachController(ControllerConfig()),
            RecordingRobot(),
            armed=True,
            max_frames=3,
            clock=StepClock(),
            event_sink=publisher,
        )
        messages: list[dict] = []
        while True:
            try:
                payload, _ = receiver.recvfrom(65535)
            except TimeoutError:
                break
            message = json.loads(payload)
            messages.append(message)
            if message["event"]["kind"] == "finished":
                break
    finally:
        publisher.close()
        receiver.close()

    assert result.termination == "max_frames"
    assert messages[0]["event"]["kind"] == "started"
    assert messages[-1]["event"]["kind"] == "finished"
    assert messages[-1]["termination"] == "max_frames"
    assert messages[-1]["output"] == {
        "v": 0.0,
        "steer": 0.0,
        "grab": False,
        "source": "shutdown",
    }
    assert messages[-1]["armed"] is False
    assert [message["seq"] for message in messages] == list(range(1, len(messages) + 1))
    assert len({message["session_id"] for message in messages}) == 1
