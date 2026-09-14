"""Train separate v1 models from reviewed route captures; no navigation changes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from train_edge_detector import evaluate, fit_model, load_samples, training_data

from hcirobot.detector import DetectorConfig
from hcirobot.edge_detector import EdgeBallDetector, FeatureExtractor
from hcirobot.hybrid_detector import HybridBallDetector

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/edge-route-20260914'
ANNOTATIONS = ROOT / 'data/red-ball-route-20260914/annotations-combined.json'


def augmentation_data(samples):
    """Positive crops only; full-image context and actual image-edge flags retained.

    Integer image translation simulates a visible bottom-clipped region. This
    augments pixels/features only; it is not a camera or robot motion command.
    """
    extractor = FeatureExtractor(1)
    features, provenance = [], []
    for row in samples:
        if row['split'] != 'train' or not row['capture'].endswith(('213202', '213345')):
            continue
        if row['box'] is None:
            continue
        image = row['image']
        x1, y1, x2, y2 = row['box']
        for fraction in (.25, .5, .8):
            visible = max(4, round((y2 - y1) * fraction))
            shift = image.shape[0] - visible - y1
            shifted = cv2.warpAffine(image, np.float32([[1, 0, 0], [0, 1, shift]]),
                                     (image.shape[1], image.shape[0]),
                                     flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_REFLECT_101)
            box = (x1, image.shape[0] - visible, x2, image.shape[0])
            features.append(extractor.extract(shifted, box))
            provenance.append({'capture': row['capture'], 'frame': row['frame'],
                               'transform': 'bottom_clip', 'fraction': fraction, 'box': box})
        for axis in ('horizontal', 'vertical'):
            kernel = np.zeros((5, 5), np.float32)
            if axis == 'horizontal':
                kernel[2, :] = .2
            else:
                kernel[:, 2] = .2
            blurred = cv2.filter2D(image, -1, kernel)
            features.append(extractor.extract(blurred, row['box']))
            provenance.append({'capture': row['capture'], 'frame': row['frame'],
                               'transform': f'blur_{axis}', 'kernel': 5, 'box': row['box']})
    return np.stack(features), np.ones(len(features), np.int32), provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--balanced-only', action='store_true',
                        help='train a third version with reviewed old-development false positives')
    args = parser.parse_args()
    cv2.setNumThreads(1)
    annotations = ANNOTATIONS.with_name('annotations-balanced.json') if args.balanced_only else ANNOTATIONS
    samples = load_samples(annotations, ROOT / 'artifacts/captures')
    x, y, counts = training_data(samples, feature_version=1)
    lab_negative_count = 0
    if args.balanced_only:
        detector = HybridBallDetector(ROOT / 'artifacts/edge-model-20260914/red_ball_svm.npz',
                                      DetectorConfig())
        lab_negatives = []
        for row in samples:
            if (row['split'] == 'train' and row['box'] is None
                    and row['capture'].endswith('091540') and row['frame'] in
                    ('frame-01567.png', 'frame-01587.png', 'frame-01624.png')):
                lab_negatives.extend(detector.extractor.extract(row['image'], box)
                                     for box in detector._lab_boxes(row['image'], None))
        lab_negative_count = len(lab_negatives)
        if lab_negatives:
            x = np.concatenate((x, np.stack(lab_negatives)))
            y = np.concatenate((y, -np.ones(lab_negative_count, np.int32)))
    extra_x, extra_y, provenance = augmentation_data(samples)
    base_profile = json.loads((ROOT / 'artifacts/edge-versions-20260914/profiles/hybrid-v1-fast.json')
                              .read_text(encoding='utf-8'))
    results = {}
    versions = (('route-balanced-v1', True),) if args.balanced_only else (
        ('route-v1', False), ('route-aug-v1', True))
    for name, augmented in versions:
        directory = OUT / name
        directory.mkdir(exist_ok=False)
        model_path = directory / 'red_ball_svm.npz'
        train_x = np.concatenate((x, extra_x)) if augmented else x
        train_y = np.concatenate((y, extra_y)) if augmented else y
        fitting = fit_model(train_x, train_y, model_path, feature_version=1)
        detector = EdgeBallDetector(model_path, DetectorConfig())
        report = {
            'id': name, 'annotations': str(annotations.relative_to(ROOT)),
            'annotations_sha256': hashlib.sha256(annotations.read_bytes()).hexdigest(),
            'training': fitting, 'original_crop_counts': counts,
            'added_lab_negative_crops': lab_negative_count,
            'added_positive_augmentation_crops': len(extra_x) if augmented else 0,
            'augmentation_provenance': provenance if augmented else [],
            'split_policy': 'New train: 213202/213345. New holdout: 213247/213403. '
                            'Old train/test unchanged. Exact duplicates across splits rejected. '
                            'First two models specified before evaluation; third uses reviewed '
                            'old-development background errors, not new holdout labels. '
                            'Fixed C=1 and threshold=0.',
            'limitations': ['Same autonomous run and room, not an independent trial.',
                            'Sparse, color-assisted, visually reviewed approximate boxes.',
                            'PC latency only; feature v1, detector and navigation unchanged.'],
            'train': evaluate(detector, samples, 'train'),
            'test': evaluate(detector, samples, 'test'),
        }
        with (directory / 'report.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
        profile = dict(base_profile, id=f'hybrid-{name}-fast', model='red_ball_svm.npz',
                       model_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
                       model_bytes=model_path.stat().st_size)
        with (directory / 'profile.json').open('x', encoding='utf-8') as stream:
            json.dump(profile, stream, indent=2)
        results[name] = {'training': fitting, 'test': report['test']['metrics']}
        print(json.dumps({name: results[name]}, indent=2), flush=True)
    summary_name = 'training-balanced-summary.json' if args.balanced_only else 'training-summary.json'
    with (OUT / summary_name).open('x', encoding='utf-8') as stream:
        json.dump(results, stream, indent=2)


if __name__ == '__main__':
    main()
