from __future__ import annotations

import importlib.util
import socket
from pathlib import Path
from typing import Self

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "discover_devices.py"
SPEC = importlib.util.spec_from_file_location("discover_devices", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

ROLES = {"TonyPi": (8080, 22), "OrangePi": (8090, 22)}


def test_parse_ports_dedupes_and_sorts() -> None:
    assert MODULE.parse_ports("8080, 8090,8080") == (8080, 8090)
    assert MODULE.parse_ports("22") == (22,)


def test_parse_ports_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        MODULE.parse_ports("")
    with pytest.raises(ValueError):
        MODULE.parse_ports("0,70000")
    with pytest.raises(ValueError):
        MODULE.parse_ports("http")


def test_cidr_helpers() -> None:
    assert MODULE.cidr_from_ip("192.168.137.187") == "192.168.137.0/24"
    assert MODULE.cidr_hosts("192.168.137.0/30") == ["192.168.137.1", "192.168.137.2"]


def test_pick_candidate_prefers_hotspot_subnet() -> None:
    addresses = ["10.208.76.139", "192.168.137.1", "192.168.56.1"]
    assert MODULE.pick_candidate(addresses) == "192.168.137.1"
    assert MODULE.pick_candidate(["10.0.0.5", "192.168.56.1"]) == "192.168.56.1"
    assert MODULE.pick_candidate(["10.0.0.5"]) is None


def test_classify_full_and_partial() -> None:
    # 22 is shared by both roles, so a full match on one role always
    # partially matches the other.
    assert MODULE.classify({8080, 22}, ROLES) == (["TonyPi"], ["OrangePi"])
    assert MODULE.classify({8080}, ROLES) == ([], ["TonyPi"])
    assert MODULE.classify({8090, 22}, ROLES) == (["OrangePi"], ["TonyPi"])
    assert MODULE.classify({8090}, ROLES) == ([], ["OrangePi"])
    assert MODULE.classify({8080, 8090, 22}, ROLES) == (["TonyPi", "OrangePi"], [])
    assert MODULE.classify(set(), ROLES) == ([], [])


def test_parse_windows_arp_normalizes_mac() -> None:
    text = """
接口: 192.168.137.1 --- 0x9
  192.168.137.187          28-f5-2b-a7-bf-cc     动态
  192.168.137.10           B8-27-EB-AA-BB-CC     动态
  224.0.0.22               01-00-5e-00-00-16     静态
"""
    table = MODULE.parse_windows_arp(text)
    assert table["192.168.137.187"] == "28:f5:2b:a7:bf:cc"
    assert table["192.168.137.10"] == "b8:27:eb:aa:bb:cc"
    assert "224.0.0.22" in table


def test_parse_linux_arp_skips_incomplete_entries() -> None:
    text = """IP address       HW type     Flags       HW address            Mask     Device
192.168.137.10   0x1         0x2         b8:27:eb:aa:bb:cc     *        wlan0
192.168.137.200  0x1         0x0         00:00:00:00:00:00     *        wlan0
"""
    table = MODULE.parse_linux_arp(text)
    assert table == {"192.168.137.10": "b8:27:eb:aa:bb:cc"}


def test_scan_finds_open_loopback_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        result = MODULE.scan("127.0.0.0/30", (port,), 0.2, workers=8)
    assert result == {"127.0.0.1": {port}}


def test_extra_ports_may_not_shadow_roles() -> None:
    with pytest.raises(SystemExit):
        MODULE.main(["--extra-ports", "8080"])


def test_banner_helper_reports_server_header() -> None:
    payload = b"HTTP/1.0 200 OK\r\nServer: MJPG-Streamer/0.2\r\n\r\n"

    class _FakeSocket:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def settimeout(self, value: float) -> None:
            pass

        def connect(self, address: tuple[str, int]) -> None:
            pass

        def sendall(self, data: bytes) -> None:
            pass

        def recv(self, size: int) -> bytes:
            return payload

    original = MODULE.socket.socket
    MODULE.socket.socket = lambda *args, **kwargs: _FakeSocket()  # type: ignore[assignment]
    try:
        banner = MODULE.http_server_banner("192.0.2.1", 8080)
    finally:
        MODULE.socket.socket = original
    assert banner == "MJPG-Streamer/0.2"
