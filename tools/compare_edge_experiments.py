"""Replay LAB, SVM v1/v2 and hybrid experiments with identical CPU settings.

All frames are evaluated. Hybrid scheduling uses synthetic capture time at the
requested FPS, never replay execution speed. Only annotated frames have accuracy
metrics; unannotated candidate counts are not accuracy. No robot connection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from collections import Counter
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path

import cv2
import numpy as np

from hcirobot.detector import DetectorConfig, RedBallDetector
from hcirobot.detector_profile import load_detector
from hcirobot.edge_detector import EdgeBallDetector, box_iou
from hcirobot.hybrid_detector import HybridBallDetector


class RecordedEdgeDetector(EdgeBallDetector):
    def predict_boxes(self, frame):
        self.last_predictions = super().predict_boxes(frame)
        return self.last_predictions


def metrics(rows):
    positives = sum(row["truth"] is not None for row in rows)
    predicted = sum(row["box"] is not None for row in rows)
    tp = sum(row["truth"] is not None and row["box"] is not None
             and row["iou"] >= .5 for row in rows)
    return {
        "frames": len(rows), "positive_frames": positives,
        "negative_frames": len(rows) - positives,
        "tp": tp, "fp": predicted - tp, "fn": positives - tp,
        "negative_frame_false_positives": sum(
            row["truth"] is None and row["box"] is not None for row in rows),
        "precision": tp / predicted if predicted else None,
        "recall": tp / positives if positives else None,
        "mean_iou_positive": float(np.mean(
            [row["iou"] for row in rows if row["truth"] is not None])) if positives else None,
    }


def aggregate(records):
    elapsed = [row["ms"] for row in records]
    distances = [float(np.linalg.norm(np.subtract(b["center"], a["center"])))
                 for a, b in pairwise(records)
                 if a["center"] is not None and b["center"] is not None]
    runs, streak = [], 0
    for row in records:
        if row["candidate"]:
            streak += 1
        elif streak:
            runs.append(streak)
            streak = 0
    if streak:
        runs.append(streak)
    return {
        "frames": len(records),
        "candidate_frames": sum(row["candidate"] for row in records),
        "control_frames": sum(row["control"] for row in records),
        "candidate_runs": len(runs), "longest_candidate_run": max(runs, default=0),
        "latency_ms": {"mean": float(np.mean(elapsed)),
                       "median": float(np.median(elapsed)),
                       "p95": float(np.percentile(elapsed, 95))},
        "mean_center_step_px": float(np.mean(distances)) if distances else None,
        "mean_classified_candidates": float(np.mean([
            row["stats"]["classified_count"] for row in records]))
        if "classified_count" in records[0]["stats"] else None,
        "routes": dict(Counter(row["stats"].get("route", "full") for row in records)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v1", type=Path)
    parser.add_argument("--v2", type=Path)
    parser.add_argument("--profiles", nargs='+', type=Path,
                        help='compare preserved JSON versions, replacing the v1/v2 study')
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--captures-root", type=Path, default=Path("artifacts/captures"))
    parser.add_argument("--captures", nargs="+", default=[
        "capture-20260914-091540", "capture-20260914-093059"])
    parser.add_argument("--annotations", type=Path, default=Path("data/red-ball-20260914/annotations.json"))
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--recovery-study", action="store_true",
                        help="compare v1 SVM, original hybrid, recovery-only, recovery+frame cache")
    args = parser.parse_args()
    if args.fps <= 0 or args.threads < 1:
        parser.error("FPS and threads must be positive")
    if not args.profiles and (args.v1 is None or args.v2 is None):
        parser.error('provide --profiles or both --v1 and --v2')
    if args.out.exists() and any(args.out.iterdir()):
        parser.error('output is not empty; choose a new report directory to preserve old results')
    cv2.setNumThreads(args.threads)
    cfg = DetectorConfig()
    factories = {
        "lab": lambda: RedBallDetector(cfg),
        "svm_v1": lambda: RecordedEdgeDetector(args.v1, cfg),
        "svm_v2": lambda: RecordedEdgeDetector(args.v2, cfg),
        "hybrid_v1": lambda: HybridBallDetector(args.v1, cfg,
                                               recent_hit_recovery_seconds=0, reuse_frame_scores=False),
        "hybrid_v2": lambda: HybridBallDetector(args.v2, cfg,
                                               recent_hit_recovery_seconds=0, reuse_frame_scores=False),
    }
    if args.recovery_study:
        factories.pop('svm_v2')
        factories.pop('hybrid_v2')
        factories['hybrid_recovery'] = lambda: HybridBallDetector(
            args.v1, cfg, reuse_frame_scores=False)
        factories['hybrid_optimized'] = lambda: HybridBallDetector(args.v1, cfg)
    if args.profiles:
        factories = {}
        for path in args.profiles:
            profile = json.loads(path.read_text(encoding='utf-8'))
            name = profile['id']
            if name in factories:
                parser.error(f'duplicate profile ID: {name}')

            def factory(path=path):
                detector = load_detector(path, cfg)
                if not isinstance(detector, HybridBallDetector):
                    profile = json.loads(path.read_text(encoding='utf-8'))
                    detector = RecordedEdgeDetector(path.parent / profile['model'], detector.config)
                return detector

            factories[name] = factory
    labels = [row for row in json.loads(args.annotations.read_text(encoding="utf-8"))
              if row["capture"] in args.captures]
    label_lookup = {(r["capture"], r["frame"]): r for r in labels}
    splits = list(dict.fromkeys(row['split'] for row in labels))
    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "platform": platform.platform(), "opencv": cv2.__version__,
        "numpy": np.__version__, "opencv_threads": cv2.getNumThreads(),
        "config": asdict(cfg), "synthetic_capture_fps": args.fps,
        "study": "recent-hit recovery and same-frame cache" if args.recovery_study else "feature-v2",
        "models": {name: {"path": str(path), "bytes": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                   for name, path in (("v1", args.v1), ("v2", args.v2)) if path is not None},
        "profiles": [json.loads(path.read_text(encoding='utf-8')) for path in args.profiles or []],
        "notes": ["PC only; no Orange Pi performance claim.",
                  "Inference time excludes decode, logging and preview; no profiler.",
                  "Sequential detector calls; rotating order; no parallel benchmarks.",
                  "Replay time assumes 10 Hz unless changed, not measured capture timestamps.",
                  "Existing sparse annotations; test already inspected previously.",
                  "Training split metrics are development, not held-out performance.",
                  "LAB has no bounding box API: its localization IoU is not inferred from a circle.",
                  "Candidate counts and center motion are not accuracy or camera-motion-free jitter."],
        "captures": {},
    }
    all_labeled = {name: [] for name in factories if name != "lab"}
    for capture in args.captures:
        paths = sorted((args.captures_root / capture).glob("*.png"))
        if not paths:
            raise ValueError(f"no frames in {capture}")
        warm = cv2.imread(str(paths[0]))
        for factory in factories.values():
            detector = factory()
            for index in range(5):
                if isinstance(detector, HybridBallDetector):
                    detector.process(warm, now=index / args.fps)
                else:
                    detector.process(warm)
        detectors = {name: factory() for name, factory in factories.items()}
        records = {name: [] for name in factories}
        names = list(detectors)
        for index, path in enumerate(paths):
            frame = cv2.imread(str(path))
            if frame is None or frame.shape[:2] != (480, 640):
                raise ValueError(f"expected readable 640x480 capture: {path}")
            order = names[index % len(names):] + names[:index % len(names)]
            for name in order:
                detector = detectors[name]
                before = time.perf_counter()
                if isinstance(detector, HybridBallDetector):
                    detection = detector.process(frame, now=index / args.fps)
                else:
                    detection = detector.process(frame)
                elapsed = (time.perf_counter() - before) * 1000
                predictions = getattr(detector, "last_predictions", [])
                row = {
                    "frame": path.name, "ms": elapsed,
                    "candidate": bool(detection.candidate_detected),
                    "control": bool(detection.has_current_target),
                    "center": [detection.center_x, detection.center_y]
                    if detection.center_x is not None else None,
                    "radius": detection.radius, "score": detection.score,
                    "box": predictions[0][0] if predictions else None,
                    "stats": dict(getattr(detector, "last_stats", {})),
                }
                records[name].append(row)
                label = label_lookup.get((capture, path.name))
                if label is not None and name != "lab":
                    labeled = dict(row, capture=capture, truth=label["box"], split=label["split"])
                    labeled["iou"] = box_iou(row["box"], label["box"]) if (
                        row["box"] is not None and label["box"] is not None) else 0.0
                    all_labeled[name].append(labeled)
            if (index + 1) % 200 == 0:
                print(f"{capture}: {index + 1}/{len(paths)}", flush=True)
        report["captures"][capture] = {name: aggregate(rows) for name, rows in records.items()}
        (args.out / f"{capture}.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        make_preview(args.out, capture, paths, records)
        print(json.dumps(report["captures"][capture], indent=2), flush=True)
    report["annotated_sequential"] = {
        name: {split: metrics([row for row in rows if row["split"] == split])
               for split in splits} for name, rows in all_labeled.items()}
    # Isolate fresh acquisition from favorable preceding tracking state.
    fresh = {name: [] for name in all_labeled}
    for label in labels:
        frame = cv2.imread(str(args.captures_root / label["capture"] / label["frame"]))
        for name, fresh_rows in fresh.items():
            detector = factories[name]()
            predictions = detector.predict_boxes(frame, now=0.0) if isinstance(
                detector, HybridBallDetector) else detector.predict_boxes(frame)
            box = predictions[0][0] if predictions else None
            fresh_rows.append(dict(label, truth=label["box"], box=box,
                                    iou=box_iou(box, label["box"]) if box and label["box"] else 0.0))
    report["annotated_fresh"] = {
        name: {split: metrics([row for row in rows if row["split"] == split])
               for split in splits} for name, rows in fresh.items()}
    (args.out / "annotated.json").write_text(json.dumps(
        {"sequential": all_labeled, "fresh": fresh}, indent=2), encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"annotated_sequential": report["annotated_sequential"],
                      "annotated_fresh": report["annotated_fresh"]}, indent=2), flush=True)


def make_preview(out, capture, paths, records, selected=None):
    if selected is None:
        selected = [1, 116, 190, 568] if "093059" in capture else [857, 986, 1414, 1543]
    sheets = []
    for number in selected:
        frame = cv2.imread(str(paths[number - 1]))
        tiles = []
        for name, rows in records.items():
            row = rows[number - 1]
            tile = frame.copy()
            if row["box"] is not None:
                x1, y1, x2, y2 = row["box"]
                cv2.rectangle(tile, (x1, y1), (x2, y2), (0, 255, 0), 2)
            elif name == "lab" and row["candidate"]:
                cv2.circle(tile, tuple(round(v) for v in row["center"]),
                           round(row["radius"]), (0, 255, 0), 2)
            tile = cv2.resize(tile, (320, 240))
            cv2.rectangle(tile, (0, 0), (320, 24), (0, 0, 0), -1)
            cv2.putText(tile, f'{number:05} {name} hit={int(row["candidate"])}',
                        (4, 17), cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 255, 255), 1)
            tiles.append(tile)
        sheets.append(np.hstack(tiles))
    cv2.imwrite(str(out / f"{capture}-comparison.jpg"), np.vstack(sheets))


if __name__ == "__main__":
    main()
