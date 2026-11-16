#!/usr/bin/env python
"""Copy-paste augmentation with REAL cells: cut verified cells out of the training photos
along their fitted 3D silhouette and paste them onto background frames with random
rotation, scale, flip (left/right labels swapped), mild perspective and colour changes.
The corners are moved with exactly the same transform, so the labels stay exact.

    .venv/Scripts/python -I tools/paste.py --count 6000 [--seed 3]
Writes dataset/yolo/images/paste + labels/paste.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hivekit as hk  # noqa: E402
import render as rd  # noqa: E402
from build_yolo import DROP, PARTIAL, TEST, OUTLINE  # noqa: E402

ROOT = HERE.parent
YOLO = ROOT / "dataset" / "yolo"
M = hk.MODEL


def load_sources(max_side=1100):
    sources = []
    for path in sorted((ROOT / "dataset" / "labels").glob("*.json")):
        if path.stem in DROP or path.stem in TEST:
            continue
        label = json.loads(path.read_text(encoding="utf-8"))
        cells = [c for c in label["cells"] if "camera" in c]
        if path.stem in PARTIAL:
            cells = [c for c in cells if c.get("fit", {}).get("iou_roi", 0) >= 0.35]
        img = cv2.imread(str(ROOT / label["image"]))
        if img is None:
            continue
        H, W = img.shape[:2]
        for c in cells:
            cam = hk.Camera.from_dict(c["camera"])
            outline = cam.project(OUTLINE)
            corners = cam.project(M.corner_world)
            if outline.min() < 0 or outline[:, 0].max() >= W or outline[:, 1].max() >= H:
                continue  # only cells that are completely inside the photo
            hull = cv2.convexHull(outline.astype(np.float32)).reshape(-1, 2)
            x0, y0 = np.floor(hull.min(0)).astype(int)
            x1, y1 = np.ceil(hull.max(0)).astype(int)
            pad = int(0.04 * max(x1 - x0, y1 - y0)) + 2
            x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
            crop = img[y0:y1, x0:x1].copy()
            mask = np.zeros(crop.shape[:2], np.uint8)
            cv2.fillConvexPoly(mask, np.round((hull - [x0, y0]) * 4).astype(np.int32), 255, cv2.LINE_AA, 2)
            s = min(1.0, max_side / max(crop.shape[:2]))
            if s < 1:
                crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
                mask = cv2.resize(mask, (crop.shape[1], crop.shape[0]), interpolation=cv2.INTER_AREA)
            sources.append({"crop": crop, "mask": mask, "corners": (corners - [x0, y0]) * s,
                            "outline": (outline - [x0, y0]) * s, "cls": 0 if c["color"] == "red" else 1,
                            "name": path.stem})
    return sources


def random_homography(w, h, rng, out_w, out_h, target):
    """Similarity (+ small perspective) placing a w x h crop so its longer side spans `target` px."""
    scale = target / max(w, h)
    ang = rng.normal(0, 0.35) if rng.random() < 0.8 else rng.uniform(-math.pi, math.pi)
    c, s = math.cos(ang) * scale, math.sin(ang) * scale
    A = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])
    pts = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    jit = rng.normal(0, 0.035, (4, 2)) * [w, h]
    P = cv2.getPerspectiveTransform(pts, (pts + jit).astype(np.float32))
    Hm = A @ P
    box = cv2.perspectiveTransform(pts[None], Hm)[0]
    bw, bh = box.max(0) - box.min(0)
    lo_x, hi_x = sorted((-0.25 * bw, out_w - 0.75 * bw))
    lo_y, hi_y = sorted((-0.25 * bh, out_h - 0.75 * bh))
    tx = rng.uniform(lo_x, hi_x) - box.min(0)[0]
    ty = rng.uniform(lo_y, hi_y) - box.min(0)[1]
    return np.array([[1, 0, tx], [0, 1, ty], [0, 0, 1.0]]) @ Hm


def make_image(sources, rng):
    W, H = [(640, 480), (480, 640), (640, 360), (512, 512)][int(rng.integers(4))]
    bg = rd.load_background(W, H, rng)
    out = bg.astype(np.float32)
    labels = []
    n = 1 if rng.random() < 0.7 else 2
    for _ in range(n):
        src = sources[int(rng.integers(len(sources)))]
        crop, mask = src["crop"], src["mask"]
        corners, outline = src["corners"].copy(), src["outline"].copy()
        h, w = crop.shape[:2]
        if rng.random() < 0.5:  # mirror: x -> w - x, left/right labels swap
            crop, mask = crop[:, ::-1], mask[:, ::-1]
            corners[:, 0] = w - 1 - corners[:, 0]
            outline[:, 0] = w - 1 - outline[:, 0]
            corners = corners[hk.FLIP_INDEX]
        # colour jitter on the cell (hue kept so red stays red)
        hsv = cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_BGR2HSV).astype(np.float32)
        hsv[..., 1] *= rng.uniform(0.75, 1.15)
        hsv[..., 2] *= rng.uniform(0.6, 1.25)
        cell = cv2.cvtColor(np.clip(hsv, 0, 255).astype(np.uint8), cv2.COLOR_HSV2BGR)
        target = rng.uniform(0.18, 1.05) * max(W, H) if rng.random() < 0.85 else rng.uniform(0.06, 0.18) * max(W, H)
        Hm = random_homography(w, h, rng, W, H, target)
        warped = cv2.warpPerspective(cell, Hm, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        feather = max(1.0, 0.012 * target)
        m = cv2.warpPerspective(np.ascontiguousarray(mask), Hm, (W, H), flags=cv2.INTER_LINEAR)
        m = cv2.GaussianBlur(m.astype(np.float32) / 255.0, (0, 0), feather)[..., None]
        out = out * (1 - m) + warped.astype(np.float32) * m
        kp = cv2.perspectiveTransform(corners[None].astype(np.float32), Hm)[0]
        ol = cv2.perspectiveTransform(outline[None].astype(np.float32), Hm)[0]
        x0, y0 = np.clip(ol.min(0), 0, [W - 1, H - 1])
        x1, y1 = np.clip(ol.max(0), 0, [W - 1, H - 1])
        if x1 - x0 < 12 or y1 - y0 < 12:
            continue
        inside = (kp[:, 0] >= 0) & (kp[:, 0] <= W - 1) & (kp[:, 1] >= 0) & (kp[:, 1] <= H - 1)
        if inside.sum() < 3:
            continue
        labels.append({"cls": src["cls"], "box": [x0, y0, x1, y1], "kp": kp, "vis": inside})
    img = np.clip(out, 0, 255).astype(np.uint8)
    img, labels = rd.postprocess(img, labels_full(labels), rng)
    return img, labels


def labels_full(labels):
    for lab in labels:
        lab["full_box"] = lab["box"]
    return labels


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=3)
    args = ap.parse_args()
    import psutil
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    sources = load_sources()
    print("source cells:", len(sources), sorted({s["name"] for s in sources}), flush=True)
    rng = np.random.default_rng(args.seed)
    img_dir, lab_dir = YOLO / "images" / "paste", YOLO / "labels" / "paste"
    img_dir.mkdir(parents=True, exist_ok=True)
    lab_dir.mkdir(parents=True, exist_ok=True)
    for i in range(args.count):
        img, labels = make_image(sources, rng)
        H, W = img.shape[:2]
        name = f"cp_{i:05d}"
        cv2.imwrite(str(img_dir / f"{name}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(70, 96))])
        (lab_dir / f"{name}.txt").write_text("\n".join(rd.yolo_lines(labels, W, H)) + ("\n" if labels else ""))
        if i % 1000 == 0:
            print(i, flush=True)
    print("done", args.count)


if __name__ == "__main__":
    main()
