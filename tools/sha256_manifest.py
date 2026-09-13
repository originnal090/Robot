#!/usr/bin/env python3
"""Create or verify SHA-256 manifests for an offline deployment directory."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

_MANIFEST = "SHA256SUMS"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*") if path.is_file() and path.name != _MANIFEST
    )


def create(root: Path) -> int:
    entries = [f"{digest(path)}  {path.relative_to(root).as_posix()}" for path in files(root)]
    (root / _MANIFEST).write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")
    print(f"wrote {len(entries)} entries to {root / _MANIFEST}")
    return 0


def verify(root: Path) -> int:
    manifest = root / _MANIFEST
    failures = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        expected, separator, relative = line.partition("  ")
        if not separator or not relative:
            print(f"invalid manifest line: {line}", file=sys.stderr)
            failures += 1
            continue
        path = root / Path(relative)
        actual = digest(path) if path.is_file() else "missing"
        if actual != expected:
            print(f"FAILED {relative}: expected {expected}, got {actual}", file=sys.stderr)
            failures += 1
        else:
            print(f"OK {relative}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if not args.directory.is_dir():
        parser.error(f"not a directory: {args.directory}")
    return verify(args.directory) if args.verify else create(args.directory)


if __name__ == "__main__":
    raise SystemExit(main())
