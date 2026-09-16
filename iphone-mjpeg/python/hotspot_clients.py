# ruff: noqa: UP045
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Optional

# This file also runs on the phone's Python 3.9, so keep Optional rather than
# adopting the repository's Python 3.11-only union spelling.

DEFAULT_INTERFACE = "bridge100"
DEFAULT_LEASE_FILE = Path("/var/db/dhcpd_leases")
ARP_CANDIDATES = (
    "/var/jb/usr/sbin/arp",
    "/usr/sbin/arp",
    "/usr/bin/arp",
    "/sbin/arp",
)
IFCONFIG_CANDIDATES = (
    "/var/jb/usr/sbin/ifconfig",
    "/sbin/ifconfig",
    "/usr/sbin/ifconfig",
)
MAC_RE = re.compile(r"^(?:[0-9a-f]{1,2}:){5}[0-9a-f]{1,2}$", re.IGNORECASE)
STANDARD_ARP_RE = re.compile(
    r"\((?P<ip>\d+(?:\.\d+){3})\)\s+at\s+"
    r"(?P<mac>(?:[0-9a-f]{1,2}:){5}[0-9a-f]{1,2}|\(incomplete\))",
    re.IGNORECASE,
)


def normalize_mac(value: str) -> str:
    value = value.strip().lower()
    if "," in value:
        value = value.split(",", 1)[1]
    if not MAC_RE.fullmatch(value):
        return ""
    return ":".join(f"{int(part, 16):02x}" for part in value.split(":"))


def parse_lease_timestamp(value: str) -> Optional[int]:
    try:
        return int(value.strip(), 0)
    except (TypeError, ValueError):
        return None


def parse_leases(text: str, now: Optional[int] = None) -> list[dict[str, Any]]:
    """Parse Apple's bootpd lease file and return live, non-declined leases."""
    if now is None:
        now = int(time.time())
    leases: list[dict[str, Any]] = []
    for block in re.findall(r"\{(.*?)\}", text, flags=re.DOTALL):
        values: dict[str, str] = {}
        for line in block.splitlines():
            if "=" not in line:
                continue
            key, value = line.strip().split("=", 1)
            values[key.strip()] = value.strip()
        if "declined" in values:
            continue
        ip = values.get("ip_address", "")
        try:
            ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            continue
        expires_at = parse_lease_timestamp(values.get("lease", ""))
        if expires_at is not None and expires_at <= now:
            continue
        leases.append(
            {
                "ip": ip,
                "mac": normalize_mac(values.get("hw_address", "")),
                "name": values.get("name", ""),
                "lease_expires_at": expires_at,
            }
        )
    # Prefer the newest lease when a file contains duplicates for one address.
    by_ip: dict[str, dict[str, Any]] = {}
    for lease in leases:
        previous = by_ip.get(lease["ip"])
        if previous is None or (lease["lease_expires_at"] or 0) > (
            previous["lease_expires_at"] or 0
        ):
            by_ip[lease["ip"]] = lease
    return list(by_ip.values())


def _active_reachability(value: str) -> bool:
    return value.lower() not in {"expired", "(none)", "none", "-"}


def parse_arp(text: str) -> list[dict[str, Any]]:
    """Parse Darwin's extended (-l) or regular ARP table output."""
    clients: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Neighbor"):
            continue
        standard = STANDARD_ARP_RE.search(line)
        if standard:
            mac = normalize_mac(standard.group("mac"))
            if mac:
                clients[standard.group("ip")] = {
                    "ip": standard.group("ip"),
                    "mac": mac,
                    "arp_expire_out": None,
                    "arp_expire_in": None,
                }
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            ipaddress.IPv4Address(parts[0])
        except ipaddress.AddressValueError:
            continue
        mac = normalize_mac(parts[1])
        if not mac:
            continue
        expire_out, expire_in = parts[2], parts[3]
        # ri_rcv_expire is the useful signal that this client was recently heard.
        if not _active_reachability(expire_in):
            continue
        clients[parts[0]] = {
            "ip": parts[0],
            "mac": mac,
            "arp_expire_out": expire_out,
            "arp_expire_in": expire_in,
        }
    return list(clients.values())


def parse_interface(text: str, interface: str) -> dict[str, Any]:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    flags_match = re.search(r"flags=\w+<([^>]*)>", first_line)
    flags = set(flags_match.group(1).split(",")) if flags_match else set()
    inet_match = re.search(
        r"^\s*inet\s+(?P<ip>\d+(?:\.\d+){3})\s+netmask\s+(?P<mask>\S+)",
        text,
        flags=re.MULTILINE,
    )
    address = inet_match.group("ip") if inet_match else None
    subnet = None
    if inet_match:
        raw_mask = inet_match.group("mask")
        try:
            mask_value = int(raw_mask, 16) if raw_mask.startswith("0x") else int(
                ipaddress.IPv4Address(raw_mask)
            )
            prefix = f"{mask_value:032b}".count("1")
            subnet = str(ipaddress.IPv4Network(f"{address}/{prefix}", strict=False))
        except (ValueError, ipaddress.AddressValueError):
            subnet = None
    return {
        "interface": interface,
        "active": bool(address and "UP" in flags and "RUNNING" in flags),
        "address": address,
        "subnet": subnet,
    }


