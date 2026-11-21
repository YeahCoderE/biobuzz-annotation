#!/usr/bin/env python
"""Train the BIOBUZZ hive-cell keypoint detector (Ultralytics YOLO-pose, CPU friendly).

    .venv/Scripts/python -I tools/train.py --name v1 --epochs 8 [--model yolo11n-pose.pt] [--fraction 1.0]

The model finds every hive cell (class red_cell / blue_cell) and its 10 corners in the
order of hivekit.CORNER_IDS. tools/detect.py turns that into the 14 lines of main.py.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="v1")
    ap.add_argument("--model", default="yolo11n-pose.pt")
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--imgsz", type=int, default=512)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--fraction", type=float, default=1.0)
    ap.add_argument("--lr0", type=float, default=0.002)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--save-period", type=int, default=1)
    ap.add_argument("--mosaic", type=float, default=0.5)
    ap.add_argument("--close-mosaic", type=int, default=1)
    args = ap.parse_args()
    import torch
    torch.set_num_threads(max(1, (torch.get_num_threads())))
    from ultralytics import YOLO
    if args.resume:
        model = YOLO(str(ROOT / "runs" / args.name / "weights" / "last.pt"))
        model.train(resume=True)
        return
    model = YOLO(args.model)
    model.train(
        data=str(ROOT / "dataset" / "yolo" / "biobuzz.yaml"), imgsz=args.imgsz, epochs=args.epochs,
        batch=args.batch, workers=args.workers, device="cpu", project=str(ROOT / "runs"), name=args.name,
        exist_ok=True, cache=False, patience=0, optimizer="AdamW", lr0=args.lr0, cos_lr=True, warmup_epochs=0.5,
        mosaic=args.mosaic, close_mosaic=args.close_mosaic, degrees=12.0, translate=0.12, scale=0.5, shear=2.0, perspective=0.0005,
        fliplr=0.5, hsv_h=0.012, hsv_s=0.45, hsv_v=0.4, pose=14.0, kobj=1.0, amp=False, plots=True,
        fraction=args.fraction, save_period=args.save_period, seed=1, deterministic=False, val=True,
    )


if __name__ == "__main__":
    main()
