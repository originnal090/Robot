"""Create an immutable, portable field-test bundle of code, weights and reports."""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / 'artifacts/edge-versions-20260914'


def main():
    target = VERSIONS / 'field-test-bundle.zip'
    if target.exists():
        raise FileExistsError(f'archive already exists: {target}')
    files = list((ROOT / 'src/hcirobot').rglob('*.py'))
    files += list((ROOT / 'data/red-ball-20260914').glob('*.json'))
    files += [p for p in VERSIONS.rglob('*') if p.is_file()]
    files += [ROOT / name for name in (
        'pyproject.toml', 'uv.lock', 'README.md', 'config.toml',
        'tools/train_edge_detector.py', 'tools/prepare_edge_versions.py',
        'tools/compare_edge_experiments.py', 'tools/benchmark_edge_detector.py',
        'tools/audit_edge_versions.py', 'tools/package_edge_versions.py',
        'docs/edge-versions-20260914.md',
        'tests/test_detector_versions.py', 'tests/test_hybrid_detector.py',
        'tests/test_edge_detector.py', 'tests/test_gui_smoke.py', 'tests/test_cli.py',
    )]
    hashes = {}
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(set(files)):
            relative = path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            bundle.writestr(relative, content)
            hashes[relative] = hashlib.sha256(content).hexdigest()
        bundle.writestr('BUNDLE-SHA256.json', json.dumps(hashes, indent=2))
    with zipfile.ZipFile(target) as bundle:
        assert bundle.testzip() is None
        assert len(bundle.namelist()) == len(hashes) + 1
        for relative, digest in hashes.items():
            assert hashlib.sha256(bundle.read(relative)).hexdigest() == digest
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    with target.with_suffix('.sha256').open('x', encoding='ascii') as stream:
        stream.write(f'{digest}  {target.name}\n')
    print(json.dumps({'archive': str(target), 'bytes': target.stat().st_size,
                      'files': len(hashes), 'sha256': digest, 'integrity_verified': True}, indent=2))


if __name__ == '__main__':
    main()
