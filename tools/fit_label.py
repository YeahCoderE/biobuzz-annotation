#!/usr/bin/env python
"""Accurate labels for real photos: rough corner guesses -> 3D cell fitted to the rib pixels.

    .venv/Scripts/python -I tools/fit_label.py grid IMG [IMG ...] --out sheet.jpg
        image(s) with a labelled pixel grid, to read rough corner positions from
    .venv/Scripts/python -I tools/fit_label.py fit LABEL.json [...] [--sheet out.jpg]
        refine every cell of the label file(s) and write an overlay sheet for checking

Label file (dataset/labels/<stem>.json):
    {"image": "docs/images/x.jpg", "width": W, "height": H, "cells": [
        {"color": "red", "rough": {"F.BL": [x, y], ...}}  ...]}
After fitting each cell also gets "camera", "corners" {id: [x, y, state]} and "fit" stats.
State is "v" (inside the image) or "o" (outside the image, extrapolated by the 3D fit).

How the fit works: the camera (rotation, position, focal length) is first solved from the
rough clicks (PnP), then refined so that the rendered silhouette of both Goal Ribs matches
the red/blue pixels of the photo (symmetric truncated chamfer distance), with a weak prior
that keeps the corners near the clicks. Cells are fitted nearest first and the pixels a
fitted cell explains are ignored for the cells behind it.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hivekit as hk  # noqa: E402
import render as rd  # noqa: E402

ROOT = HERE.parent
M = hk.MODEL


def load_image(path):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        rgb = np.array(ImageOps.exif_transpose(im).convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def color_mask(img, color):
    """Rib pixels: strict colour seeds plus fainter pixels of the same colour that touch them
    (dim light, or the far frame seen through the plastic skins)."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0].astype(np.int16), hsv[..., 1], hsv[..., 2]
    if color == "red":
        core = ((h <= 6) | (h >= 167)) & (s >= 95) & (v >= 40)
        weak = ((h <= 10) | (h >= 158)) & (s >= 50) & (v >= 22)
    else:
        core = (h >= 98) & (h <= 126) & (s >= 90) & (v >= 35)
        weak = (h >= 92) & (h <= 132) & (s >= 50) & (v >= 22)
    core = cv2.morphologyEx(core.astype(np.uint8) * 255, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    near = cv2.dilate(core, np.ones((5, 5), np.uint8), iterations=4) > 0
    m = ((core > 0) | (weak & near)).astype(np.uint8) * 255
    return cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))


# ---------------------------------------------------------------------------
# Silhouette of the two ribs for a camera
# ---------------------------------------------------------------------------

_OUT_NO_TAB = np.concatenate(rd._outer_arcs)
_TAB_PRISM = rd._tab


def rib_mask(cam, size, scale=1.0, offset=(0.0, 0.0)):
    """Binary silhouette of both Goal Ribs (uint8) in a (w, h) canvas: image coords * scale - offset."""
    w, h = size
    mask = np.zeros((h, w), np.uint8)
    hole_a = np.zeros_like(mask)
    hole_b = np.zeros_like(mask)
    off = np.asarray(offset, float)
    for zc in (M.z_front, -M.z_front):
        z1, z2 = zc - rd.RIBD / 2, zc + rd.RIBD / 2
        pts = []
        for z in (z1, z2):
            pts.append(np.column_stack([_OUT_NO_TAB, np.full(len(_OUT_NO_TAB), z)]))
        P = cam.camera_points(np.vstack(pts))
        if np.any(P[:, 2] < 1):
            return None
        p2 = cam.project(np.vstack(pts)) * scale - off
        hull = cv2.convexHull(p2.astype(np.float32))
        cv2.fillConvexPoly(mask, np.round(hull.reshape(-1, 2) * 8).astype(np.int32), 255, cv2.LINE_8, 3)
        tab = cam.project(np.vstack([np.column_stack([_TAB_PRISM, np.full(4, z)]) for z in (z1, z2)])) * scale - off
        cv2.fillConvexPoly(mask, np.round(cv2.convexHull(tab.astype(np.float32)).reshape(-1, 2) * 8).astype(np.int32),
                           255, cv2.LINE_8, 3)
        hole_a[:] = 0
        hole_b[:] = 0
        ia = cam.project(np.column_stack([rd.INNER_C, np.full(len(rd.INNER_C), z1)])) * scale - off
        ib = cam.project(np.column_stack([rd.INNER_C, np.full(len(rd.INNER_C), z2)])) * scale - off
        cv2.fillConvexPoly(hole_a, np.round(ia * 8).astype(np.int32), 255, cv2.LINE_8, 3)
        cv2.fillConvexPoly(hole_b, np.round(ib * 8).astype(np.int32), 255, cv2.LINE_8, 3)
        frame = np.zeros_like(mask)
        cv2.fillConvexPoly(frame, np.round(hull.reshape(-1, 2) * 8).astype(np.int32), 255, cv2.LINE_8, 3)
        cv2.fillConvexPoly(frame, np.round(cv2.convexHull(tab.astype(np.float32)).reshape(-1, 2) * 8).astype(np.int32),
                           255, cv2.LINE_8, 3)
        frame[(hole_a > 0) & (hole_b > 0)] = 0
        mask = np.maximum(mask * 0, mask)  # keep dtype
        if zc == M.z_front:
            union = frame
        else:
            union = np.maximum(union, frame)
    return union


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------

