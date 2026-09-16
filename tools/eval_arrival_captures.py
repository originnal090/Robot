"""Replay saved capture frames through Ascend YOLO and both arrival-size versions."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import cv2

from hcirobot.ascend_yolo import AscendYoloDetector
from hcirobot.config import ascend_yolo_config, controller_config, detector_config, load_config
from hcirobot.controller import VisualApproachController
from hcirobot.model import ControlState, Detection


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def replay(detections: list[Detection], config, version: str) -> dict:
    controller = VisualApproachController(replace(config, arrival_size_version=version))
    controller.arm(0.0)
    # Captures are deliberately thinned.  Start at the approach gate so this
    # audit measures ARRIVED evidence rather than sparse-frame alignment timing.
    controller.state = ControlState.APPROACHING
    states = []
    now = 0.0
    for detection in detections:
        decision = controller.update(detection, now)
        states.append(decision.state.value)
        now += 0.1
        if decision.state is ControlState.ARRIVED:
            break
    if controller.state is ControlState.FINAL_APPROACH:
        decision = controller.update(
            Detection(False, False, None, None, None, 640, 480),
            now + config.final_approach_seconds,
        )
        states.append(decision.state.value)
    return {
        "final_state": controller.state.value,
        "entered_final_approach": ControlState.FINAL_APPROACH.value in states,
        "arrived": controller.state is ControlState.ARRIVED,
    }


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    temporal = detector_config(config["detection"])
    detector = AscendYoloDetector(ascend_yolo_config(config["npu"]), temporal)
    controller = controller_config(config["controller"])
    report = {"config": str(args.config), "captures": []}
    try:
        for directory in sorted(path for path in args.captures.iterdir() if path.is_dir()):
            detector.update_config(temporal)
            detections = []
            frames = []
            for path in sorted(directory.glob("*.png")):
                frame = cv2.imread(str(path))
                if frame is None:
                    raise ValueError(f"cannot read capture frame: {path}")
                detection = detector.process(frame)
                detections.append(detection)
                frames.append(
                    {
                        "name": path.name,
                        "detected": detection.has_current_target,
                        "bottom_clipped": detection.bottom_clipped,
                        "error": (
                            round(detection.horizontal_error, 4)
                            if detection.has_current_target
                            else None
                        ),
                        "radius_v1": (
                            round(detection.arrival_radius_ratio("v1"), 4)
                            if detection.has_current_target
                            else None
                        ),
                        "radius_v2": (
                            round(detection.arrival_radius_ratio("v2"), 4)
                            if detection.has_current_target
                            else None
                        ),
                    }
                )
            report["captures"].append(
                {
                    "name": directory.name,
                    "frame_count": len(frames),
                    "detected_frames": sum(row["detected"] for row in frames),
                    "clipped_frames": sum(row["bottom_clipped"] for row in frames),
                    "v1": replay(detections, controller, "v1"),
                    "v2": replay(detections, controller, "v2"),
                    "frames": frames,
                }
            )
    finally:
        detector.close()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