def _ip_sort_key(value: str) -> int:
    try:
        return int(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError:
        return 2**32


def make_snapshot(
    interface: str,
    ifconfig_output: str,
    arp_output: str,
    leases_text: str,
    *,
    now: Optional[int] = None,
    include_all_leases: bool = False,
    warnings: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    if now is None:
        now = int(time.time())
    interface_info = parse_interface(ifconfig_output, interface)
    leases = parse_leases(leases_text, now)
    lease_by_ip = {lease["ip"]: lease for lease in leases}
    lease_by_mac = {lease["mac"]: lease for lease in leases if lease["mac"]}
    clients: list[dict[str, Any]] = []
    online_keys: set[tuple[str, str]] = set()
    for neighbor in parse_arp(arp_output):
        lease = lease_by_mac.get(neighbor["mac"]) or lease_by_ip.get(neighbor["ip"])
        client = {
            **neighbor,
            "name": lease["name"] if lease else "",
            "lease_expires_at": lease["lease_expires_at"] if lease else None,
            "online": True,
        }
        clients.append(client)
        online_keys.add((client["ip"], client["mac"]))
    if include_all_leases:
        for lease in leases:
            if (lease["ip"], lease["mac"]) in online_keys:
                continue
            clients.append(
                {
                    **lease,
                    "arp_expire_out": None,
                    "arp_expire_in": None,
                    "online": False,
                }
            )
    clients.sort(key=lambda item: _ip_sort_key(item["ip"]))
    return {
        **interface_info,
        "observed_at": dt.datetime.fromtimestamp(now).astimezone().isoformat(),
        "client_count": sum(1 for client in clients if client["online"]),
        "clients": clients,
        "warnings": list(warnings or ()),
    }


def find_command(candidates: Sequence[str], name: str) -> Optional[str]:
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name)


def run_command(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def collect_snapshot(
    interface: str,
    lease_file: Path,
    include_all_leases: bool,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = run_command,
) -> dict[str, Any]:
    warnings: list[str] = []
    ifconfig = find_command(IFCONFIG_CANDIDATES, "ifconfig")
    arp = find_command(ARP_CANDIDATES, "arp")
    if ifconfig is None:
        raise RuntimeError("ifconfig not found; install the Procursus network-cmds package")
    if arp is None:
        raise RuntimeError("arp not found; install the Procursus network-cmds package")

    interface_result = runner((ifconfig, interface))
    if interface_result.returncode == 0:
        ifconfig_output = interface_result.stdout
    else:
        ifconfig_output = ""
        warnings.append(f"interface {interface} is unavailable; Personal Hotspot may be off")

    arp_result = runner((arp, "-anl", "-i", interface))
    if arp_result.returncode == 0:
        arp_output = arp_result.stdout
    else:
        fallback_result = runner((arp, "-an", "-i", interface))
        arp_output = fallback_result.stdout if fallback_result.returncode == 0 else ""
        if fallback_result.returncode != 0 and ifconfig_output:
            warnings.append("could not read the hotspot ARP table")

    try:
        leases_text = lease_file.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        leases_text = ""
        warnings.append(f"could not read {lease_file}: {error}")

    return make_snapshot(
        interface,
        ifconfig_output,
        arp_output,
        leases_text,
        include_all_leases=include_all_leases,
        warnings=warnings,
    )


def format_lease_time(timestamp: Optional[int]) -> str:
    if timestamp is None:
        return "-"
    return dt.datetime.fromtimestamp(timestamp).astimezone().strftime("%m-%d %H:%M")


def print_human(snapshot: dict[str, Any]) -> None:
    if snapshot["active"]:
        print(
            f"Personal Hotspot: active  interface={snapshot['interface']}  "
            f"gateway={snapshot['address']}  subnet={snapshot['subnet'] or '-'}"
        )
    else:
        print(f"Personal Hotspot: inactive or unavailable  interface={snapshot['interface']}")
    print(f"Recently active clients: {snapshot['client_count']}")
    clients = snapshot["clients"]
    if clients:
        print(f"{'STATE':<8} {'IP':<15} {'NAME':<24} {'MAC':<17} {'ARP-IN':<9} LEASE-UNTIL")
        for client in clients:
            state = "ONLINE" if client["online"] else "LEASE"
            name = client["name"] or "-"
            if len(name) > 24:
                name = name[:21] + "..."
            print(
                f"{state:<8} {client['ip']:<15} {name:<24} {client['mac'] or '-':<17} "
                f"{client['arp_expire_in'] or '-':<9} {format_lease_time(client['lease_expires_at'])}"
            )
    for warning in snapshot["warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Show clients recently active on an iOS Personal Hotspot by combining "
            "the bridge ARP table with bootpd DHCP leases."
        )
    )
    parser.add_argument("--interface", default=DEFAULT_INTERFACE)
    parser.add_argument("--lease-file", type=Path, default=DEFAULT_LEASE_FILE)
    parser.add_argument(
        "--all-leases",
        action="store_true",
        help="also show unexpired DHCP leases that are not in the ARP reachability table",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help="refresh continuously (minimum 0.5 seconds)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.watch is not None and args.watch < 0.5:
        raise SystemExit("--watch must be at least 0.5 seconds")
    first = True
    try:
        while True:
            snapshot = collect_snapshot(args.interface, args.lease_file, args.all_leases)
            if args.json:
                print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
            else:
                if args.watch is not None and not first:
                    print("\x1b[2J\x1b[H", end="")
                print_human(snapshot)
            sys.stdout.flush()
            first = False
            if args.watch is None:
                return 0
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"hotspot-clients: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
