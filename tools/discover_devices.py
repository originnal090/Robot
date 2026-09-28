#!/usr/bin/env python3
"""Scan the hotspot LAN for HCIRobot boards (no mDNS needed).

DHCP on the Windows hotspot reassigns addresses, so before a session this
port-probes the local /24 and classifies boards by their open ports:

  TonyPi    = TCP 8080 (mjpg-streamer) + 22 (sshd).
  Orange Pi = TCP 8090 + 22 (sshd).

Port 5075 is deliberately absent: the robot server is single-client, so a
raw scanner connect would kick the live control session. 6001 is UDP and
not probed either.

Stdlib only -- the same script runs on the Windows PC, the Orange Pi and,
if ever needed, TonyPi's Python 3.7.

Usage:
  python tools/discover_devices.py
  python tools/discover_devices.py --cidr 192.168.137.0/24 --json
  python tools/discover_devices.py --extra-ports 5075   # report-only ports
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import platform
import re
import socket
import subprocess
import sys
import time

DEFAULT_CIDR = "192.168.137.0/24"
HOTSPOT_PREFIX = "192.168.137."  # Windows ICS/mobile-hotspot subnet is fixed
ROLE_TONYPI = "TonyPi"
ROLE_ORANGEPI = "OrangePi"
CONNECT_TIMEOUT_SECONDS = 0.5
SCAN_WORKERS = 128
BANNER_TIMEOUT_SECONDS = 0.8
BANNER_RECV_BYTES = 512

_MAC_PATTERN = r"[0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5}"
_WINDOWS_ARP_LINE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+(" + _MAC_PATTERN + r")\b")
_LINUX_ARP_LINE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+\S+\s+\S+\s+(" + _MAC_PATTERN + r")\s")
_ZERO_MAC = "00:00:00:00:00:00"


def parse_ports(text: str) -> tuple[int, ...]:
    """Parse "8080, 8090" into a sorted tuple of valid port numbers."""
    ports = tuple(sorted({int(piece) for piece in text.split(",") if piece.strip()}))
    if not ports or any(port < 1 or port > 65535 for port in ports):
        raise ValueError(f"invalid port list: {text!r}")
    return ports


def cidr_from_ip(ip: str, prefix: int = 24) -> str:
    network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    return f"{network.network_address}/{prefix}"


def autodetect_cidr(default: str = DEFAULT_CIDR) -> tuple[str, str]:
    """Return (cidr, source).

    The default route often points elsewhere (campus LAN, VPN), so enumerate
    every local IPv4 and prefer the hotspot subnet, then any 192.168.x iface,
    then the default-route subnet.
    """
    candidate = pick_candidate(local_ipv4s())
    if candidate:
        label = f"hotspot interface {candidate}" if candidate.startswith(HOTSPOT_PREFIX) else f"local ip {candidate}"
        return cidr_from_ip(candidate), label
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
    except OSError:
        return default, "fallback"
    if ip.startswith(("127.", "169.254.")):
        return default, "fallback"
    return cidr_from_ip(ip), f"default route {ip}"


def local_ipv4s() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    addresses: list[str] = []
    seen: set[str] = set()
    for info in infos:
        ip = info[4][0]
        if ip not in seen and not ip.startswith(("127.", "169.254.")):
            seen.add(ip)
            addresses.append(ip)
    return addresses


def pick_candidate(addresses: list[str]) -> str | None:
    for prefix in (HOTSPOT_PREFIX, "192.168."):
        for ip in addresses:
            if ip.startswith(prefix):
                return ip
    return None


def cidr_hosts(cidr: str) -> list[str]:
    return [str(host) for host in ipaddress.ip_network(cidr, strict=False).hosts()]


def port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, port))
        return True
    except OSError:
        return False


def scan(cidr: str, ports: tuple[int, ...], timeout: float, workers: int) -> dict[str, set[int]]:
    """Probe every host in cidr on every port; return open ports per host."""
    targets = [(ip, port) for ip in cidr_hosts(cidr) for port in ports]
    result: dict[str, set[int]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(port_open, ip, port, timeout): (ip, port) for ip, port in targets}
        for future in concurrent.futures.as_completed(futures):
            ip, port = futures[future]
            if future.result():
                result.setdefault(ip, set()).add(port)
    return result


def http_server_banner(ip: str, port: int, timeout: float = BANNER_TIMEOUT_SECONDS) -> str | None:
    """Best-effort Server header fetch, used to confirm mjpg-streamer on 8080."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, port))
            sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
            data = sock.recv(BANNER_RECV_BYTES)
    except OSError:
        return None
    for line in data.decode("latin-1").split("\r\n"):
        if line.lower().startswith("server:"):
            return line.split(":", 1)[1].strip()
    return None


def classify(open_ports: set[int], roles: dict[str, tuple[int, ...]]) -> tuple[list[str], list[str]]:
    """Return (full matches, partial matches) against {role: required ports}."""
    full: list[str] = []
    partial: list[str] = []
    for role, required in roles.items():
        hits = sum(1 for port in required if port in open_ports)
        if hits == len(required):
            full.append(role)
        elif hits:
            partial.append(role)
    return full, partial


def normalize_mac(mac: str) -> str:
    return mac.replace("-", ":").lower()


