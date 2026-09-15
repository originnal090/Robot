# 2026-09-15 capture development annotations

`annotations-new.json` contains 22 visually reviewed development rows from captures
`194703` and `195031`: 18 visible-ball boxes and four no-ball frames. The boxes were
initialized from the isolated LAB component and checked against the source image.

All rows are training/development data. They were inspected while selecting the A-channel
threshold and must not be reported as an independent holdout. The prior 65-row test split in
`data/red-ball-route-20260914/annotations-balanced.json` remains excluded from fitting, and the
training loader rejects exact image hashes crossing that split.

Frames 195031/58-61 contain no visible ball. Frames 195031/189-190 are identical, severely
motion-blurred visible-ball images; only frame 189 is included so duplicate content is not
needlessly repeated during fitting.

The source PNGs remain under `artifacts/captures/` and are intentionally ignored by Git.
Reproduce the lightweight v2 model with:

```sh
python tools/train_capture_update.py
```
