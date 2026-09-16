from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PYTHON_DIR = Path(__file__).resolve().parents[1] / "python"
SPEC = importlib.util.spec_from_file_location(
    "iphone_hotspot_clients", PYTHON_DIR / "hotspot_clients.py"
)
assert SPEC and SPEC.loader
hotspot_clients = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = hotspot_clients
SPEC.loader.exec_module(hotspot_clients)


IFCONFIG = """\
bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 172.20.10.1 netmask 0xfffffff0 broadcast 172.20.10.15
\tstatus: active
\tdesc: com.apple.MobileInternetSharing
"""

ARP_LONG = """\
Neighbor                Linklayer Address Expire(O) Expire(I)          Netif Refs Prbs
172.20.10.3             c0:35:32:4b:82:2d 2m58s     2m58s     bridge10    1
172.20.10.4             22:23:da:7d:ab:9d expired   expired   bridge10    1
"""

LEASES = """\
{
    name=Nekotail-PC
    ip_address=172.20.10.3
    hw_address=1,c0:35:32:4b:82:2d
    identifier=1,c0:35:32:4b:82:2d
    lease=0x6aab3e52
}
{
    name=orangepiaipro-20t
    ip_address=172.20.10.2
    hw_address=1,28:f5:2b:a7:bf:cc
    identifier=1,28:f5:2b:a7:bf:cc
    lease=0x6aab3682
}
{
    name=old-device
    ip_address=172.20.10.5
    hw_address=1,dc:c7:93:f9:82:6c
    lease=0x659c13da
}
{
    name=declined-device
    ip_address=172.20.10.6
    hw_address=1,f0:d5:bf:c0:9c:f7
    lease=0x6aab3e52
    declined=1,f0:d5:bf:c0:9c:f7
}
"""


def test_snapshot_reports_only_reachable_clients_by_default():
    snapshot = hotspot_clients.make_snapshot(
        "bridge100", IFCONFIG, ARP_LONG, LEASES, now=1789521501
    )

    assert snapshot["active"] is True
    assert snapshot["address"] == "172.20.10.1"
    assert snapshot["subnet"] == "172.20.10.0/28"
    assert snapshot["client_count"] == 1
    assert [client["ip"] for client in snapshot["clients"]] == ["172.20.10.3"]
    assert snapshot["clients"][0]["name"] == "Nekotail-PC"
    assert snapshot["clients"][0]["arp_expire_in"] == "2m58s"


def test_all_leases_adds_unexpired_lease_without_calling_it_online():
    snapshot = hotspot_clients.make_snapshot(
        "bridge100",
        IFCONFIG,
        ARP_LONG,
        LEASES,
        now=1789521501,
        include_all_leases=True,
    )

    assert [client["ip"] for client in snapshot["clients"]] == [
        "172.20.10.2",
        "172.20.10.3",
    ]
    assert snapshot["clients"][0]["online"] is False
    assert snapshot["clients"][1]["online"] is True
    assert snapshot["client_count"] == 1


def test_regular_arp_output_is_supported_as_a_fallback():
    parsed = hotspot_clients.parse_arp(
        "? (172.20.10.3) at c0:35:32:4b:82:2d on bridge100 ifscope [bridge]\n"
    )

    assert parsed == [
        {
            "ip": "172.20.10.3",
            "mac": "c0:35:32:4b:82:2d",
            "arp_expire_out": None,
            "arp_expire_in": None,
        }
    ]


def test_inactive_interface_is_not_mistaken_for_a_hotspot():
    snapshot = hotspot_clients.make_snapshot(
        "bridge100", "", "", LEASES, now=1789521501
    )

    assert snapshot["active"] is False
    assert snapshot["client_count"] == 0
