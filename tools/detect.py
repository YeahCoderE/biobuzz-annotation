#!/usr/bin/env python
"""Detect BIOBUZZ hive cells in images: colour, 10 corners, the 14 lines of main.py, 3D pose.

    .venv/Scripts/python -I tools/detect.py IMG [IMG ...] [--model models/biobuzz_hive.onnx] [--out runs/detect]

For every cell the network predicts 10 corners; the known 3D shape of the cell is then fitted
to them (PnP), which checks that the corners agree with a real cell, fills corners that are
hidden or outside the picture, and gives the 28 line endpoints in main.py order.

Outputs, per image: an annotated JPEG and a JSON file with
    cells[]: color, confidence, box, corners {id: [x, y, conf]}, lines [{id, name, a:[x,y], b:[x,y]}],
             fit_rms_px, camera (rvec, tvec in inches, f), distance_in, side, level
    record: the main.py annotation record (240 values) so it can be used as training data.
Works with the .onnx model (only onnxruntime needed) or a .pt model (Ultralytics).
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

ROOT = HERE.parent
M = hk.MODEL
DEFAULT_MODEL = ROOT / "models" / "biobuzz_hive.onnx"
if not DEFAULT_MODEL.exists():  # the copy published with the site
    DEFAULT_MODEL = ROOT / "docs" / "models" / "biobuzz_hive.onnx"
NAMES = {0: "red", 1: "blue"}
ROT180 = [hk.CORNER_IDS.index(hk.relabel_rot180(c)) for c in hk.CORNER_IDS]


def load_image(path):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        rgb = np.array(ImageOps.exif_transpose(im).convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


# ---------------------------------------------------------------------------
# Network backends
# ---------------------------------------------------------------------------

class OnnxPose:
    def __init__(self, path, imgsz=None):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        inp = self.sess.get_inputs()[0]
        self.name = inp.name
        shape = inp.shape
        self.imgsz = imgsz or (shape[2] if isinstance(shape[2], int) else 512)

    def __call__(self, img, conf=0.3, iou=0.5):
        h, w = img.shape[:2]
        s = self.imgsz / max(h, w)
        nh, nw = int(round(h * s)), int(round(w * s))
        canvas = np.full((self.imgsz, self.imgsz, 3), 114, np.uint8)
        top, left = (self.imgsz - nh) // 2, (self.imgsz - nw) // 2
        canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        x = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
        out = self.sess.run(None, {self.name: x})[0][0]  # (C, N)
        out = out.T
        boxes, cls_scores, kpts = out[:, :4], out[:, 4:6], out[:, 6:].reshape(-1, 10, 3)
        cls = cls_scores.argmax(1)
        score = cls_scores.max(1)
        keep = score >= conf
        boxes, cls, score, kpts = boxes[keep], cls[keep], score[keep], kpts[keep]
        xyxy = np.stack([boxes[:, 0] - boxes[:, 2] / 2, boxes[:, 1] - boxes[:, 3] / 2,
                         boxes[:, 0] + boxes[:, 2] / 2, boxes[:, 1] + boxes[:, 3] / 2], 1)
        idx = cv2.dnn.NMSBoxes(xyxy[:, :2].tolist() and [[float(a), float(b), float(c - a), float(d - b)] for a, b, c, d in xyxy],
                               score.tolist(), conf, iou) if len(score) else []
        idx = np.array(idx).reshape(-1)
        dets = []
        for i in idx:
            b = (xyxy[i] - [left, top, left, top]) / s
            k = kpts[i].copy()
            k[:, 0] = (k[:, 0] - left) / s
            k[:, 1] = (k[:, 1] - top) / s
            dets.append({"cls": int(cls[i]), "conf": float(score[i]), "box": b.tolist(), "kpts": k})
        return dets


class UltraPose:
    def __init__(self, path, imgsz=512):
        from ultralytics import YOLO
        self.model = YOLO(str(path))
        self.imgsz = imgsz

    def __call__(self, img, conf=0.3, iou=0.5):
        r = self.model.predict(img, imgsz=self.imgsz, conf=conf, iou=iou, verbose=False, device="cpu")[0]
        dets = []
        if r.boxes is None or len(r.boxes) == 0:
            return dets
        kp = r.keypoints.data.cpu().numpy()
        for i in range(len(r.boxes)):
            dets.append({"cls": int(r.boxes.cls[i]), "conf": float(r.boxes.conf[i]),
                         "box": r.boxes.xyxy[i].cpu().numpy().tolist(), "kpts": kp[i]})
        return dets


def load_model(path, imgsz=None):
    path = Path(path)
    if path.suffix == ".onnx":
        return OnnxPose(path, imgsz)
    return UltraPose(path, imgsz or 512)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def geometric_fit(kpts, size, min_conf=0.35):
    """Fit the 3D cell to the predicted corners. Returns (camera, corners10, rms) or (None, raw, None)."""
    good = kpts[:, 2] >= min_conf
    inside = (kpts[:, 0] >= -2) & (kpts[:, 0] <= size[0] + 2) & (kpts[:, 1] >= -2) & (kpts[:, 1] <= size[1] + 2)
    use = good & inside
    if use.sum() < 5:
        return None, kpts[:, :2].copy(), None
    world = M.corner_world[use]
    image = kpts[use, :2]
    cam, res = hk.fit_camera(world, image, size, weights=np.clip(kpts[use, 2], 0.2, 1.0))
    if cam is None:
        return None, kpts[:, :2].copy(), None
    if cam.center[2] < 0:  # keep the convention: front frame nearer the camera
        pass
    proj = cam.project(M.corner_world)
    rms = float(np.sqrt(np.mean(res ** 2)))
    return cam, proj, rms


def fit_cell_3d(img, det, cam_seed, ignore, masks, accept=0.42):
    """Fit the real 3D cell to the rib pixels inside a detection (the method used for the labels).
    Fast path: refine from the network's pose; slow path: pose search inside the box."""
    import fit_label as fl
    H, W = img.shape[:2]
    color = NAMES[det["cls"]]
    if color not in masks:
        masks[color] = fl.color_mask(img, color)
    mask = masks[color]
    best = None
    if cam_seed is not None:
        cam, _, st = fl.refine(mask, ignore, cam_seed, (W, H))
        if cam is not None and fl.plausible(cam, W, H):
            best = (cam, st.get("iou_roi", 0.0), "3d-fit (refined)")
    if best is None or best[1] < 0.62:  # the fit has nearby wrong optima: also search unless clearly right
        res = fl.auto_cell(mask, ignore, {"box": det["box"], "color": color}, (W, H), 4000, 3)
        if res is not None and fl.plausible(res[0], W, H) and (best is None or res[2].get("iou_roi", 0) > best[1]):
            best = (res[0], res[2].get("iou_roi", 0.0), "3d-fit (searched)")
    if best is None or best[1] < accept:
        return None
    cam = best[0]
    if cam.center[2] < 0:
        cam = fl.flip_camera(cam)
    return cam, best[1], best[2]


