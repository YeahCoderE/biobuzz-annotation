#!/usr/bin/env python
"""Measure the detector on held-out labelled photos (never used for training).

    .venv/Scripts/python -I tools/evaluate.py --model runs/v1/weights/best.pt [--split real_test] [--sheet out.jpg]

Per ground-truth cell:
  found      a predicted cell of the same colour overlaps it (box IoU >= 0.5)
  corner err distance of each corner to the truth, as a fraction of the cell size (box diagonal)
  success    found AND at least 9 of its 10 in-image corners within 5% of the cell size
Also reports precision (false detections) and line-endpoint errors for the 14 main.py lines.
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hivekit as hk  # noqa: E402
import detect as dt  # noqa: E402

ROOT = HERE.parent
YOLO = ROOT / "dataset" / "yolo"


def read_gt(label_path, W, H):
    cells = []
    for line in Path(label_path).read_text().split("\n"):
        v = line.split()
        if len(v) < 35:
            continue
        v = [float(x) for x in v]
        cx, cy, bw, bh = v[1] * W, v[2] * H, v[3] * W, v[4] * H
        k = np.array(v[5:35]).reshape(10, 3)
        k[:, 0] *= W
        k[:, 1] *= H
        cells.append({"cls": int(v[0]), "box": [cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], "kpts": k})
    return cells


def box_iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--split", default="real_test")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--imgsz", type=int, default=None)
    ap.add_argument("--refine", action="store_true")
    ap.add_argument("--sheet")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    model = dt.load_model(args.model, args.imgsz)
    img_dir, lab_dir = YOLO / "images" / args.split, YOLO / "labels" / args.split
    per_cell, false_pos, tiles = [], 0, []
    for img_path in sorted(img_dir.glob("*.jpg")):
        img = cv2.imread(str(img_path))
        H, W = img.shape[:2]
        gt = read_gt(lab_dir / f"{img_path.stem}.txt", W, H)
        pred = dt.detect_image(model, img, args.conf, args.refine)
        cost = np.ones((len(gt), len(pred)))
        for i, g in enumerate(gt):
            for j, p in enumerate(pred):
                if (p["color"] == "red") == (g["cls"] == 0):
                    cost[i, j] = 1 - box_iou(g["box"], p["box"])
        rows, cols = linear_sum_assignment(cost) if len(gt) and len(pred) else ([], [])
        matched = {i: j for i, j in zip(rows, cols) if cost[i, j] <= 0.5}
        meta_file = YOLO / "labels" / f"{args.split}_meta" / f"{img_path.stem}.json"
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else []
        ign_file = YOLO / "labels" / f"{args.split}_ignore" / f"{img_path.stem}.json"
        ignore = json.loads(ign_file.read_text()) if ign_file.exists() else []
        used = set(matched.values())
        for j, p in enumerate(pred):
            if j in used:
                continue
            b = p["box"]
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            if any(ig[0] <= cx <= ig[2] and ig[1] <= cy <= ig[3] for ig in ignore) and \
                    all(box_iou(b, g["box"]) < 0.3 for g in gt):
                continue  # an unlabelled real cell (not a duplicate of a labelled one)
            false_pos += 1
        for i, g in enumerate(gt):
            diag = float(np.hypot(g["box"][2] - g["box"][0], g["box"][3] - g["box"][1]))
            rec = {"image": img_path.stem, "color": "red" if g["cls"] == 0 else "blue", "diag_px": round(diag, 1)}
            if i not in matched:
                rec.update(found=False, success=False)
                per_cell.append(rec)
                continue
            p = pred[matched[i]]
            pk = np.array([p["corners"][c][:2] for c in hk.CORNER_IDS])
            vis = g["kpts"][:, 2] > 0
            if vis.sum() < 4:
                rec.update(found=True, corners=0, within5=0, mean_err=0.0, max_err=0.0, success=True, note="few corners")
                per_cell.append(rec)
                continue
            err = np.linalg.norm(pk[vis] - g["kpts"][vis, :2], axis=1) / max(diag, 1)
            if meta and i < len(meta) and meta[i].get("side_view"):
                # side-on view: which frame is "front" is not defined, accept the 180-degree relabelling
                err2 = np.linalg.norm(pk[dt.ROT180][vis] - g["kpts"][vis, :2], axis=1) / max(diag, 1)
                if err2.mean() < err.mean():
                    err = err2
                rec["side_view"] = True
            ok = int((err <= 0.05).sum())
            rec.update(found=True, corners=int(vis.sum()), within5=ok, mean_err=round(float(err.mean()), 4),
                       max_err=round(float(err.max()), 4), success=bool(ok >= max(1, vis.sum() - 1)),
                       fit_rms_px=p.get("fit_rms_px"))
            per_cell.append(rec)
        if args.sheet:
            vis_img = dt.draw(img, pred)
            for g in gt:
                k = g["kpts"]
                for a in range(10):
                    if k[a, 2] > 0:
                        cv2.circle(vis_img, (int(k[a, 0]), int(k[a, 1])), max(4, W // 250), (255, 255, 255), 2)
            s = 640 / W
            tiles.append(cv2.resize(vis_img, (640, int(H * s))))
    found = [c for c in per_cell if c["found"]]
    n = len(per_cell)
    summary = {
        "cells": n, "found": len(found), "recall": round(len(found) / max(n, 1), 4),
        "false_detections": false_pos, "precision": round(len(found) / max(len(found) + false_pos, 1), 4),
        "corner_mean_err_pct": round(100 * float(np.mean([c["mean_err"] for c in found])) if found else -1, 2),
        "corners_within_5pct": round(sum(c["within5"] for c in found) / max(sum(c["corners"] for c in found), 1), 4),
        "cell_success_rate": round(sum(c["success"] for c in per_cell) / max(n, 1), 4),
    }
    print(json.dumps(summary, indent=1))
    out = Path(args.out) if args.out else ROOT / "runs" / f"eval_{Path(args.model).parent.parent.name}_{args.split}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "cells": per_cell}, indent=1))
    if args.sheet and tiles:
        w = 640
        rows = []
        for i in range(0, len(tiles), 3):
            row = tiles[i:i + 3]
            h = max(t.shape[0] for t in row)
            row = [cv2.copyMakeBorder(t, 0, h - t.shape[0], 0, 0, cv2.BORDER_CONSTANT) for t in row]
            row += [np.zeros((h, w, 3), np.uint8)] * (3 - len(row))
            rows.append(np.hstack(row))
        cv2.imwrite(args.sheet, np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 85])


if __name__ == "__main__":
    main()
