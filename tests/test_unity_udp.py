from __future__ import annotations

import json

from hcirobot.app import RuntimeEvent
from hcirobot.unity_udp import UnityStatusConfig, UnityStatusPublisher, fanout_event_sinks


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeSocket:
    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.close_count = 0

    def sendto(self, payload: bytes, address: tuple[str, int]) -> int:
        if self.error is not None:
            raise self.error
        self.sent.append((payload, address))
        return len(payload)

    def close(self) -> None:
        self.close_count += 1


def event(kind: str = "frame", **kwargs) -> RuntimeEvent:
    values = {"frame_count": 1, "output_source": "autonomy", "armed": True}
    values.update(kwargs)
    return RuntimeEvent(kind, **values)


def test_disabled_publisher_is_a_noop() -> None:
    sock = FakeSocket()
    publisher = UnityStatusPublisher(UnityStatusConfig(enabled=False), sock=sock)
    assert publisher.publish(event()) is False
    assert sock.sent == []
    assert publisher.seq == 0


def test_regular_frames_are_throttled_but_state_change_is_forced() -> None:
    clock = Clock()
    sock = FakeSocket()
    publisher = UnityStatusPublisher(
        UnityStatusConfig(enabled=True, frame_rate_hz=10.0),
        sock=sock,
        monotonic=clock,
    )
    assert publisher.publish(event()) is True
    clock.value = 0.02
    assert publisher.publish(event(frame_count=2)) is False
    assert publisher.seq == 1

    assert publisher.publish(event(frame_count=2, obstacle_state="CAUTION")) is True
    clock.value = 0.11
    assert publisher.publish(event(frame_count=3, obstacle_state="CAUTION")) is False
    clock.value = 0.120001
    assert publisher.publish(event(frame_count=4, obstacle_state="CAUTION")) is True

    messages = [json.loads(payload) for payload, _ in sock.sent]
    assert [message["seq"] for message in messages] == [1, 2, 3]
    assert all(address == ("127.0.0.1", 6102) for _, address in sock.sent)


def test_non_frame_events_bypass_frame_throttle() -> None:
    clock = Clock()
    sock = FakeSocket()
    publisher = UnityStatusPublisher(UnityStatusConfig(enabled=True), sock=sock, monotonic=clock)
    assert publisher.publish(event()) is True
    assert publisher.publish(event("state")) is True
    assert publisher.publish(event("error", message="boom")) is True
    assert publisher.publish(event("finished", message="runtime_error")) is True
    assert len(sock.sent) == 4


def test_oversize_datagrams_are_dropped_without_advancing_sequence() -> None:
    sock = FakeSocket()
    publisher = UnityStatusPublisher(
        UnityStatusConfig(enabled=True, maximum_datagram_bytes=256),
        sock=sock,
    )
    assert publisher.publish(event(message="x" * 1000)) is False
    assert publisher.seq == 0
    assert publisher.send_errors == 1
    assert sock.sent == []


def test_udp_errors_never_escape_or_advance_sequence() -> None:
    sock = FakeSocket(OSError("network unavailable"))
    publisher = UnityStatusPublisher(UnityStatusConfig(enabled=True), sock=sock)
    assert publisher.publish(event()) is False
    assert publisher.publish(event("error", message="still running")) is False
    assert publisher.seq == 0
    assert publisher.send_errors == 2


def test_close_is_idempotent_and_disables_future_sends() -> None:
    sock = FakeSocket()
    publisher = UnityStatusPublisher(UnityStatusConfig(enabled=True), sock=sock)
    publisher.close()
    publisher.close()
    assert sock.close_count == 1
    assert publisher.publish(event()) is False


def test_fanout_isolates_observer_failures() -> None:
    received: list[str] = []

    def broken(_event: RuntimeEvent) -> None:
        raise RuntimeError("observer failed")

    sink = fanout_event_sinks(broken, lambda current: received.append(current.kind))
    assert sink is not None
    sink(RuntimeEvent("frame"))
    assert received == ["frame"]
