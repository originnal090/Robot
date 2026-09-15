"""Probe a focused Ultralytics YOLO fine-tune on an Ascend NPU.

This entry point is intentionally separate from the route-level selection
workflow: it consumes an already materialized YOLO dataset and never replaces
the robot's active model or service configuration. The tested 310B1/CANN 8.0
stack does not currently complete YOLO classification-loss backward.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from train_yolo_detector import enable_ascend_npu_compatibility


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--freeze-layers", type=int, default=11)
    parser.add_argument("--lr", type=float, default=5e-4)
    parser.add_argument("--seed", type=int, default=20260915)
    parser.add_argument("--detect-anomaly", action="store_true")
    args = parser.parse_args()

    import torch
    import torch_npu  # noqa: F401  # registers the private-use NPU backend
    from ultralytics import YOLO

    torch.npu.set_device(0)
    torch.npu.set_compile_mode(jit_compile=False)
    torch.autograd.set_detect_anomaly(args.detect_anomaly)
    enable_ascend_npu_compatibility()

    model = YOLO(args.model)
    model.train(
        data=str(args.data.resolve()),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch_size,
        device="npu:0",
        workers=0,
        optimizer="AdamW",
        lr0=args.lr,
        lrf=0.02,
        weight_decay=5e-4,
        warmup_epochs=1,
        patience=30,
        hsv_h=0.03,
        hsv_s=0.55,
        hsv_v=0.45,
        translate=0.08,
        scale=0.35,
        fliplr=0.5,
        mosaic=0.5,
        close_mosaic=1,
        degrees=0,
        shear=0,
        perspective=0,
        mixup=0,
        copy_paste=0,
        seed=args.seed,
        deterministic=True,
        single_cls=True,
        amp=True,
        freeze=args.freeze_layers,
        val=False,
        plots=False,
        save=True,
        save_period=-1,
        project=str(args.project.resolve()),
        name=args.name,
        exist_ok=False,
        verbose=False,
    )


if __name__ == "__main__":
    main()
