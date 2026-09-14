"""Audit split leakage, preserved profiles and reviewed background regressions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from hcirobot.detector import DetectorConfig
from hcirobot.detector_profile import load_detector

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/edge-route-20260914'


def image_hash(row):
    return hashlib.sha256((ROOT / 'artifacts/captures' / row['capture'] / row['frame'])
                          .read_bytes()).hexdigest()


def main():
    cv2.setNumThreads(1)
    rows = json.loads((ROOT / 'data/red-ball-route-20260914/annotations-balanced.json').read_text())
    train_hashes = {image_hash(row) for row in rows if row['split'] == 'train'}
    test_hashes = {image_hash(row) for row in rows if row['split'] == 'test'}
    assert not train_hashes & test_hashes
    reference = json.loads((ROOT / 'artifacts/edge-versions-20260914/profiles/hybrid-v1-fast.json')
                           .read_text())
    audit = {'cross_split_exact_duplicates': 0, 'profiles': {}, 'background': {}}
    paths = {'v1-fast': ROOT / 'artifacts/edge-versions-20260914/profiles/hybrid-v1-fast.json',
             'route-v1': OUT / 'route-v1/profile.json',
             'route-aug-v1': OUT / 'route-aug-v1/profile.json',
             'route-balanced-v1': OUT / 'route-balanced-v1/profile.json'}
    for name, path in paths.items():
        profile = json.loads(path.read_text())
        assert profile['options'] == reference['options']
        assert profile['detector_config'] == reference['detector_config']
        detector = load_detector(path, DetectorConfig())
        assert detector.feature_version == 1
        audit['profiles'][name] = {'sha256': profile['model_sha256'],
                                   'bytes': profile['model_bytes'], 'strategy_unchanged': True}
    diagnostic = json.loads((OUT / 'background-diagnostics.json').read_text())
    old_capture = 'capture-20260914-091540'
    balanced_records = json.loads((OUT / 'comparison-balanced' / f'{old_capture}.json').read_text())
    first_records = json.loads((OUT / 'comparison' / f'{old_capture}.json').read_text())
    for name, path in paths.items():
        results = []
        record = (first_records if name == 'route-v1' else balanced_records)[name]
        for row in diagnostic:
            frame = cv2.imread(str(ROOT / 'artifacts/captures' / row['capture'] / row['frame']))
            prediction = load_detector(path, DetectorConfig()).predict_boxes(frame, now=0.)
            sequential = record[int(row['frame'][6:11]) - 1]
            results.append(dict(row, duplicates_training=image_hash(row) in train_hashes,
                                fresh_box=prediction[0][0] if prediction else None,
                                sequential_box=sequential['box'], control=sequential['control']))
        audit['background'][name] = {
            'reviewed_frames': len(results),
            'fresh_false_positives': sum(row['fresh_box'] is not None for row in results),
            'sequential_false_positives': sum(row['sequential_box'] is not None for row in results),
            'control_false_positives': sum(row['control'] for row in results),
            'training_duplicate_frames': sum(row['duplicates_training'] for row in results),
            'rows': results,
        }
    (OUT / 'diagnostics').mkdir(exist_ok=True)
    selected = {'213247': [1, 215, 242, 350, 377], '213403': [41, 58, 61, 65, 68, 75],
                '091540': [1567, 1587, 1624, 1625]}
    lookup = {(r['capture'], r['frame']): r for r in rows}
    for suffix, numbers in selected.items():
        capture = f'capture-20260914-{suffix}'
        records = json.loads((OUT / 'comparison-balanced' / f'{capture}.json').read_text())
        strips = []
        for number in numbers:
            frame_name = f'frame-{number:05}.png'
            image = cv2.imread(str(ROOT / 'artifacts/captures' / capture / frame_name))
            tiles = []
            for name in ('v1-fast', 'route-aug-v1', 'route-balanced-v1'):
                row = records[name][number - 1]
                tile = image.copy()
                truth = lookup.get((capture, frame_name), {}).get('box')
                if truth:
                    cv2.rectangle(tile, tuple(truth[:2]), tuple(truth[2:]), (255, 255, 255), 1)
                box = row['box']
                if box:
                    cv2.rectangle(tile, tuple(box[:2]), tuple(box[2:]), (0, 255, 0), 2)
                tile = cv2.resize(tile, (320, 240))
                cv2.rectangle(tile, (0, 0), (320, 24), (0, 0, 0), -1)
                cv2.putText(tile, f'{number:05} {name} ctrl={int(row["control"])}',
                            (3, 17), cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1)
                tiles.append(tile)
            strips.append(np.hstack(tiles))
        cv2.imwrite(str(OUT / 'diagnostics' / f'{capture}.jpg'), np.vstack(strips))
    with (OUT / 'audit.json').open('x', encoding='utf-8') as stream:
        json.dump(audit, stream, indent=2)
    print(json.dumps({name: {k: v for k, v in result.items() if k != 'rows'}
                      for name, result in audit['background'].items()}, indent=2))


if __name__ == '__main__':
    main()