def detect_image(model, img, conf=0.3, refine=False, fit=False):
    """refine=True (or fit=True): fit the 3D cell to the red/blue pixels for exact corners."""
    H, W = img.shape[:2]
    dets = model(img, conf=conf)
    cells = []
    fit = fit or refine
    masks, ignore = {}, np.zeros((H, W), np.uint8)
    dets = sorted(dets, key=lambda d: -(d["box"][2] - d["box"][0]) * (d["box"][3] - d["box"][1]))
    for d in dets:
        cam, proj, rms = geometric_fit(d["kpts"], (W, H))
        k = d["kpts"]
        span = float(np.linalg.norm(k[:, :2].max(0) - k[:, :2].min(0)))
        if cam is not None and cam.center[2] < 0:
            # The network called the far frame "front". The cell is symmetric, so the same corners fit a
            # camera behind it; relabel (front<->back, left<->right) when the fit is trustworthy.
            used = (k[:, 2] >= 0.35)
            both = used[:5].sum() >= 2 and used[5:].sum() >= 2
            if rms is not None and rms <= max(4.0, 0.05 * span) and both:
                R = hk.rodrigues(cam.rvec) @ np.diag([-1.0, 1.0, -1.0])
                cam = hk.Camera(cv2.Rodrigues(R)[0].reshape(3), cam.tvec, cam.f, cam.cx, cam.cy)
                k = k[ROT180]
                proj = cam.project(M.corner_world)
        method, fit_iou = "network", None
        if fit:
            res = fit_cell_3d(img, d, cam, ignore, masks)
            if res is not None:
                cam, fit_iou, method = res
                proj = cam.project(M.corner_world)
                k = np.column_stack([proj, np.ones(10)])  # every corner from the fitted cell
                import fit_label as fl
                sil = fl.fitted_silhouette_full(cam, (W, H))
                ignore = np.maximum(ignore, cv2.dilate(sil, np.ones((9, 9), np.uint8)))
        # final corners: geometric where the network is unsure, network where it is confident and agrees
        corners = {}
        for i, cid in enumerate(hk.CORNER_IDS):
            x, y, c = k[i]
            if cam is not None and (c < 0.5 or np.hypot(x - proj[i, 0], y - proj[i, 1]) > max(6, 0.04 * span)):
                x, y = proj[i]
            corners[cid] = [round(float(x), 1), round(float(y), 1), round(float(c), 3)]
        xy = np.array([corners[c][:2] for c in hk.CORNER_IDS])
        pts28 = hk.points28_from_corners(xy, cam)
        lines = [{"id": i + 1, "name": M.line_names[i], "a": [round(float(v), 1) for v in pts28[2 * i]],
                  "b": [round(float(v), 1) for v in pts28[2 * i + 1]]} for i in range(14)]
        cell = {"color": NAMES[d["cls"]], "confidence": round(d["conf"], 3), "box": [round(v, 1) for v in d["box"]],
                "corners": corners, "lines": lines, "fit_rms_px": None if rms is None else round(rms, 2),
                "method": method, "fit_iou": None if fit_iou is None else round(fit_iou, 3)}
        if cam is not None:
            cell["camera"] = cam.to_dict()
            cell["distance_in"] = round(float(np.linalg.norm(cam.camera_points(np.array([[0, 7.0, 0]]))[0])), 1)
        cells.append(cell)
    # side / level like main.py (relative to the other cells, or to the image centre)
    centers = [np.mean([c["corners"][k][:2] for k in hk.CORNER_IDS], axis=0) for c in cells]
    ref = np.mean(centers, axis=0) if len(cells) > 1 else np.array([W / 2, H / 2])
    for c, ctr in zip(cells, centers):
        c["side"] = "right" if ctr[0] > ref[0] else "left"
        c["level"] = "top" if ctr[1] < ref[1] else "bottom"
    return cells


