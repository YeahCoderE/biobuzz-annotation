#!/usr/bin/env python
"""Auto-label harvested frames with a trained model and keep only the verified ones.

    .venv/Scripts/python -I tools/pseudo.py --model runs/v1/weights/best.pt [--max-per-video 80]

A frame is accepted when every detected cell passes two independent checks:
  1. geometry: the 3D cell fitted to the predicted corners reproduces them (small PnP error), and
  2. pixels: the silhouette of that fitted cell covers the red/blue rib pixels (mask IoU),
     after a short silhouette refinement for borderline cases.
Accepted frames go to dataset/yolo/images/pseudo (+ labels/pseudo) with a JSON report.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hivekit as hk  # noqa: E402
import fit_label as fl  # noqa: E402
import detect as dt  # noqa: E402
from build_yolo import OUTLINE  # noqa: E402

ROOT = HERE.parent
YOLO = ROOT / "dataset" / "yolo"
M = hk.MODEL


def silhouette_iou(cam, mask, box, size):
    W, H = size
    x0, y0, x1, y1 = box
    pad = 0.15 * max(x1 - x0, y1 - y0)
    X0, Y0 = int(max(0, x0 - pad)), int(max(0, y0 - pad))
    X1, Y1 = int(min(W - 1, x1 + pad)), int(min(H - 1, y1 + pad))
    if X1 - X0 < 8 or Y1 - Y0 < 8:
        return 0.0
    s = min(1.0, 300.0 / max(X1 - X0, Y1 - Y0))
    w, h = int((X1 - X0) * s) + 1, int((Y1 - Y0) * s) + 1
    sil = fl.rib_mask(cam, (w, h), s, (X0 * s, Y0 * s))
    if sil is None:
        return 0.0
    obs = cv2.resize(mask[Y0:Y1 + 1, X0:X1 + 1], (w, h), interpolation=cv2.INTER_AREA) > 96
    model = sil > 0
    return float((model & obs).sum() / max((model | obs).sum(), 1))


def label_rows(cells, W, H):
    rows = []
    for c in cells:
        cam = c["cam"]
        pts = cam.project(OUTLINE)
        x0, y0 = np.clip(pts.min(0), 0, [W - 1, H - 1])
        x1, y1 = np.clip(pts.max(0), 0, [W - 1, H - 1])
        if x1 - x0 < 10 or y1 - y0 < 10:
            continue
        vals = [c["cls"], (x0 + x1) / 2 / W, (y0 + y1) / 2 / H, (x1 - x0) / W, (y1 - y0) / H]
        for x, y in cam.project(M.corner_world):
            vals += [x / W, y / H, 2] if (0 <= x <= W - 1 and 0 <= y <= H - 1) else [0, 0, 0]
        rows.append(" ".join(f"{v:.6f}" if isinstance(v, float) else str(v) for v in vals))
    return rows


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--sources", nargs="*", default=[str(ROOT / "dataset" / "real" / "pool"),
                                                      str(ROOT / "dataset" / "real" / "webflat")])
    ap.add_argument("--conf", type=float, default=0.45)
    ap.add_argument("--min-iou", type=float, default=0.42)
    ap.add_argument("--max-per-video", type=int, default=80)
    ap.add_argument("--imgsz", type=int, default=512)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    model = dt.load_model(args.model, args.imgsz)
    files = []
    for src in args.sources:
        files += sorted(p for p in Path(src).rglob("*.jpg"))
    if args.limit:
        files = files[:args.limit]
    out_img, out_lab = YOLO / "images" / "pseudo", YOLO / "labels" / "pseudo"
    out_img.mkdir(parents=True, exist_ok=True)
    out_lab.mkdir(parents=True, exist_ok=True)
    per_video = defaultdict(int)
    stats = defaultdict(int)
    accepted = []
    for n, path in enumerate(files):
        video = path.parent.name
        if per_video[video] >= args.max_per_video:
            continue
        img = cv2.imread(str(path))
        if img is None:
            continue
        H, W = img.shape[:2]
        dets = model(img, conf=args.conf)
        if not dets:
            stats["no_detection"] += 1
            continue
        masks = {}
        cells, ok = [], True
        for d in dets:
            cam, proj, rms = dt.geometric_fit(d["kpts"], (W, H))
            if cam is None:
                ok = False
                stats["no_fit"] += 1
                break
            span = float(np.linalg.norm(proj.max(0) - proj.min(0)))
            if rms > max(4.0, 0.05 * span):
                ok = False
                stats["bad_geometry"] += 1
                break
            color = dt.NAMES[d["cls"]]
            if color not in masks:
                masks[color] = fl.color_mask(img, color)
            iou = silhouette_iou(cam, masks[color], d["box"], (W, H))
            if iou < args.min_iou and iou > 0.25:
                cam2, _, st = fl.refine(masks[color], np.zeros((H, W), np.uint8), cam, (W, H))
                if cam2 is not None and st.get("iou_roi", 0) > iou:
                    cam, iou = cam2, st["iou_roi"]
            if iou < args.min_iou or not fl.plausible(cam, W, H):
                ok = False
                stats["bad_pixels"] += 1
                break
            if cam.center[2] < 0:
                cam = fl.flip_camera(cam)
            cells.append({"cls": d["cls"], "cam": cam, "iou": round(iou, 3), "conf": round(d["conf"], 3)})
        if not ok or not cells:
            continue
        rows = label_rows(cells, W, H)
        if not rows:
            continue
        name = f"ps_{video}_{path.stem}"[:120]
        cv2.imwrite(str(out_img / f"{name}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 93])
        (out_lab / f"{name}.txt").write_text("\n".join(rows) + "\n")
        per_video[video] += 1
        stats["accepted"] += 1
        accepted.append({"name": name, "source": str(path), "cells": [{k: c[k] for k in ("cls", "iou", "conf")} for c in cells]})
        if n % 200 == 0:
            print(n, "/", len(files), dict(stats), flush=True)
    (YOLO / "pseudo_report.json").write_text(json.dumps({"stats": stats, "accepted": accepted}, indent=1))
    print("DONE", dict(stats), "videos with frames:", len(per_video))


if __name__ == "__main__":
    main()
