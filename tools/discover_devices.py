#!/usr/bin/env python3
"""CLI for LAN board discovery. Port 5075 is intentionally never probed."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hcirobot.device_discovery import *


def print_report(report: dict) -> None:
    print(f"scanned {report['cidr']} ({report['cidr_source']}) in {report['elapsed_seconds']:.1f}s")
    print(f"{'IP':<17}{'MAC':<19}{'Role':<22}Open ports")
    for device in report["devices"]:
        roles = "+".join(device["roles_full"]) or "unknown"
        if device["roles_partial"]:
            roles += f" ({'+'.join(device['roles_partial'])}?)"
        print(f"{device['ip']:<17}{(device['mac'] or '-'):<19}{roles:<22} {', '.join(map(str, device['open_ports']))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find TonyPi / Orange Pi addresses")
    parser.add_argument("--cidr", help=f"subnet to scan (fallback: {DEFAULT_CIDR})")
    parser.add_argument("--tonypi-ports", default="8080,22")
    parser.add_argument("--orangepi-ports", default="8090,22")
    parser.add_argument("--extra-ports", default="", help="report-only ports; avoid 5075 during live control")
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument("--workers", type=int, default=128)
    parser.add_argument("--no-banner", action="store_true")
    parser.add_argument("--json", dest="as_json", action="store_true")
    args = parser.parse_args(argv)
    try:
        roles = {
            ROLE_TONYPI: parse_ports(args.tonypi_ports),
            ROLE_ORANGEPI: parse_ports(args.orangepi_ports),
        }
        extra_ports = parse_ports(args.extra_ports) if args.extra_ports.strip() else ()
        overlap = set(extra_ports) & {port for required in roles.values() for port in required}
        if overlap:
            parser.error(f"--extra-ports may not overlap role ports: {sorted(overlap)}")
    except ValueError as exc:
        parser.error(str(exc))
    report = discover_devices(
        args.cidr,
        timeout=args.timeout,
        workers=args.workers,
        roles=roles,
        extra_ports=extra_ports,
        include_banner=not args.no_banner,
    )
    if args.as_json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print_report(report)
    return 0 if report["devices"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
