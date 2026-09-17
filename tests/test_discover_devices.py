from __future__ import annotations

import socket

import pytest

from hcirobot import device_discovery as discovery


def test_helpers_and_roles() -> None:
    assert discovery.parse_ports("8080,8090,8080") == (8080, 8090)
    assert discovery.cidr_from_ip("192.168.137.187") == "192.168.137.0/24"
    assert discovery.pick_candidate(["10.0.0.1", "192.168.137.1"]) == "192.168.137.1"
    assert discovery.classify({22, 8080}) == (["TonyPi"], ["OrangePi"])
    with pytest.raises(ValueError):
        discovery.parse_ports("")


def test_scan_finds_open_loopback_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        result = discovery.scan("127.0.0.0/30", (port,), 0.2, 8)
    assert result == {"127.0.0.1": {port}}


def test_arp_parsers_normalize_and_skip_zero_mac() -> None:
    windows = "  192.168.137.10  B8-27-EB-AA-BB-CC  dynamic"
    linux = "192.168.137.10 0x1 0x2 b8:27:eb:aa:bb:cc * wlan0\n192.168.137.20 0x1 0x0 00:00:00:00:00:00 * wlan0"
    assert discovery.parse_windows_arp(windows)["192.168.137.10"] == "b8:27:eb:aa:bb:cc"
    assert discovery.parse_linux_arp(linux) == {"192.168.137.10": "b8:27:eb:aa:bb:cc"}


def test_discovery_never_probes_control_port(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(discovery, "autodetect_cidr", lambda: ("127.0.0.0/30", "test"))
    monkeypatch.setattr(discovery, "scan", lambda cidr, ports, timeout, workers: seen.append(ports) or {})
    monkeypatch.setattr(discovery, "arp_table", dict)
    discovery.discover_devices()
    assert seen == [(22, 8080, 8090)]
    assert 5075 not in seen[0]