def to_record(cells, W, H, image_path):
    cells = sorted(cells, key=lambda c: -c["confidence"])[:hk.MAX_HIVES]
    cells = sorted(cells, key=lambda c: np.mean([c["corners"][k][0] for k in hk.CORNER_IDS]))
    values = np.zeros(hk.OUTPUT_SIZE, np.float32)
    for slot, c in enumerate(cells):
        off = slot * hk.VALUES_PER_HIVE
        values[off] = 1
        values[off + 1:off + 4] = [c["color"] == "blue", c["side"] == "right", c["level"] == "top"]
        pts = np.array([p for line in c["lines"] for p in (line["a"], line["b"])], np.float32)
        pts[:, 0] = np.clip(pts[:, 0], 0, W - 1) / W
        pts[:, 1] = np.clip(pts[:, 1], 0, H - 1) / H
        values[off + 4:off + hk.VALUES_PER_HIVE] = pts.reshape(-1)
    return {"image_path": str(image_path), "label": [round(float(v), 6) for v in values], "hive_count": len(cells),
            "calibration": None, "line_names": M.line_names, "model_version": 2, "annotator": "biobuzz-detector"}


LINE_COLORS = [(0, 220, 0), (200, 0, 200), (0, 200, 255)]


def draw(img, cells):
    out = img.copy()
    t = max(2, int(round(max(img.shape[:2]) / 700)))
    for c in cells:
        for line in c["lines"]:
            group = 0 if line["id"] in (1, 3, 5, 7, 9) else (1 if line["id"] <= 10 else 2)
            a, b = tuple(int(v) for v in line["a"]), tuple(int(v) for v in line["b"])
            cv2.line(out, a, b, LINE_COLORS[group], t, cv2.LINE_AA)
            mid = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)
            cv2.putText(out, f"{line['id']:02d}", mid, cv2.FONT_HERSHEY_SIMPLEX, 0.45 * t, (255, 255, 255), t + 1, cv2.LINE_AA)
            cv2.putText(out, f"{line['id']:02d}", mid, cv2.FONT_HERSHEY_SIMPLEX, 0.45 * t, (0, 0, 0), max(1, t - 1), cv2.LINE_AA)
        x0, y0 = int(c["box"][0]), int(c["box"][1])
        label = f"{c['color']} {c['confidence']:.2f} {c['side']}/{c['level']}"
        if c.get("distance_in"):
            label += f" ~{c['distance_in'] * 2.54 / 100:.1f} m"
        cv2.putText(out, label, (x0, max(15, y0 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55 * t, (0, 0, 0), 3 * t, cv2.LINE_AA)
        cv2.putText(out, label, (x0, max(15, y0 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55 * t, (0, 255, 255), t, cv2.LINE_AA)
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--out", default=str(ROOT / "runs" / "detect"))
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--imgsz", type=int, default=None)
    ap.add_argument("--refine", "--fit", dest="refine", action="store_true",
                    help="fit the real 3D cell to the red/blue pixels of each detection (slower, exact corners)")
    args = ap.parse_args()
    model = load_model(args.model, args.imgsz)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for p in args.images:
        p = Path(p)
        paths += sorted(q for q in p.iterdir() if q.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")) if p.is_dir() else [p]
    for p in paths:
        img = load_image(p)
        cells = detect_image(model, img, args.conf, args.refine)
        H, W = img.shape[:2]
        result = {"image": str(p), "width": W, "height": H, "cells": cells, "record": to_record(cells, W, H, p)}
        (out_dir / f"{p.stem}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        cv2.imwrite(str(out_dir / f"{p.stem}.jpg"), draw(img, cells), [cv2.IMWRITE_JPEG_QUALITY, 90])
        print(f"{p.name}: {len(cells)} cell(s) " + ", ".join(f"{c['color']} {c['confidence']:.2f} {c['method']}"
                                                               + (f" iou={c['fit_iou']}" if c['fit_iou'] else "") for c in cells))


if __name__ == "__main__":
    main()
