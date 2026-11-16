#!/usr/bin/env python
"""Label harvested video frames automatically, keeping only fits that are reliable.

    .venv/Scripts/python -I tools/autolabel.py --model runs/v1_epoch1.pt [--per-video 30] [--hours 6]

For every detection the 3D cell is fitted to the red/blue rib pixels:
  fast path  start from the network's corners (PnP), refine the silhouette;
  slow path  if that is not good enough, search camera poses inside the box (as for the photos).
A cell is accepted only when the fitted silhouette matches the rib pixels with IoU >= 0.5; in the
visual review of the photos every fit above that level was correct. A frame is kept when all its
confident detections are accepted (weak ones are painted out). Frames are spread over each video.
Output: dataset/yolo/images/auto + labels/auto, dataset/yolo/auto_report.jsonl (resumable).
"""
import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hivekit as hk  # noqa: E402
import fit_label as fl  # noqa: E402
import detect as dt  # noqa: E402
from pseudo import label_rows  # noqa: E402

ROOT = HERE.parent
YOLO = ROOT / "dataset" / "yolo"


def frame_time(path):
    m = re.search(r"_t(\d{7})\.jpg$", path.name)
    return int(m.group(1)) / 100 if m else 0.0


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--imgsz", type=int, default=448)
    ap.add_argument("--per-video", type=int, default=30)
    ap.add_argument("--min-gap", type=float, default=4.0, help="seconds between kept frames of one video")
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--accept", type=float, default=0.5)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    args = ap.parse_args()
    import psutil
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    rank = list(csv.DictReader(open(ROOT / "dataset" / "real" / "pool_rank.csv")))
    rank = [r for r in rank if float(r["red"]) + float(r["blue"]) > 0.004]
    # spread work across videos: round-robin over videos, most colourful frames first
    by_video = defaultdict(list)
    for r in sorted(rank, key=lambda r: -(float(r["red"]) + float(r["blue"]))):
        by_video[r["video"]].append(ROOT / r["file"])
    videos = sorted(by_video)
    videos = videos[args.shard::args.shards]
    order = []
    depth = max(len(v) for v in by_video.values())
    for k in range(depth):
        for v in videos:
            if k < len(by_video[v]):
                order.append(by_video[v][k])
    out_img, out_lab = YOLO / "images" / "auto", YOLO / "labels" / "auto"
    out_img.mkdir(parents=True, exist_ok=True)
    out_lab.mkdir(parents=True, exist_ok=True)
    report = YOLO / f"auto_report_{args.shard}.jsonl"
    done, kept_times = set(), defaultdict(list)
    if report.exists():
        for line in report.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            done.add(rec["source"])
            if rec.get("kept"):
                kept_times[rec["video"]].append(rec["t"])
    model = dt.load_model(args.model, args.imgsz)
    t_end = time.time() + args.hours * 3600
    stats = defaultdict(int)
    fh = report.open("a", encoding="utf-8")
    for path in order:
        if time.time() > t_end:
            break
        src = str(path.relative_to(ROOT))
        video = path.parent.name
        if src in done or len(kept_times[video]) >= args.per_video:
            continue
        t = frame_time(path)
        if any(abs(t - k) < args.min_gap for k in kept_times[video]):
            continue
        img = cv2.imread(str(path))
        if img is None:
            continue
        H, W = img.shape[:2]
        dets = model(img, conf=0.3)
        rec = {"source": src, "video": video, "t": t, "kept": False, "cells": []}
        if not dets:
            stats["no_detection"] += 1
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            continue
        masks, ignore, cells, ok, paint = {}, np.zeros((H, W), np.uint8), [], True, []
        for d in sorted(dets, key=lambda d: -(d["box"][2] - d["box"][0]) * (d["box"][3] - d["box"][1])):
            color = dt.NAMES[d["cls"]]
            if color not in masks:
                masks[color] = fl.color_mask(img, color)
            best = None
            cam0, _, rms = dt.geometric_fit(d["kpts"], (W, H))
            if cam0 is not None:
                cam, _, st = fl.refine(masks[color], ignore, cam0, (W, H))
                if cam is not None:
                    best = (cam, st.get("iou_roi", 0), "fast")
            if best is None or best[1] < args.accept + 0.05:
                res = fl.auto_cell(masks[color], ignore, {"box": d["box"], "color": color}, (W, H), 4000, 3)
                if res is not None and (best is None or res[2].get("iou_roi", 0) > best[1]):
                    best = (res[0], res[2].get("iou_roi", 0), "search")
            if best is not None and not fl.plausible(best[0], W, H):
                best = None
            if best is None or best[1] < args.accept:
                if d["conf"] < 0.45:
                    paint.append(d["box"])
                    continue
                ok = False
                stats["fit_failed"] += 1
                break
            cam = best[0]
            if cam.center[2] < 0:
                cam = fl.flip_camera(cam)
            sil = fl.fitted_silhouette_full(cam, (W, H))
            ignore = np.maximum(ignore, cv2.dilate(sil, np.ones((9, 9), np.uint8)))
            cells.append({"cls": d["cls"], "cam": cam})
            rec["cells"].append({"color": color, "iou": round(best[1], 3), "path": best[2], "conf": round(d["conf"], 3),
                                 "camera": cam.to_dict()})
        if ok and cells:
            for x0, y0, x1, y1 in paint:
                x0, y0, x1, y1 = max(0, int(x0)), max(0, int(y0)), min(W, int(x1)), min(H, int(y1))
                if x1 - x0 > 2 and y1 - y0 > 2:
                    roi = img[y0:y1, x0:x1]
                    img[y0:y1, x0:x1] = cv2.GaussianBlur(cv2.resize(cv2.resize(roi, (6, 6)), (x1 - x0, y1 - y0)), (0, 0), 7)
            rows = label_rows(cells, W, H)
            if rows:
                name = f"au_{video}_{path.stem}"[:120]
                cv2.imwrite(str(out_img / f"{name}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 93])
                (out_lab / f"{name}.txt").write_text("\n".join(rows) + "\n")
                rec["kept"] = True
                rec["name"] = name
                kept_times[video].append(t)
                stats["kept"] += 1
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
        n = sum(stats.values())
        if n % 25 == 0:
            print(time.strftime("%H:%M"), dict(stats), "videos with frames:", sum(1 for v in kept_times if kept_times[v]),
                  flush=True)
    print("DONE", dict(stats))


if __name__ == "__main__":
    main()