def params_to_cam(x, base, size):
    R0 = hk.rodrigues(base.rvec)
    dR = hk.rodrigues(x[0:3] * 0.05)
    R = dR @ R0
    rvec = cv2.Rodrigues(R)[0].reshape(3)
    dist = float(np.linalg.norm(base.tvec))
    tvec = base.tvec + x[3:6] * dist * 0.02
    f = base.f * math.exp(x[6] * 0.05)
    return hk.Camera(rvec, tvec, f, size[0] / 2, size[1] / 2)


def fit_cell(img_mask, ignore, cell, size, rough_sigma):
    """Refine one cell. Returns (camera, stats)."""
    rough = {k: v for k, v in cell["rough"].items() if v is not None}
    fit0 = hk.fit_cell(rough, size)
    if not fit0["ok"]:
        return None, {"error": fit0.get("reason", "nofit"), "n": len(rough)}
    cam0 = fit0["camera"]
    # region of interest around the cell (scaled so the cell is ~360 px across)
    corners0 = cam0.project(M.corner_world)
    span = float(np.linalg.norm(corners0.max(0) - corners0.min(0)))
    scale = min(1.0, 360.0 / max(span, 1.0))
    pad = 0.35 * span
    x0, y0 = np.maximum(corners0.min(0) - pad, 0)
    x1, y1 = np.minimum(corners0.max(0) + pad, [size[0] - 1, size[1] - 1])
    off = np.array([x0, y0]) * scale
    w, h = int((x1 - x0) * scale) + 1, int((y1 - y0) * scale) + 1
    obs = cv2.resize(img_mask[int(y0):int(y1) + 1, int(x0):int(x1) + 1], (w, h), interpolation=cv2.INTER_AREA) > 96
    ign = cv2.resize(ignore[int(y0):int(y1) + 1, int(x0):int(x1) + 1], (w, h), interpolation=cv2.INTER_NEAREST) > 0
    obs &= ~ign
    dt_obs = cv2.distanceTransform((~obs).astype(np.uint8), cv2.DIST_L2, 3)
    T = max(4.0, 0.04 * 360)
    obs_idx = np.flatnonzero(obs)
    ids = list(rough)
    rough_xy = np.array([rough[k] for k in ids], float)
    rough_world = np.array([M.corners[k] for k in ids])
    sig = rough_sigma * span

    def cost(x):
        cam = params_to_cam(x, cam0, size)
        m = rib_mask(cam, (w, h), scale, off)
        if m is None:
            return 1e6
        model = m > 0
        model &= ~ign
        if model.sum() < 30:
            return 1e6
        c1 = np.minimum(dt_obs[model], T).mean()
        dt_model = cv2.distanceTransform((~model).astype(np.uint8), cv2.DIST_L2, 3)
        c2 = np.minimum(dt_model.flat[obs_idx], T).mean() if len(obs_idx) else 0.0
        prior = np.mean(np.sum((cam.project(rough_world) - rough_xy) ** 2, 1)) / (sig * sig)
        return c1 + c2 + 0.5 * prior

    best = None
    for k, start in enumerate([np.zeros(7)] + [np.random.default_rng(k).normal(0, 0.6, 7) for k in range(2)]):
        res = minimize(cost, start, method="Powell", options={"maxiter": 4000, "xtol": 1e-3, "ftol": 1e-4})
        if best is None or res.fun < best.fun:
            best = res
    cam = params_to_cam(best.x, cam0, size)
    m = rib_mask(cam, (w, h), scale, off)
    model = (m > 0) & ~ign
    inter = (model & obs).sum()
    union = (model | obs).sum()
    proj = cam.project(rough_world)
    click_err = np.linalg.norm(proj - rough_xy, axis=1)
    stats = {"cost": round(float(best.fun), 3), "iou_roi": round(float(inter / max(union, 1)), 3),
             "click_rms_px": round(float(np.sqrt(np.mean(click_err ** 2))), 1), "span_px": round(span, 1),
             "f": round(cam.f, 1), "n_clicks": len(ids), "scale": round(scale, 3)}
    return cam, stats


