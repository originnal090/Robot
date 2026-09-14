"""Compare preserved weights with the identical LAB/SVM/ROI strategy; no robot IO."""
from __future__ import annotations

import argparse
import json
import platform
import time
from pathlib import Path

import cv2
import numpy as np
from compare_edge_experiments import aggregate, metrics

from hcirobot.detector import DetectorConfig
from hcirobot.detector_profile import load_detector
from hcirobot.edge_detector import box_iou

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/edge-route-20260914'


def annotate(row, label):
    return dict(row, capture=label['capture'], split=label['split'], truth=label['box'],
                iou=box_iou(row['box'], label['box']) if row['box'] and label['box'] else 0.)


def summarize(rows):
    return {f'{era}_{split}': metrics([r for r in rows if r['split'] == split
                                     and ('-213' in r['capture']) == (era == 'new')])
            for era in ('old', 'new') for split in ('train', 'test')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--balanced-study', action='store_true')
    args = parser.parse_args()
    cv2.setNumThreads(1)
    destination = OUT / ('comparison-balanced' if args.balanced_study else 'comparison')
    destination.mkdir(exist_ok=False)
    profiles = {
        'v1-fast': ROOT / 'artifacts/edge-versions-20260914/profiles/hybrid-v1-fast.json',
        'hn-fast': ROOT / 'artifacts/edge-versions-20260914/profiles/hybrid-hn-v1-fast.json',
        'route-v1': OUT / 'route-v1/profile.json',
        'route-aug-v1': OUT / 'route-aug-v1/profile.json',
    }
    if args.balanced_study:
        profiles.pop('hn-fast')
        profiles.pop('route-v1')
        profiles['route-balanced-v1'] = OUT / 'route-balanced-v1/profile.json'
    labels = json.loads((ROOT / 'data/red-ball-route-20260914/annotations-combined.json')
                        .read_text(encoding='utf-8'))
    lookup = {(r['capture'], r['frame']): r for r in labels}
    report = {'platform': platform.platform(), 'opencv': cv2.__version__, 'numpy': np.__version__,
              'threads': cv2.getNumThreads(), 'synthetic_capture_fps': 10,
              'profiles': {name: json.loads(path.read_text(encoding='utf-8'))
                           for name, path in profiles.items()},
              'notes': ['All methods use the same frozen LAB/SVM/ROI detection strategy.',
                        'Each capture starts with fresh tracking; no timestamps recorded, assume 10 Hz.',
                        'Rotating serial inference order; decode excluded; PC only.',
                        'Unlabeled candidate/control counts are not accuracy.',
                        'Holdout captures are parts of the same trip; no cross-trip claim.'],
              'captures': {}}
    sequential = {name: [] for name in profiles}
    fresh = {name: [] for name in profiles}
    cfg = DetectorConfig()
    for capture in sorted({r['capture'] for r in labels}):
        files = sorted((ROOT / 'artifacts/captures' / capture).glob('*.png'))
        detectors = {name: load_detector(path, cfg) for name, path in profiles.items()}
        first = cv2.imread(str(files[0]))
        for detector in detectors.values():
            for step in range(5):
                detector.process(first, now=(step - 5) / 10)
            detector.update_config(detector.config)
        records = {name: [] for name in profiles}
        names = list(detectors)
        for index, path in enumerate(files):
            frame = cv2.imread(str(path))
            if frame is None or frame.shape != (480, 640, 3):
                raise ValueError(f'expected readable 640x480 BGR capture: {path}')
            order = names[index % len(names):] + names[:index % len(names)]
            for name in order:
                detector = detectors[name]
                before = time.perf_counter()
                result = detector.process(frame, now=index / 10)
                elapsed = (time.perf_counter() - before) * 1000
                predictions = detector.last_predictions
                row = {'frame': path.name, 'ms': elapsed,
                       'candidate': bool(result.candidate_detected),
                       'control': bool(result.has_current_target),
                       'center': [result.center_x, result.center_y] if result.center_x is not None else None,
                       'radius': result.radius, 'score': result.score,
                       'box': predictions[0][0] if predictions else None,
                       'stats': dict(detector.last_stats)}
                records[name].append(row)
                label = lookup.get((capture, path.name))
                if label is not None:
                    sequential[name].append(annotate(row, label))
            if (index + 1) % 250 == 0:
                print(f'{capture}: {index + 1}/{len(files)}', flush=True)
        with (destination / f'{capture}.json').open('x', encoding='utf-8') as stream:
            json.dump(records, stream, indent=2)
        report['captures'][capture] = {name: aggregate(rows) for name, rows in records.items()}
        print(capture, {name: value['candidate_frames']
                        for name, value in report['captures'][capture].items()}, flush=True)
    for label in labels:
        frame = cv2.imread(str(ROOT / 'artifacts/captures' / label['capture'] / label['frame']))
        for name, path in profiles.items():
            predictions = load_detector(path, cfg).predict_boxes(frame, now=0.)
            row = {'frame': label['frame'], 'box': predictions[0][0] if predictions else None}
            fresh[name].append(annotate(row, label))
    report['sequential'] = {name: summarize(rows) for name, rows in sequential.items()}
    report['fresh'] = {name: summarize(rows) for name, rows in fresh.items()}
    with (destination / 'annotated.json').open('x', encoding='utf-8') as stream:
        json.dump({'sequential': sequential, 'fresh': fresh}, stream, indent=2)
    with (destination / 'summary.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report['sequential'], indent=2))


if __name__ == '__main__':
    main()
