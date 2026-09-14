# Capture development annotations

`annotations.json` contains 52 assistant visual annotations from the two user-provided
640×480 capture sequences, dated 2026-09-14. Boxes were manually estimated from contact
sheets and are approximate visible extents, not detector-generated labels. Coordinates
are `[x1,y1,x2,y2]`, exclusive upper endpoints; `null` means no visible ball.

29 training/development frames: 15 positive, 14 negative, all from 091540.
23 held-out frames: 13 positive from 093059 and 10 negative from a separate temporal
block of 091540. The long stationary tail of 093059 is deliberately thinned. No exact
image hashes cross the split. These are two captures of the same environment, so this
does not establish performance in new rooms or lighting.

091540/frame-01457.png is excluded: only a roughly 14-pixel-high cap of the ball is
visible at the bottom. It must not be treated as a negative training example.

Original images remain under `artifacts/captures/` and are not checked into Git.
Run `tools/train_edge_detector.py --annotations data/red-ball-20260914/annotations.json`
to reproduce the lightweight model. See `docs/edge-recognition.md` for evaluation limits.
