import runpy
from pathlib import Path

import cv2
import numpy as np


def test_route_augmentation_uses_only_training_pixels_and_preserves_source(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'tools'))
    functions = runpy.run_path(str(Path(__file__).parents[1] / 'tools/train_route_models.py'))
    image = np.full((480, 640, 3), 40, np.uint8)
    cv2.circle(image, (300, 240), 20, (60, 50, 210), -1)
    before = image.copy()
    sample = {'capture': 'capture-20260914-213202', 'frame': 'frame-00001.png',
              'split': 'train', 'image': image, 'box': (280, 220, 321, 261)}
    # Sentinel None images would crash if held-out or old rows were augmented.
    held_out = dict(sample, split='test', image=None)
    old = dict(sample, capture='capture-20260914-091540', image=None)
    x, y, provenance = functions['augmentation_data']([sample, held_out, old])
    assert x.shape == (5, 406)
    assert np.isfinite(x).all()
    assert y.tolist() == [1] * 5
    assert len(provenance) == 5
    assert all(row['capture'] == sample['capture'] for row in provenance)
    assert all(row['box'][3] == 480 for row in provenance[:3])
    assert np.all(x[:3, -2] == 1)  # True image-bottom geometry, not a fake ROI edge.
    np.testing.assert_array_equal(image, before)
