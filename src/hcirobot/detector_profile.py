"""Portable, checksummed model + strategy versions for GUI/CLI field comparisons."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .detector import DetectorConfig
from .edge_detector import EdgeBallDetector
from .hybrid_detector import HybridBallDetector


def load_detector(path: str | Path, config: DetectorConfig) -> EdgeBallDetector:
    path = Path(path)
    if path.suffix.lower() != '.json':
        return EdgeBallDetector(path, config)
    profile = json.loads(path.read_text(encoding='utf-8'))
    if profile.get('schema_version') != 1:
        raise ValueError('unsupported detector profile schema')
    model_path = path.parent / profile['model']
    if hashlib.sha256(model_path.read_bytes()).hexdigest() != profile['model_sha256']:
        raise ValueError(f'model checksum mismatch for {profile["id"]}')
    # Profiles freeze the full detector config. Controller/video settings remain
    # session settings. Explicit live tuning still works through update_config.
    parameters = dict(profile['detector_config'])
    for key in ('lab_min', 'lab_max'):
        parameters[key] = tuple(parameters[key])
    frozen_config = DetectorConfig(**parameters)
    strategy = profile['strategy']
    if strategy == 'svm':
        detector = EdgeBallDetector(model_path, frozen_config)
    elif strategy == 'hybrid':
        detector = HybridBallDetector(model_path, frozen_config, **profile['options'])
    else:
        raise ValueError(f'unsupported detector strategy: {strategy}')
    detector.profile_id = profile['id']
    return detector
