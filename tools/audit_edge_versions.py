"""Audit an existing version replay and render selected diagnostic differences."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from compare_edge_experiments import make_preview

from hcirobot.detector import DetectorConfig
from hcirobot.detector_profile import load_detector

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/edge-versions-20260914'


def main():
    audit = {'captures': {}, 'versions': {}}
    for path in sorted((OUT / 'profiles').glob('*.json')):
        detector = load_detector(path, DetectorConfig())
        audit['versions'][path.stem] = {'load_ok': True, 'feature_version': detector.feature_version}
    for capture in ('capture-20260914-091540', 'capture-20260914-093059'):
        rows = json.loads((OUT / 'comparison' / f'{capture}.json').read_text(encoding='utf-8'))
        reference, fast = rows['hybrid-v1-recovery'], rows['hybrid-v1-fast']
        fields = ('frame', 'box', 'candidate', 'control', 'center', 'radius', 'stats')
        differences = [a['frame'] for a, b in zip(reference, fast, strict=True)
                       if any(a[field] != b[field] for field in fields)]
        score_error = max((abs(a['score'] - b['score']) for a, b in zip(reference, fast, strict=True)
                           if a['score'] is not None and b['score'] is not None), default=0.)
        comparisons = {}
        for method in ('hybrid-hn-v1-fast', 'hybrid-v1-geometry', 'hybrid-hn-v1-geometry'):
            comparisons[method] = [{
                'frame': a['frame'], 'before_box': a['box'], 'after_box': b['box'],
                'before_control': a['control'], 'after_control': b['control'],
            } for a, b in zip(fast, rows[method], strict=True)
                if a['box'] != b['box'] or a['control'] != b['control']]
        wide = [i for i, row in enumerate(reference) if row['stats']['wide_search']]
        audit['captures'][capture] = {
            'frames': len(reference), 'fast_semantic_differences': differences,
            'fast_max_top_score_error': score_error,
            'wide_frame_mean_ms': {name: float(np.mean([rows[name][i]['ms'] for i in wide]))
                                   for name in ('hybrid-v1-recovery', 'hybrid-v1-fast')} if wide else {},
            'changes': comparisons,
        }
        assert not differences and score_error < 1e-5, 'fast backend changed replay decisions'
        paths = sorted((ROOT / 'artifacts/captures' / capture).glob('frame-*.png'))
        selected = [1471, 1474, 1538, 1544, 1560, 1582] if '091540' in capture else [102, 116, 117, 190]
        preview_dir = OUT / 'diagnostics'
        preview_dir.mkdir(exist_ok=True)
        make_preview(preview_dir, capture, paths,
                     {name: rows[name] for name in ('hybrid-v1-fast', 'hybrid-hn-v1-fast',
                                                   'hybrid-v1-geometry')}, selected=selected)
    # Weight archives must still match the original files, even after all experiments.
    originals = {'svm-v1': ROOT / 'artifacts/edge-model-20260914/red_ball_svm.npz',
                 'feature-v2': ROOT / 'artifacts/edge-experiments-20260914/feature-v2/red_ball_svm.npz'}
    audit['original_models'] = {}
    for name, path in originals.items():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert path.read_bytes() == (OUT / 'models' / f'{name}.npz').read_bytes()
        audit['original_models'][name] = {'sha256': digest, 'archive_identical': True}
    target = OUT / 'audit.json'
    with target.open('x', encoding='utf-8') as stream:
        json.dump(audit, stream, indent=2)
    compact = {name: {k: v for k, v in value.items() if k != 'changes'}
               for name, value in audit['captures'].items()}
    print(json.dumps(compact, indent=2))
    labels = json.loads((OUT / 'comparison/annotated.json').read_text(encoding='utf-8'))
    for mode in ('sequential', 'fresh'):
        print(mode)
        for name, rows in labels[mode].items():
            print(name, [(row['frame'], row['box'], round(row['iou'], 3))
                         for row in rows if row['split'] == 'diagnostic'])


if __name__ == '__main__':
    cv2.setNumThreads(1)
    main()