def fitted_silhouette_full(cam, size):
    m = rib_mask(cam, size, 1.0, (0.0, 0.0))
    return m if m is not None else np.zeros((size[1], size[0]), np.uint8)


def overlay(img, label, max_w=1100):
    out = img.copy()
    for cell in label["cells"]:
        if "camera" not in cell:
            for k, (x, y) in (cell.get("rough") or {}).items():
                cv2.circle(out, (int(x), int(y)), 9, (0, 255, 255), 2)
            continue
        cam = hk.Camera.from_dict(cell["camera"])
        sil = fitted_silhouette_full(cam, (label["width"], label["height"]))
        cnts, _ = cv2.findContours(sil, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, cnts, -1, (255, 255, 0), 2, cv2.LINE_AA)
        xy = np.array([cell["corners"][c][:2] for c in hk.CORNER_IDS])
        for a, b in [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0)]:
            cv2.line(out, tuple(np.int32(xy[a])), tuple(np.int32(xy[b])), (0, 255, 0), 2, cv2.LINE_AA)
            cv2.line(out, tuple(np.int32(xy[a + 5])), tuple(np.int32(xy[b + 5])), (255, 0, 255), 2, cv2.LINE_AA)
        for a in range(5):
            cv2.line(out, tuple(np.int32(xy[a])), tuple(np.int32(xy[a + 5])), (0, 200, 255), 1, cv2.LINE_AA)
        for i, c in enumerate(hk.CORNER_IDS):
            cv2.circle(out, tuple(np.int32(xy[i])), 6, (0, 255, 0) if i < 5 else (255, 0, 255), -1)
            cv2.putText(out, c, tuple(np.int32(xy[i]) + [6, -6]), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        for k, (x, y) in (cell.get("rough") or {}).items():
            cv2.drawMarker(out, (int(x), int(y)), (0, 255, 255), cv2.MARKER_CROSS, 18, 2)
        st = cell.get("fit", {})
        org = tuple(np.int32(xy.min(0)) + [0, -14])
        cv2.putText(out, f"iou {st.get('iou_roi')} click {st.get('click_rms_px')}", org, cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (0, 255, 255), 2)
    s = max_w / out.shape[1]
    return cv2.resize(out, (max_w, int(out.shape[0] * s)), interpolation=cv2.INTER_AREA)


def cmd_fit(args):
    tiles = []
    for path in args.labels:
        path = Path(path)
        label = hk.load_label(path)
        img = load_image(ROOT / label["image"])
        size = (img.shape[1], img.shape[0])
        label["width"], label["height"] = size
        ignore = np.zeros((size[1], size[0]), np.uint8)
        masks = {c: color_mask(img, c) for c in {cell["color"] for cell in label["cells"]}}
        # nearest (largest) first
        order = sorted(range(len(label["cells"])), key=lambda i: -spread(label["cells"][i]["rough"]))
        for i in order:
            cell = label["cells"][i]
            cam, stats = fit_cell(masks[cell["color"]], ignore, cell, size, args.rough_sigma)
            cell["fit"] = stats
            if cam is None:
                cell.pop("camera", None)
                cell.pop("corners", None)
                print(path.name, i, "FAILED", stats)
                continue
            xy = cam.project(M.corner_world)
            cell["camera"] = cam.to_dict()
            cell["corners"] = {c: [round(float(x), 1), round(float(y), 1),
                                   "v" if 0 <= x < size[0] and 0 <= y < size[1] else "o"]
                               for c, (x, y) in zip(hk.CORNER_IDS, xy)}
            sil = fitted_silhouette_full(cam, size)
            ignore = np.maximum(ignore, cv2.dilate(sil, np.ones((7, 7), np.uint8)))
            print(path.name, i, stats)
        hk.save_label(path, label)
        if args.sheet:
            tiles.append(overlay(img, label, args.tile))
    if args.sheet and tiles:
        sheet = make_sheet(tiles, args.cols)
        cv2.imwrite(args.sheet, sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
        print("sheet", args.sheet)


def spread(rough):
    pts = np.array([v for v in rough.values() if v is not None], float)
    return float(np.linalg.norm(pts.max(0) - pts.min(0))) if len(pts) > 1 else 0.0


def make_sheet(tiles, cols):
    w = max(t.shape[1] for t in tiles)
    rows = []
    for i in range(0, len(tiles), cols):
        row = tiles[i:i + cols]
        h = max(t.shape[0] for t in row)
        padded = [cv2.copyMakeBorder(t, 0, h - t.shape[0], 0, w - t.shape[1], cv2.BORDER_CONSTANT) for t in row]
        padded += [np.zeros((h, w, 3), np.uint8)] * (cols - len(row))
        rows.append(np.hstack(padded))
    return np.vstack(rows)


def cmd_grid(args):
    tiles = []
    for p in args.images:
        img = load_image(p)
        h, w = img.shape[:2]
        step = 100 if max(w, h) <= 2400 else 200
        out = img.copy()
        for x in range(0, w, step):
            cv2.line(out, (x, 0), (x, h), (0, 255, 255) if x % 500 == 0 else (255, 255, 255), 1)
            cv2.putText(out, str(x), (x + 3, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 4)
            cv2.putText(out, str(x), (x + 3, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
        for y in range(0, h, step):
            cv2.line(out, (0, y), (w, y), (0, 255, 255) if y % 500 == 0 else (255, 255, 255), 1)
            cv2.putText(out, str(y), (3, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 4)
            cv2.putText(out, str(y), (3, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
        cv2.putText(out, f"{Path(p).name} {w}x{h}", (10, h - 15), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 5)
        cv2.putText(out, f"{Path(p).name} {w}x{h}", (10, h - 15), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 2)
        s = args.tile / w
        tiles.append(cv2.resize(out, (args.tile, int(h * s)), interpolation=cv2.INTER_AREA))
    cv2.imwrite(args.out, make_sheet(tiles, args.cols), [cv2.IMWRITE_JPEG_QUALITY, 88])
    print("grid", args.out)


# ---------------------------------------------------------------------------
# Automatic pose search from a rough box (no corner clicks needed)
# ---------------------------------------------------------------------------

def _rotation_samples(n, seed=7):
    cache = ROOT / "dataset" / f"rot_samples_{n}.npy"
    if cache.exists():
        return np.load(cache)
    rng = np.random.default_rng(seed)
    q = rng.normal(size=(n, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    w, x, y, z = q.T
    R = np.stack([np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
                  np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
                  np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1)], 1)
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.save(cache, R.astype(np.float32))
    return R.astype(np.float32)


def _outline_sets():
    sets = []
    for zc in (M.z_front, -M.z_front):
        z1, z2 = zc - rd.RIBD / 2, zc + rd.RIBD / 2
        outer = np.vstack([np.column_stack([_OUT_NO_TAB[::2], np.full(len(_OUT_NO_TAB[::2]), z)]) for z in (z1, z2)])
        tab = np.vstack([np.column_stack([_TAB_PRISM, np.full(4, z)]) for z in (z1, z2)])
        ia = np.column_stack([rd.INNER_C, np.full(len(rd.INNER_C), z1)])
        ib = np.column_stack([rd.INNER_C, np.full(len(rd.INNER_C), z2)])
        sets.append((outer, tab, ia, ib))
    return sets


OUTLINES = _outline_sets()
ALL_OUTLINE = np.vstack([np.vstack(s_) for s_ in OUTLINES])


def _raster_weak(parts, size):
    w, h = size
    out = np.zeros((h, w), np.uint8)
    a = np.zeros_like(out)
    b = np.zeros_like(out)
    for outer, tab, ia, ib in parts:
        frame = np.zeros_like(out)
        cv2.fillConvexPoly(frame, cv2.convexHull(np.round(outer * 4).astype(np.int32)), 1, cv2.LINE_8, 2)
        cv2.fillConvexPoly(frame, cv2.convexHull(np.round(tab * 4).astype(np.int32)), 1, cv2.LINE_8, 2)
        a[:] = 0
        b[:] = 0
        cv2.fillConvexPoly(a, cv2.convexHull(np.round(ia * 4).astype(np.int32)), 1, cv2.LINE_8, 2)
        cv2.fillConvexPoly(b, cv2.convexHull(np.round(ib * 4).astype(np.int32)), 1, cv2.LINE_8, 2)
        frame[(a > 0) & (b > 0)] = 0
        out |= frame
    return out


CELL_CENTER = np.array([0.0, 7.0, 0.0])
DIST_RATIOS = (1.15, 1.7, 2.8, 9.0)  # camera distance / 21 inch


def coarse_search(obs, n_samples=8000, top=6, dists=DIST_RATIOS):
    """Perspective-aware search over rotations and camera distances on a small binary mask.

    Returns [(score, R_model, Z, sc, shift)] best first. A candidate maps the normalised
    projection u = Xc_x / Xc_z, v = -Xc_y / Xc_z (camera looking down -z of the rotated model
    at distance Z from the cell centre) to ROI pixels by  pixel = (u, v) * sc + shift.
    """
    h, w = obs.shape
    ys, xs = np.nonzero(obs)
    if len(xs) < 20:
        return []
    bx0, bx1 = np.percentile(xs, [1, 99])
    by0, by1 = np.percentile(ys, [1, 99])
    bw, bh = bx1 - bx0 + 1, by1 - by0 + 1
    obs_soft = cv2.GaussianBlur(obs.astype(np.float32), (0, 0), 1.2)
    obs_sum = float(obs_soft.sum())
    Rs = _rotation_samples(n_samples)
    X = (ALL_OUTLINE - CELL_CENTER).astype(np.float32)
    rot = np.einsum("sij,nj->sni", Rs, X)  # (S, N, 3), model frame rotated; camera looks along -z
    lens = [len(x) for s_ in OUTLINES for x in s_]
    results = []
    for d in dists:
        Z = d * 21.0
        depth = Z - rot[..., 2]  # camera at +Z on the z axis looking towards -z
        ok = np.all(depth > 2.0, axis=1)
        proj = np.stack([rot[..., 0] / np.maximum(depth, 1e-3), -rot[..., 1] / np.maximum(depth, 1e-3)], -1)
        for k in np.flatnonzero(ok):
            P = proj[k]
            mn, mx = P.min(0), P.max(0)
            ext = np.maximum(mx - mn, 1e-9)
            sc = 0.5 * (bw / ext[0] + bh / ext[1])
            shift = np.array([(bx0 + bx1) / 2, (by0 + by1) / 2]) - (mn + mx) / 2 * sc
            Q = P * sc + shift
            parts, i, j = [], 0, 0
            for _ in OUTLINES:
                group = []
                for _ in range(4):
                    n = lens[j]
                    group.append(Q[i:i + n])
                    i += n
                    j += 1
                parts.append(group)
            m = cv2.GaussianBlur(_raster_weak(parts, (w, h)).astype(np.float32), (0, 0), 1.2)
            inter = float((m * obs_soft).sum())
            results.append((inter / (float(m.sum()) + obs_sum - inter + 1e-6), int(k), Z, sc, shift))
    results.sort(key=lambda r: -r[0])
    flip = np.diag([-1.0, 1.0, -1.0])
    chosen = []
    for score, k, Z, sc, shift in results:
        R = Rs[k].astype(float)
        if any(min(np.linalg.norm(R - Rc), np.linalg.norm(R @ flip - Rc)) < 0.5 and abs(np.log(Z / Zc)) < 0.4
               for _, Rc, Zc, _, _ in chosen):
            continue
        chosen.append((score, R, Z, sc, shift))
        if len(chosen) >= top:
            break
    return chosen


def camera_from_candidate(R_model, Z, sc, shift, roi_scale, roi_off, size, f_hint):
    """Full perspective camera from a coarse candidate (keeps the image centre as principal point)."""
    f = sc / roi_scale  # pixels per unit of normalised projection
    if not (0.25 * max(size) < f < 4.0 * max(size)):
        f = f_hint
    c_img = shift / roi_scale + np.asarray(roi_off, float)  # where the cell centre lands
    cx, cy = size[0] / 2, size[1] / 2
    ray = np.array([(c_img[0] - cx) / f, (c_img[1] - cy) / f, 1.0])
    ray /= np.linalg.norm(ray)
    # rotation taking the optical axis (0,0,1) onto the ray
    zax = np.array([0.0, 0.0, 1.0])
    v = np.cross(zax, ray)
    sn, cs = np.linalg.norm(v), float(np.dot(zax, ray))
    R_off = np.eye(3) if sn < 1e-9 else hk.rodrigues(v / sn * np.arctan2(sn, cs))
    flipy = np.diag([1.0, -1.0, -1.0])
    Rc = R_off @ flipy @ R_model
    t = ray * Z - Rc @ CELL_CENTER
    return hk.Camera(cv2.Rodrigues(Rc)[0].reshape(3), t, f, cx, cy)


def refine(img_mask, ignore, cam0, size, prior=None, prior_sigma=0.08):
    corners0 = cam0.project(M.corner_world)
    span = float(np.linalg.norm(corners0.max(0) - corners0.min(0)))
    scale = min(1.0, 360.0 / max(span, 1.0))
    pad = 0.35 * span
    x0, y0 = np.maximum(corners0.min(0) - pad, 0)
    x1, y1 = np.minimum(corners0.max(0) + pad, [size[0] - 1, size[1] - 1])
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None, 1e9, {}
    off = np.array([x0, y0]) * scale
    w, h = int((x1 - x0) * scale) + 1, int((y1 - y0) * scale) + 1
    obs = cv2.resize(img_mask[int(y0):int(y1) + 1, int(x0):int(x1) + 1], (w, h), interpolation=cv2.INTER_AREA) > 96
    ign = cv2.resize(ignore[int(y0):int(y1) + 1, int(x0):int(x1) + 1], (w, h), interpolation=cv2.INTER_NEAREST) > 0
    obs &= ~ign
    dt_obs = cv2.distanceTransform((~obs).astype(np.uint8), cv2.DIST_L2, 3)
    T = 14.0
    obs_idx = np.flatnonzero(obs)
    prior = prior or None
    pids = list(prior) if prior else []
    pw = np.array([M.corners[k] for k in pids]) if pids else None
    pxy = np.array([prior[k] for k in pids], float) if pids else None

    def cost(x, base):
        cam = params_to_cam(x, base, size)
        m = rib_mask(cam, (w, h), scale, off)
        if m is None:
            return 1e6
        model = (m > 0) & ~ign
        if model.sum() < 30:
            return 1e6
        c1 = np.minimum(dt_obs[model], T).mean()
        dt_model = cv2.distanceTransform((~model).astype(np.uint8), cv2.DIST_L2, 3)
        c2 = np.minimum(dt_model.flat[obs_idx], T).mean() if len(obs_idx) else 0.0
        pr = 0.0
        if pids:
            pr = 0.5 * np.mean(np.sum((cam.project(pw) - pxy) ** 2, 1)) / (prior_sigma * span) ** 2
        return c1 + c2 + pr

    cam = cam0
    fun = None
    for _ in range(2):
        res = minimize(cost, np.zeros(7), args=(cam,), method="Powell",
                       options={"maxiter": 3000, "xtol": 1e-3, "ftol": 1e-4})
        cam, fun = params_to_cam(res.x, cam, size), float(res.fun)
    m = rib_mask(cam, (w, h), scale, off)
    model = (m > 0) & ~ign if m is not None else np.zeros_like(obs)
    iou = float((model & obs).sum() / max((model | obs).sum(), 1))
    return cam, fun, {"iou_roi": round(iou, 3), "cost": round(fun, 3), "span_px": round(span, 1), "f": round(cam.f, 1)}


def auto_cell(img_mask, ignore, cell, size, n_samples, top):
    x0, y0, x1, y1 = cell["box"]
    bw, bh = x1 - x0, y1 - y0
    X0, Y0 = max(0, int(x0 - 0.12 * bw)), max(0, int(y0 - 0.12 * bh))
    X1, Y1 = min(size[0] - 1, int(x1 + 0.12 * bw)), min(size[1] - 1, int(y1 + 0.12 * bh))
    roi_scale = 128.0 / max(X1 - X0, Y1 - Y0)
    w, h = int((X1 - X0) * roi_scale) + 1, int((Y1 - Y0) * roi_scale) + 1
    small = cv2.resize(img_mask[Y0:Y1 + 1, X0:X1 + 1], (w, h), interpolation=cv2.INTER_AREA) > 96
    small &= ~(cv2.resize(ignore[Y0:Y1 + 1, X0:X1 + 1], (w, h), interpolation=cv2.INTER_NEAREST) > 0)
    cands = coarse_search(small, n_samples, top)
    best = None
    f_hint = cell.get("f", 0.85 * max(size))
    for score, R, Z, sc, shift in cands:
        cam0 = camera_from_candidate(R, Z, sc, shift, roi_scale, np.array([X0, Y0], float), size, f_hint)
        cam, c, st = refine(img_mask, ignore, cam0, size, cell.get("rough"))
        if cam is None:
            continue
        st["coarse_iou"] = round(score, 3)
        if best is None or c < best[1]:
            best = (cam, c, st)
    return best


def plausible(cam, W, H):
    """Reject degenerate fits (e.g. a flat scoreboard graphic fitted as a cell)."""
    from build_yolo import OUTLINE
    if not (0.3 * max(W, H) < cam.f < 3.0 * max(W, H)):
        return False
    pc = cam.camera_points(OUTLINE)
    if pc[:, 2].min() < 0.2 * M.diagonal:
        return False
    p2 = cam.project(OUTLINE)
    span = float(np.linalg.norm(p2.max(0) - p2.min(0)))
    return span < 3.0 * float(np.hypot(W, H))


def flip_camera(cam):
    """Same view with the model turned 180 degrees about y, so the camera sits on the +z (front) side."""
    R = hk.rodrigues(cam.rvec) @ np.diag([-1.0, 1.0, -1.0])
    return hk.Camera(cv2.Rodrigues(R)[0].reshape(3), cam.tvec, cam.f, cam.cx, cam.cy)


def cmd_auto(args):
    tiles = []
    for path in args.labels:
        path = Path(path)
        label = hk.load_label(path)
        img = load_image(ROOT / label["image"])
        size = (img.shape[1], img.shape[0])
        label["width"], label["height"] = size
        ignore = np.zeros((size[1], size[0]), np.uint8)
        masks = {c: color_mask(img, c) for c in {cell["color"] for cell in label["cells"]}}

        def area(c):
            b = c["box"]
            return (b[2] - b[0]) * (b[3] - b[1])

        for i in sorted(range(len(label["cells"])), key=lambda i: -area(label["cells"][i])):
            cell = label["cells"][i]
            if cell.get("locked") and "camera" in cell:
                cam = hk.Camera.from_dict(cell["camera"])
            else:
                best = auto_cell(masks[cell["color"]], ignore, cell, size, args.samples, args.top)
                if best is None:
                    print(path.name, i, "FAILED", flush=True)
                    continue
                cam, _, st = best
                if cam.center[2] < 0:
                    cam = flip_camera(cam)
                xy = cam.project(M.corner_world)
                cell["camera"] = cam.to_dict()
                cell["corners"] = {k: [round(float(x), 1), round(float(y), 1),
                                       "v" if 0 <= x < size[0] and 0 <= y < size[1] else "o"]
                                   for k, (x, y) in zip(hk.CORNER_IDS, xy)}
                cell["fit"] = st
                print(path.name, i, st, flush=True)
            sil = fitted_silhouette_full(cam, size)
            ignore = np.maximum(ignore, cv2.dilate(sil, np.ones((9, 9), np.uint8)))
        hk.save_label(path, label)
        if args.sheet:
            tiles.append(overlay(img, label, args.tile))
    if args.sheet and tiles:
        cv2.imwrite(args.sheet, make_sheet(tiles, args.cols), [cv2.IMWRITE_JPEG_QUALITY, 85])
        print("sheet", args.sheet)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grid")
    g.add_argument("images", nargs="+")
    g.add_argument("--out", required=True)
    g.add_argument("--tile", type=int, default=1000)
    g.add_argument("--cols", type=int, default=2)
    g.set_defaults(func=cmd_grid)
    f = sub.add_parser("fit")
    f.add_argument("labels", nargs="+")
    f.add_argument("--sheet")
    f.add_argument("--tile", type=int, default=1000)
    f.add_argument("--cols", type=int, default=2)
    f.add_argument("--rough-sigma", type=float, default=0.06)
    f.set_defaults(func=cmd_fit)
    a = sub.add_parser("auto")
    a.add_argument("labels", nargs="+")
    a.add_argument("--sheet")
    a.add_argument("--tile", type=int, default=1000)
    a.add_argument("--cols", type=int, default=2)
    a.add_argument("--samples", type=int, default=8000)
    a.add_argument("--top", type=int, default=6)
    a.set_defaults(func=cmd_auto)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
