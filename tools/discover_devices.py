#!/usr/bin/env python3
"""CLI for LAN board discovery. Port 5075 is intentionally never probed."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hcirobot.device_discovery import discover_devices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find TonyPi / Orange Pi addresses")
    parser.add_argument("--cidr")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = discover_devices(args.cidr)
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"scanned {report['cidr']} ({report['cidr_source']}) in {report['elapsed_seconds']:.1f}s")
        for device in report["devices"]:
            roles = ",".join(device["roles_full"]) or "unknown"
            print(f"{device['ip']:<16} {roles:<10} ports={device['open_ports']}")
    return 0 if report["devices"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
