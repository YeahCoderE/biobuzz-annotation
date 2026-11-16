#!/usr/bin/env python
"""Rank harvested frames by how much saturated red/blue (rib colour) they contain.

    .venv/Scripts/python -I tools/prefilter.py  ->  dataset/real/pool_rank.csv
"""
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent


def main():
    import psutil
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    files = sorted((ROOT / "dataset" / "real" / "pool").rglob("*.jpg")) + sorted((ROOT / "dataset" / "real" / "webflat").glob("*.jpg"))
    out = ROOT / "dataset" / "real" / "pool_rank.csv"
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "video", "red", "blue"])
        for i, p in enumerate(files):
            img = cv2.imread(str(p), cv2.IMREAD_REDUCED_COLOR_4)
            if img is None:
                continue
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
            red = float((((h <= 6) | (h >= 167)) & (s >= 110) & (v >= 60)).mean())
            blue = float(((h >= 100) & (h <= 125) & (s >= 110) & (v >= 50)).mean())
            w.writerow([str(p.relative_to(ROOT)), p.parent.name, round(red, 5), round(blue, 5)])
            if i % 5000 == 0:
                print(i, "/", len(files), flush=True)
    print("done", len(files))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
