"""Discover HCIRobot boards without touching the single-client control port."""

from __future__ import annotations

import concurrent.futures
import ipaddress
import platform
import re
import socket
import subprocess
import time

DEFAULT_CIDR = "192.168.137.0/24"
HOTSPOT_PREFIX = "192.168.137."
ROLE_TONYPI = "TonyPi"
ROLE_ORANGEPI = "OrangePi"
ROLE_PORTS = {ROLE_TONYPI: (8080, 22), ROLE_ORANGEPI: (8090, 22)}
_MAC = r"[0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5}"
_WINDOWS_ARP = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+(" + _MAC + r")\b")
_LINUX_ARP = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+\S+\s+\S+\s+(" + _MAC + r")\s")


def parse_ports(text: str) -> tuple[int, ...]:
    ports = tuple(sorted({int(piece) for piece in text.split(",") if piece.strip()}))
    if not ports or any(port < 1 or port > 65535 for port in ports):
        raise ValueError(f"invalid port list: {text!r}")
    return ports


def cidr_from_ip(ip: str, prefix: int = 24) -> str:
    network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    return f"{network.network_address}/{prefix}"


def local_ipv4s() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return list(dict.fromkeys(info[4][0] for info in infos if not info[4][0].startswith(("127.", "169.254."))))


def pick_candidate(addresses: list[str]) -> str | None:
    for prefix in (HOTSPOT_PREFIX, "192.168."):
        if candidate := next((ip for ip in addresses if ip.startswith(prefix)), None):
            return candidate
    return None


def autodetect_cidr(default: str = DEFAULT_CIDR) -> tuple[str, str]:
    if candidate := pick_candidate(local_ipv4s()):
        label = "hotspot interface" if candidate.startswith(HOTSPOT_PREFIX) else "local ip"
        return cidr_from_ip(candidate), f"{label} {candidate}"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        if not ip.startswith(("127.", "169.254.")):
            return cidr_from_ip(ip), f"default route {ip}"
    except OSError:
        pass
    return default, "fallback"


def cidr_hosts(cidr: str) -> list[str]:
    network = ipaddress.ip_network(cidr, strict=False)
    if network.num_addresses > 1024:
        raise ValueError("设备发现最多扫描 /22 子网")
    return [str(host) for host in network.hosts()]


def port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, port))
        return True
    except OSError:
        return False


def scan(cidr: str, ports: tuple[int, ...], timeout: float, workers: int) -> dict[str, set[int]]:
    targets = [(ip, port) for ip in cidr_hosts(cidr) for port in ports]
    result: dict[str, set[int]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(port_open, ip, port, timeout): (ip, port) for ip, port in targets}
        for future in concurrent.futures.as_completed(futures):
            ip, port = futures[future]
            if future.result():
                result.setdefault(ip, set()).add(port)
    return result


def http_server_banner(ip: str, port: int, timeout: float = 0.8) -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, port))
            sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
            data = sock.recv(512)
    except OSError:
        return None
    return next((line.split(":", 1)[1].strip() for line in data.decode("latin-1").split("\r\n") if line.lower().startswith("server:")), None)


def classify(open_ports: set[int], roles: dict[str, tuple[int, ...]] = ROLE_PORTS) -> tuple[list[str], list[str]]:
    full, partial = [], []
    for role, required in roles.items():
        hits = sum(port in open_ports for port in required)
        (full if hits == len(required) else partial if hits else []).append(role)
    return full, partial


def normalize_mac(mac: str) -> str:
    return mac.replace("-", ":").lower()


def parse_windows_arp(text: str) -> dict[str, str]:
    return {m.group(1): normalize_mac(m.group(2)) for line in text.splitlines() if (m := _WINDOWS_ARP.match(line))}


def parse_linux_arp(text: str) -> dict[str, str]:
    return {m.group(1): normalize_mac(m.group(2)) for line in text.splitlines() if (m := _LINUX_ARP.match(line)) and normalize_mac(m.group(2)) != "00:00:00:00:00:00"}


def arp_table() -> dict[str, str]:
    try:
        if platform.system() == "Windows":
            output = subprocess.run(["arp", "-a"], capture_output=True, timeout=5, check=False).stdout.decode("utf-8", errors="replace")
            return parse_windows_arp(output)
        with open("/proc/net/arp", encoding="ascii") as handle:
            return parse_linux_arp(handle.read())
    except (OSError, subprocess.SubprocessError):
        return {}


def discover_devices(cidr: str | None = None, *, timeout: float = 0.5, workers: int = 128) -> dict:
    selected, source = (cidr, "argument") if cidr else autodetect_cidr()
    started = time.monotonic()
    scanned = scan(selected, (22, 8080, 8090), timeout, workers)
    macs = arp_table()
    devices = []
    for ip in sorted(scanned, key=ipaddress.ip_address):
        full, partial = classify(scanned[ip])
        devices.append({"ip": ip, "mac": macs.get(ip), "roles_full": full, "roles_partial": partial,
                        "open_ports": sorted(scanned[ip]), "http_server": http_server_banner(ip, 8080) if 8080 in scanned[ip] else None})
    return {"cidr": selected, "cidr_source": source, "elapsed_seconds": round(time.monotonic() - started, 2), "devices": devices}
