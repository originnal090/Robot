"""Preserve portable model/profile versions; never replace differing existing files.

Run --prepare-data before training hard-negative-v1. Then run without arguments
after training to publish the version inventory. No robot/network operations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from hcirobot.detector import DetectorConfig

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'artifacts/edge-versions-20260914'
DATA = ROOT / 'data/red-ball-20260914'


def preserve(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise FileExistsError(f'version differs; choose a new version path: {path}')
        return
    with path.open('xb') as stream:
        stream.write(content)


def save_json(path: Path, value) -> None:
    preserve(path, (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode('utf-8'))


def prepare_data() -> None:
    rows = json.loads((DATA / 'annotations.json').read_text(encoding='utf-8'))
    additions = [{'capture': 'capture-20260914-091540', 'frame': f'frame-{n:05}.png',
                      'box': None, 'split': 'train',
                      'note': 'Manually inspected background-only frame; mined development hard negative'}
                 for n in (1487, 1582, 1635)]
    save_json(DATA / 'annotations-hn-v1.json', rows + additions)
    diagnostics = [(1470, [240, 455, 335, 480]), (1471, [230, 457, 310, 480]),
                   (1474, [175, 457, 265, 480]), (1488, None),
                   (1538, [204, 149, 505, 480]), (1544, [209, 36, 556, 404]),
                   (1560, [0, 0, 200, 194]), (1622, None), (1661, None)]
    save_json(DATA / 'annotations-optimization-eval.json', rows + [
        {'capture': 'capture-20260914-091540', 'frame': f'frame-{n:05}.png', 'box': box,
             'split': 'diagnostic', 'note': 'Approximate visible extent; selected errors, not independent test'}
        for n, box in diagnostics])
    print('Prepared 3 additional background training frames and 9 diagnostic labels.')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-data', action='store_true')
    args = parser.parse_args()
    if args.prepare_data:
        prepare_data()
        return
    models = {
        'svm-v1': ROOT / 'artifacts/edge-model-20260914/red_ball_svm.npz',
        'feature-v2': ROOT / 'artifacts/edge-experiments-20260914/feature-v2/red_ball_svm.npz',
        'hn-v1': DEST / 'training-hn-v1/red_ball_svm.npz',
    }
    for name, source in models.items():
        preserve(DEST / 'models' / f'{name}.npz', source.read_bytes())
        preserve(DEST / 'training-reports' / f'{name}.json',
                 source.with_name('report.json').read_bytes())
    base = {'recent_hit_recovery_seconds': .3, 'reuse_frame_scores': True,
                'fast_backend': False, 'expanded_geometry': False}
    definitions = [
        ('svm-v1', 'svm-v1', 'svm', {}),
        ('svm-feature-v2', 'feature-v2', 'svm', {}),
        ('hybrid-v1-legacy', 'svm-v1', 'hybrid', dict(base, recent_hit_recovery_seconds=0,
                                                   reuse_frame_scores=False)),
        ('hybrid-feature-v2', 'feature-v2', 'hybrid', dict(base, recent_hit_recovery_seconds=0,
                                                        reuse_frame_scores=False)),
        ('hybrid-v1-recovery', 'svm-v1', 'hybrid', base),
        ('hybrid-v1-fast', 'svm-v1', 'hybrid', dict(base, fast_backend=True)),
        ('hybrid-hn-v1-fast', 'hn-v1', 'hybrid', dict(base, fast_backend=True)),
        ('hybrid-v1-geometry', 'svm-v1', 'hybrid', dict(base, fast_backend=True,
                                                    expanded_geometry=True)),
        ('hybrid-hn-v1-geometry', 'hn-v1', 'hybrid', dict(base, fast_backend=True,
                                                     expanded_geometry=True)),
    ]
    profiles = []
    for name, model, strategy, options in definitions:
        model_bytes = (DEST / 'models' / f'{model}.npz').read_bytes()
        profile = {'schema_version': 1, 'id': name, 'model': f'../models/{model}.npz',
                       'model_sha256': hashlib.sha256(model_bytes).hexdigest(),
                       'model_bytes': len(model_bytes), 'strategy': strategy,
                       'options': options, 'detector_config': asdict(DetectorConfig())}
        save_json(DEST / 'profiles' / f'{name}.json', profile)
        profiles.append({'id': name, 'profile': f'profiles/{name}.json',
                             'model_sha256': profile['model_sha256']})
    save_json(DEST / 'manifest.json', {'schema_version': 1, 'profiles': profiles,
                                         'note': 'Experimental versions; field performance unmeasured.'})
    # Keep source and annotations with the weights for reproduction after code changes.
    sources = ['src/hcirobot/edge_detector.py', 'src/hcirobot/hybrid_detector.py',
               'src/hcirobot/fast_features.py', 'src/hcirobot/detector_profile.py',
               'src/hcirobot/detector.py', 'src/hcirobot/model.py',
               'tools/train_edge_detector.py', 'tools/prepare_edge_versions.py',
               'tools/compare_edge_experiments.py', 'pyproject.toml', 'uv.lock']
    sources += [str(p.relative_to(ROOT)).replace('\\', '/') for p in DATA.glob('*.json')]
    hashes = {}
    for source in sources:
        content = (ROOT / source).read_bytes()
        preserve(DEST / 'source' / source, content)
        hashes[source] = hashlib.sha256(content).hexdigest()
    save_json(DEST / 'source-sha256.json', hashes)
    print(f'Preserved {len(models)} weights and {len(profiles)} detector profiles in {DEST}')


if __name__ == '__main__':
    main()
