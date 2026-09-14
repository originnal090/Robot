"""Benchmark LAB or the experimental SVM on the actual deployment board, without IO/actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from hcirobot.config import detector_config, load_config
from hcirobot.detector import RedBallDetector
from hcirobot.detector_profile import load_detector
from hcirobot.hybrid_detector import HybridBallDetector


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--edge-model", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument('--fps', type=float, default=10., help='capture-time clock for hybrid replay')
    parser.add_argument('--sampling', choices=('sequential', 'uniform'), default='sequential')
    args = parser.parse_args()
    if args.limit < 1 or args.threads < 1 or not np.isfinite(args.fps) or args.fps <= 0:
        parser.error("--limit, --threads and --fps must be positive and finite")
    if args.out.exists():
        parser.error('output exists; choose a new report path')
    cv2.setNumThreads(args.threads)
    cfg = detector_config(load_config(args.config)["detection"])
    if args.edge_model:
        detector = load_detector(args.edge_model, cfg)
        cfg = detector.config
    else:
        detector = RedBallDetector(cfg)
    files = sorted(p for p in args.frames.iterdir() if p.suffix.lower() in {".png", ".jpg"})
    if not files:
        parser.error("no images found")
    count = min(args.limit, len(files))
    selected = np.arange(count) if args.sampling == 'sequential' else np.linspace(
        0, len(files) - 1, count, dtype=int)
    times, candidates = [], 0
    for index in selected:
        frame = cv2.imread(str(files[index]))
        if frame is None:
            raise ValueError(f"unreadable image: {files[index]}")
        if not times:
            for step in range(5):
                if isinstance(detector, HybridBallDetector):
                    detector.process(frame, now=(step - 5) / args.fps)
                else:
                    detector.process(frame)
            detector.update_config(cfg)
        start = time.perf_counter()
        result = detector.process(frame, now=index / args.fps) if isinstance(
            detector, HybridBallDetector) else detector.process(frame)
        times.append((time.perf_counter() - start) * 1000)
        candidates += int(result.candidate_detected)
    board = Path("/proc/device-tree/model")
    try:
        import resource

        peak_rss_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if platform.system() != "Darwin":
            peak_rss_bytes *= 1024
    except ImportError:
        peak_rss_bytes = None
    profile = None
    weight_path = args.edge_model
    if args.edge_model and args.edge_model.suffix.lower() == '.json':
        profile = json.loads(args.edge_model.read_text(encoding='utf-8'))
        weight_path = args.edge_model.parent / profile['model']
    report = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "board": board.read_text().strip("\0") if board.exists() else None,
        "opencv": cv2.__version__,
        "threads": cv2.getNumThreads(),
        "detector": type(detector).__name__,
        "profile": profile,
        "detector_config": asdict(cfg),
        "model_bytes": weight_path.stat().st_size if weight_path else 0,
        "model_sha256": hashlib.sha256(weight_path.read_bytes()).hexdigest() if weight_path else None,
        "synthetic_capture_fps": args.fps,
        "sampling": args.sampling,
        "first_frame": files[selected[0]].name,
        "last_frame": files[selected[-1]].name,
        "frames": len(times),
        "processing_size": [cfg.processing_width, cfg.processing_height],
        "median_ms": float(np.median(times)),
        "p95_ms": float(np.percentile(times, 95)),
        "mean_ms": float(np.mean(times)),
        "process_peak_rss_bytes": peak_rss_bytes,
        "candidate_frames": candidates,
        "notes": "Inference only; excludes image decoding. Candidate count is not accuracy. "
        "Hybrid uses original frame indices / FPS; uniform sampling skips tracking observations. "
        "RSS is whole-process peak including Python/OpenCV; null where unavailable. "
        "Only target-board measurements establish OrangePi latency.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