def parse_windows_arp(text: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in text.splitlines():
        match = _WINDOWS_ARP_LINE.match(line)
        if match:
            table[match.group(1)] = normalize_mac(match.group(2))
    return table


def parse_linux_arp(text: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINUX_ARP_LINE.match(line)
        if match and normalize_mac(match.group(2)) != _ZERO_MAC:
            table[match.group(1)] = normalize_mac(match.group(2))
    return table


def arp_table() -> dict[str, str]:
    """Best-effort IP->MAC lookup; the TCP probes themselves populate the tables."""
    try:
        if platform.system() == "Windows":
            # Bytes + replace: arp -a emits the console codepage (GBK on zh-CN
            # Windows); IP/MAC columns are ASCII so type-column mojibake is fine.
            output = subprocess.run(
                ["arp", "-a"], capture_output=True, timeout=5, check=False
            ).stdout.decode("utf-8", errors="replace")
            return parse_windows_arp(output)
        with open("/proc/net/arp", encoding="ascii") as handle:
            return parse_linux_arp(handle.read())
    except (OSError, subprocess.SubprocessError):
        return {}


def format_mac(ip: str, table: dict[str, str]) -> str:
    return table.get(ip, "-")


def print_report(
    cidr: str,
    scanned: dict[str, set[int]],
    roles: dict[str, tuple[int, ...]],
    table: dict[str, str],
    banners: dict[str, str | None],
) -> None:
    print(f"cidr={cidr} hosts={len(cidr_hosts(cidr))}")
    print(f"{'IP':<17}{'MAC':<19}{'Role':<22}Open ports")
    for ip, ports in sorted(scanned.items(), key=lambda item: ipaddress.ip_address(item[0])):
        full, partial = classify(ports, roles)
        role = "+".join(full) if full else ""
        if partial:
            missing = [port for role_name in partial for port in roles[role_name] if port not in ports]
            suffix = f" ({'+'.join(partial)}? missing {','.join(str(p) for p in missing)})"
            role = (role + suffix) if role else suffix.strip()
        if 8080 in ports:
            banner = banners.get(ip)
            if banner:
                role += f"  [{banner}]"
        print(f"{ip:<17}{format_mac(ip, table):<19}{role:<22} {', '.join(str(p) for p in sorted(ports))}")
    for role, ports in roles.items():
        matches = [ip for ip, open_ports in scanned.items() if all(p in open_ports for p in ports)]
        for ip in sorted(matches, key=ipaddress.ip_address):
            if role == ROLE_TONYPI:
                print(f"{role}: {ip}  video http://{ip}:8080/?action=stream  ssh pi@{ip}")
            elif role == ROLE_ORANGEPI:
                print(f"{role}: {ip}  ssh HwHiAiUser@{ip}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find TonyPi / Orange Pi addresses on the LAN")
    parser.add_argument("--cidr", default=None, help="subnet to scan (default: autodetect, else 192.168.137.0/24)")
    parser.add_argument("--tonypi-ports", default="8080,22", help="ports that identify TonyPi (default: 8080,22)")
    parser.add_argument("--orangepi-ports", default="8090,22", help="ports that identify the Orange Pi (default: 8090,22)")
    parser.add_argument(
        "--extra-ports",
        default="",
        help="additional ports to report per host, e.g. 5075; report-only, never used for roles",
    )
    parser.add_argument("--timeout", type=float, default=CONNECT_TIMEOUT_SECONDS)
    parser.add_argument("--workers", type=int, default=SCAN_WORKERS)
    parser.add_argument("--no-banner", action="store_true", help="skip the mjpg-streamer Server header check")
    parser.add_argument("--json", dest="as_json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        tonypi_ports = parse_ports(args.tonypi_ports)
        orangepi_ports = parse_ports(args.orangepi_ports)
        extra_ports = parse_ports(args.extra_ports) if args.extra_ports.strip() else ()
        if args.extra_ports.strip():
            banned = set(extra_ports) & set(tonypi_ports) | set(extra_ports) & set(orangepi_ports)
            if banned:
                parser.error(f"--extra-ports may not overlap role ports: {sorted(banned)}")
    except ValueError as exc:
        parser.error(str(exc))

    roles = {ROLE_TONYPI: tonypi_ports, ROLE_ORANGEPI: orangepi_ports}
    probe_ports = tuple(sorted(set(tonypi_ports) | set(orangepi_ports) | set(extra_ports)))

    cidr, source = autodetect_cidr()
    if args.cidr:
        cidr, source = args.cidr, "argument"

    started = time.monotonic()
    scanned = scan(cidr, probe_ports, args.timeout, args.workers)
    elapsed = time.monotonic() - started
    table = arp_table()

    banners: dict[str, str | None] = {}
    if not args.no_banner:
        for ip, ports in scanned.items():
            if 8080 in ports:
                banners[ip] = http_server_banner(ip, 8080)

    if args.as_json:
        devices = []
        for ip in sorted(scanned, key=ipaddress.ip_address):
            full, partial = classify(scanned[ip], roles)
            devices.append(
                {
                    "ip": ip,
                    "mac": table.get(ip),
                    "roles_full": full,
                    "roles_partial": partial,
                    "open_ports": sorted(scanned[ip]),
                    "http_server": banners.get(ip),
                }
            )
        json.dump(
            {"cidr": cidr, "cidr_source": source, "elapsed_seconds": round(elapsed, 2), "devices": devices},
            sys.stdout,
            indent=2,
        )
        print()
        return 0

    print(f"scanned cidr={cidr} ({source}) ports={list(probe_ports)} elapsed={elapsed:.1f}s")
    if not scanned:
        print("no boards found")
        return 1
    print_report(cidr, scanned, roles, table, banners)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
