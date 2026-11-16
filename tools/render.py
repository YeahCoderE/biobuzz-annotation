#!/usr/bin/env python
"""Synthetic BIOBUZZ hive images with exact keypoint labels (fast software renderer).

    .venv/Scripts/python -I tools/render.py preview --n 12 --out preview.jpg [--seed 3]
    .venv/Scripts/python -I tools/render.py make --out dataset/yolo --split synth --count 50000 --workers 4 --seed 1

Geometry comes from docs/data/hive_model.json (through hivekit) plus details measured on
the official drawing (Competition Manual figure 9-11): 0.5" Goal Rib, rounded corners,
the bottom tab with its square hole, and the small holes along the rib.

Labels follow the site and main.py: the FRONT frame is the one closer to the camera,
left/right are seen from the front with the roof up. Output: YOLO-pose labels
(class cx cy w h + 10 corners x y v), class 0 = red cell, 1 = blue cell.
"""
import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hivekit as hk  # noqa: E402

M = hk.MODEL
DIM = M.dims
HALF = DIM["openingWidth"] / 2
EAVE = DIM["eaveHeight"]
PEAK = DIM["peakHeight"]
RIBW = DIM["ribWidth"]
RIBD = DIM["ribDepth"]
ZF = M.z_front
ZEND = DIM["cellDepth"] / 2
CHURRO_X = M.churro_x
ROOT = HERE.parent
BG_DIRS = [ROOT / "dataset" / "backgrounds_small"]
SS = 2  # supersampling factor


# ---------------------------------------------------------------------------
# Cell geometry (model frame, inches; x right, y up, z towards the front)
# ---------------------------------------------------------------------------

def rounded_arcs(poly, radii, seg):
    poly = np.asarray(poly, float)
    n = len(poly)
    arcs = []
    for i in range(n):
        p0, p1, p2 = poly[i - 1], poly[i], poly[(i + 1) % n]
        r = radii[i]
        d1 = (p0 - p1) / np.linalg.norm(p0 - p1)
        d2 = (p2 - p1) / np.linalg.norm(p2 - p1)
        ang = math.acos(float(np.clip(np.dot(d1, d2), -1, 1)))
        if r <= 1e-6:
            arcs.append(p1[None])
            continue
        t = r / math.tan(ang / 2)
        a, b = p1 + d1 * t, p1 + d2 * t
        bis = (d1 + d2) / np.linalg.norm(d1 + d2)
        c = p1 + bis * (r / math.sin(ang / 2))
        a0 = math.atan2(a[1] - c[1], a[0] - c[0])
        a1 = math.atan2(b[1] - c[1], b[0] - c[0])
        da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
        arcs.append(np.array([c + r * np.array([math.cos(a0 + da * k / seg), math.sin(a0 + da * k / seg)])
                              for k in range(seg + 1)]))
    return arcs


R_IN = [0.45, 0.45, 0.30, 0.55, 0.30]
INNER_C = np.concatenate(rounded_arcs(M.inner, R_IN, 4))
_outer_arcs = rounded_arcs(M.outer, [r + RIBW for r in R_IN], 5)
_tab = np.array([[-3.85, -RIBW], [-0.82, -1.6], [0.82, -1.6], [3.85, -RIBW]])
OUTER_C = np.concatenate([_outer_arcs[0], _tab] + _outer_arcs[1:])
SQUARE = np.array([[-0.49, -1.34], [0.49, -1.34], [0.49, -0.36], [-0.49, -0.36]])
_center_c = np.concatenate(rounded_arcs(M.center, [r + RIBW / 2 for r in R_IN], 4))


def _holes():
    pts, seglen = [], np.linalg.norm(np.diff(np.vstack([_center_c, _center_c[:1]]), axis=0), axis=1)
    total, step, acc, k = seglen.sum(), 3.05, 1.2, 0
    cum = np.concatenate([[0], np.cumsum(seglen)])
    loop = np.vstack([_center_c, _center_c[:1]])
    while acc < total:
        while cum[k + 1] < acc:
            k += 1
        u = (acc - cum[k]) / max(seglen[k], 1e-9)
        p = loop[k] * (1 - u) + loop[k + 1] * u
        if not (abs(p[0]) < 1.2 and p[1] < 0.2):
            pts.append(p)
        acc += step
    return np.array(pts)


HOLES = _holes()


def frame_arrays(zc):
    """Side bands of one Goal Rib (centre plane z = zc) as arrays: quads (N,4,3), normals (N,3)."""
    z1, z2 = zc - RIBD / 2, zc + RIBD / 2
    quads, normals = [], []
    for contour, sign in ((OUTER_C, 1.0), (INNER_C, -1.0)):
        nxt = np.roll(contour, -1, axis=0)
        d = nxt - contour
        length = np.linalg.norm(d, axis=1, keepdims=True)
        normal = np.stack([d[:, 1], -d[:, 0]], 1) / np.maximum(length, 1e-9) * sign
        for p, q, nrm in zip(contour, nxt, normal):
            quads.append([[p[0], p[1], z1], [q[0], q[1], z1], [q[0], q[1], z2], [p[0], p[1], z2]])
            normals.append([nrm[0], nrm[1], 0.0])
    quads = np.array(quads)
    return quads, np.array(normals), quads.mean(1), (z1, z2)


FRAME = {s: frame_arrays(s * ZF) for s in (1, -1)}


def skins(closed_end):
    z0, z1 = -ZEND + 0.15, ZEND - 0.15
    out = [
        (np.array([[-HALF, 0, z0], [HALF, 0, z0], [HALF, 0, z1], [-HALF, 0, z1]]), np.array([0, 1.0, 0])),
        (np.array([[-HALF, 0, z0], [-HALF, EAVE, z0], [-HALF, EAVE, z1], [-HALF, 0, z1]]), np.array([1.0, 0, 0])),
        (np.array([[HALF, 0, z0], [HALF, EAVE, z0], [HALF, EAVE, z1], [HALF, 0, z1]]), np.array([-1.0, 0, 0])),
        (np.array([[-HALF, EAVE, z0], [0, PEAK, z0], [0, PEAK, z1], [-HALF, EAVE, z1]]),
         np.array([PEAK - EAVE, -HALF, 0]) / math.hypot(PEAK - EAVE, HALF)),
        (np.array([[HALF, EAVE, z0], [0, PEAK, z0], [0, PEAK, z1], [HALF, EAVE, z1]]),
         np.array([-(PEAK - EAVE), -HALF, 0]) / math.hypot(PEAK - EAVE, HALF)),
    ]
    if closed_end:
        z = closed_end * (ZF - RIBD / 2 - 0.05)
        out.append((np.array([[x, y, z] for x, y in M.inner]), np.array([0, 0, -float(closed_end)])))
    return out


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def rot(axis, angle):
    axis = np.asarray(axis, float) / np.linalg.norm(axis)
    return hk.rodrigues(axis * angle)


def look_at(eye, target, roll):
    fwd = target - eye
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, [0.0, 1.0, 0.0])
    if np.linalg.norm(right) < 1e-6:
        right = np.array([1.0, 0, 0])
    right /= np.linalg.norm(right)
    down = np.cross(fwd, right)
    c, s = math.cos(roll), math.sin(roll)
    right, down = c * right + s * down, -s * right + c * down
    R = np.stack([right, down, fwd])
    return R, -R @ eye


class View:
    def __init__(self, R, t, f, cx, cy, width, height, eye):
        self.R, self.t, self.f, self.cx, self.cy = R, t, f, cx, cy
        self.w, self.h, self.eye = width, height, eye

    def cam(self, P):
        return np.asarray(P, float).reshape(-1, 3) @ self.R.T + self.t

    def proj(self, Pc):
        z = np.maximum(Pc[:, 2], 1e-6)
        return np.stack([self.f * Pc[:, 0] / z + self.cx, self.f * Pc[:, 1] / z + self.cy], 1)


def to_fixed(p2d):
    return np.round(p2d * 16).astype(np.int32)


def shade(base, normal, view_dir, light, amb, dif, spec, shin):
    ndl = float(np.dot(normal, light))
    h = light + view_dir
    h /= np.linalg.norm(h) + 1e-9
    s = max(0.0, float(np.dot(normal, h))) ** shin if ndl > 0 else 0.0
    col = base * (amb + dif * max(0.0, ndl)) + spec * s * 255.0
    return tuple(float(v) for v in np.clip(col, 0, 255))


# ---------------------------------------------------------------------------
# Scene objects
# ---------------------------------------------------------------------------

class Cell:
    def __init__(self, color, Rm, tm, closed_end, rng):
        self.color, self.Rm, self.tm, self.closed_end = color, Rm, tm, closed_end
        if color == "red":
            hsv = (rng.uniform(-6, 5) % 180, rng.uniform(170, 245), rng.uniform(150, 235))
        else:
            hsv = (rng.uniform(100, 118), rng.uniform(150, 245), rng.uniform(110, 230))
        rgb = cv2.cvtColor(np.uint8([[hsv]]), cv2.COLOR_HSV2BGR)[0, 0].astype(float)
        self.base = rgb
        self.skin_alpha = rng.uniform(0.05, 0.22)
        self.edge_gain = rng.uniform(0.55, 1.25)
        self.haze = rng.uniform(0.0, 0.12)
        self.skin_tint = np.array([rng.uniform(200, 255)] * 3) * np.array([1, rng.uniform(0.95, 1.0), rng.uniform(0.92, 1.0)])
        self.glare = int(rng.integers(0, 6))
        self.tie_color = rgb
        self.tags = rng.random() < 0.5

    def world(self, P):
        return np.asarray(P, float).reshape(-1, 3) @ self.Rm.T + self.tm

    def corners_world(self):
        return self.world(M.corner_world)


def draw_box(img, view, center, axes, half, base, light, lp):
    """Opaque box: center (3,), axes 3x3 (columns = local x,y,z in world), half extents (3,)."""
    faces = []
    for k in range(3):
        for sgn in (1, -1):
            n = axes[:, k] * sgn
            a, b = [j for j in range(3) if j != k]
            c = center + n * half[k]
            quad = np.array([c + axes[:, a] * half[a] * sa + axes[:, b] * half[b] * sb
                             for sa, sb in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
            faces.append((quad, n))
    items = []
    for quad, n in faces:
        vc = view.eye - quad.mean(0)
        if np.dot(n, vc) <= 0:
            continue
        pc = view.cam(quad)
        if np.any(pc[:, 2] < 1.0):
            continue
        items.append((pc[:, 2].mean(), quad, n, pc))
    items.sort(key=lambda it: -it[0])
    for _, quad, n, pc in items:
        vd = view.eye - quad.mean(0)
        vd /= np.linalg.norm(vd)
        col = shade(base, n, vd, light, *lp)
        cv2.fillConvexPoly(img, to_fixed(view.proj(pc)), col, cv2.LINE_AA, 4)


def shade_vec(base, N, V, L, amb, dif, spec, shin):
    ndl = N @ L
    H = L[None] + V
    H /= np.linalg.norm(H, axis=1, keepdims=True) + 1e-9
    s = np.where(ndl > 0, np.clip((N * H).sum(1), 0, None) ** shin, 0.0)
    col = base[None] * (amb + dif * np.clip(ndl, 0, None))[:, None] + spec * s[:, None] * 255.0
    return np.clip(col, 0, 255)


def draw_frame(img, view, cell, side, light, lp):
    """Opaque Goal Rib (side = +1 / -1 frame in the cell's own model frame)."""
    Q, Nm, C, (z1, z2) = FRAME[side]
    Rm, tm = cell.Rm, cell.tm
    eye_m = Rm.T @ (view.eye - tm)
    vis = np.einsum("ij,ij->i", Nm, eye_m[None] - C) > 0
    Qv, Nv, Cv = Q[vis], Nm[vis], C[vis]
    if len(Qv):
        Pc = view.cam(cell.world(Qv.reshape(-1, 3)))
        if np.any(Pc[:, 2] < 1.0):
            return
        P2 = view.proj(Pc).reshape(-1, 4, 2)
        depth = Pc[:, 2].reshape(-1, 4).mean(1)
        Cw = cell.world(Cv)
        V = view.eye[None] - Cw
        V /= np.linalg.norm(V, axis=1, keepdims=True)
        cols = shade_vec(cell.base, Nv @ Rm.T, V, light, *lp)
        for i in np.argsort(-depth):
            cv2.fillConvexPoly(img, to_fixed(P2[i]), tuple(float(v) for v in cols[i]), cv2.LINE_AA, 4)
    for z, nz in ((z1, -1.0), (z2, 1.0)):
        if (eye_m[2] - z) * nz <= 0:
            continue
        n_world = Rm @ np.array([0, 0, nz])
        outer = view.cam(cell.world(np.column_stack([OUTER_C, np.full(len(OUTER_C), z)])))
        inner = view.cam(cell.world(np.column_stack([INNER_C, np.full(len(INNER_C), z)])))
        square = view.cam(cell.world(np.column_stack([SQUARE, np.full(4, z)])))
        if min(outer[:, 2].min(), inner[:, 2].min()) < 1.0:
            return
        vd = view.eye - cell.world([0, EAVE / 2, z])[0]
        vd /= np.linalg.norm(vd)
        col = shade_vec(cell.base, n_world[None], vd[None], light, *lp)[0]
        col_t = tuple(float(v) for v in col)
        po, pi = view.proj(outer), view.proj(inner)
        cv2.fillPoly(img, [to_fixed(po), to_fixed(pi), to_fixed(view.proj(square))], col_t, cv2.LINE_AA, 4)
        edge = tuple(float(v) * cell.edge_gain for v in col)
        lw = max(1, int(round(view.f * 0.06 / max(outer[:, 2].mean(), 1))))
        cv2.polylines(img, [to_fixed(po), to_fixed(pi)], True, edge, lw, cv2.LINE_AA, 4)
        holes = view.cam(cell.world(np.column_stack([HOLES, np.full(len(HOLES), z)])))
        hp = view.proj(holes)
        rad = view.f * 0.1 / holes[:, 2]
        dark = tuple(float(v) * 0.35 for v in col)
        for (x, y), r in zip(hp, rad):
            if r > 0.6:
                cv2.circle(img, (int(x * 16), int(y * 16)), int(r * 16), dark, -1, cv2.LINE_AA, 4)


def draw_rod(img, view, p0, p1, width, base, light, lp, ties=None, tie_color=None):
    pc = view.cam(np.array([p0, p1]))
    if np.any(pc[:, 2] < 1.0):
        return
    a, b = view.proj(pc)
    zmid = pc[:, 2].mean()
    w = max(1.0, view.f * width / zmid)
    d = b - a
    L = np.linalg.norm(d)
    if L < 1:
        return
    nrm = np.array([-d[1], d[0]]) / L
    for k, (frac, gain) in enumerate(((1.0, 0.55), (0.6, 0.95), (0.22, 1.25))):
        col = tuple(float(v) for v in np.clip(base * gain, 0, 255))
        hw = w * frac / 2
        quad = np.array([a + nrm * hw, b + nrm * hw, b - nrm * hw, a - nrm * hw])
        if k == 2:
            quad = quad + nrm * w * 0.18
        cv2.fillConvexPoly(img, to_fixed(quad), col, cv2.LINE_AA, 4)
    if ties is not None:
        for u in ties:
            q = view.cam(np.array([p0 + (p1 - p0) * u]))
            if q[0, 2] < 1:
                continue
            c = view.proj(q)[0]
            tw = view.f * width * 0.65 / q[0, 2]
            dd = d / L
            seg = np.array([c - nrm * tw - dd * tw * 0.12, c + nrm * tw - dd * tw * 0.12,
                            c + nrm * tw + dd * tw * 0.12, c - nrm * tw + dd * tw * 0.12])
            cv2.fillConvexPoly(img, to_fixed(seg), tuple(float(v) for v in tie_color), cv2.LINE_AA, 4)


def blend_poly(img, poly2d, color, alpha):
    x0, y0 = np.floor(poly2d.min(0)).astype(int) - 1
    x1, y1 = np.ceil(poly2d.max(0)).astype(int) + 2
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.shape[1], x1), min(img.shape[0], y1)
    if x1 <= x0 or y1 <= y0 or alpha <= 0:
        return
    roi = img[y0:y1, x0:x1]
    over = roi.copy()
    cv2.fillConvexPoly(over, to_fixed(poly2d - [x0, y0]), tuple(float(c) for c in color), cv2.LINE_AA, 4)
    cv2.addWeighted(over, float(alpha), roi, 1.0 - float(alpha), 0, dst=roi)


def draw_skins(img, view, cell, light, rng):
    items = []
    for quad, n in skins(cell.closed_end):
        pw = cell.world(quad)
        pc = view.cam(pw)
        if np.any(pc[:, 2] < 1.0):
            continue
        nw = cell.Rm @ n
        vd = view.eye - pw.mean(0)
        vd /= np.linalg.norm(vd)
        cosv = abs(float(np.dot(nw, vd)))
        items.append((pc[:, 2].mean(), view.proj(pc), cosv, nw))
    items.sort(key=lambda it: -it[0])
    for _, p2, cosv, nw in items:
        fres = cell.skin_alpha + (1 - cell.skin_alpha) * (1 - cosv) ** 5 * 0.55
        lum = 0.75 + 0.35 * max(0.0, float(np.dot(nw, light)))
        blend_poly(img, p2, np.clip(cell.skin_tint * lum, 0, 255), min(0.6, fres))
        cv2.polylines(img, [to_fixed(p2)], True, tuple(float(v) for v in np.clip(cell.skin_tint * 0.95, 0, 255)),
                      max(1, int(view.f / 900)), cv2.LINE_AA, 4)
    for _ in range(cell.glare if items else 0):
        _, p2, _, _ = items[int(rng.integers(len(items)))]
        u0, u1 = sorted(rng.uniform(0, 1, 2))
        v0, v1 = sorted(rng.uniform(0, 1, 2))
        a, b, c, d = p2[0], p2[1], p2[2], p2[3]
        q = np.array([a + (b - a) * u0 + (d - a) * v0, a + (b - a) * u1 + (d - a) * v0,
                      a + (b - a) * u1 + (d - a) * v1, a + (b - a) * u0 + (d - a) * v1])
        if rng.random() < 0.5:
            center = q.mean(0)
            q = center + (q - center) * np.array([1.0, rng.uniform(0.08, 0.35)])
        blend_poly(img, q, (255, 255, 255), rng.uniform(0.2, 0.85))


def draw_apriltags(img, view, cell, rng):
    """White AprilTag plate under the cell floor (official cells), visible only from below."""
    eye_m = cell.Rm.T @ (view.eye - cell.tm)
    if eye_m[1] > -0.2 or not cell.tags:
        return
    y = -0.08
    plate = np.array([[-7.5, y, -2.6], [7.5, y, -2.6], [7.5, y, 2.6], [-7.5, y, 2.6]])
    pc = view.cam(cell.world(plate))
    if np.any(pc[:, 2] < 1):
        return
    cv2.fillConvexPoly(img, to_fixed(view.proj(pc)), (235.0, 235.0, 235.0), cv2.LINE_AA, 4)
    for cx in (-5.6, -2.1, 2.1, 5.6):
        x0, x1, z0, z1 = cx - 1.5, cx + 1.5, -1.5, 1.5
        tag = np.array([[x0, y - 0.01, z0], [x1, y - 0.01, z0], [x1, y - 0.01, z1], [x0, y - 0.01, z1]])
        cv2.fillConvexPoly(img, to_fixed(view.proj(view.cam(cell.world(tag)))), (20.0, 20.0, 20.0), cv2.LINE_AA, 4)
        for i in range(4):
            for j in range(4):
                if rng.random() < 0.45:
                    a0, a1 = x0 + 0.5 + i * 0.5, x0 + 1.0 + i * 0.5
                    b0, b1 = z0 + 0.5 + j * 0.5, z0 + 1.0 + j * 0.5
                    bit = np.array([[a0, y - 0.02, b0], [a1, y - 0.02, b0], [a1, y - 0.02, b1], [a0, y - 0.02, b1]])
                    cv2.fillConvexPoly(img, to_fixed(view.proj(view.cam(cell.world(bit)))), (240.0, 240.0, 240.0),
                                       cv2.LINE_AA, 4)


def draw_ball(img, view, center, radius, color):
    pc = view.cam(center)
    if pc[0, 2] < 2:
        return
    c = view.proj(pc)[0]
    r = view.f * radius / pc[0, 2]
    if r < 0.8:
        return
    col = np.asarray(color, float)
    cv2.circle(img, (int(c[0] * 16), int(c[1] * 16)), int(r * 16), tuple(float(v) for v in col * 0.7), -1, cv2.LINE_AA, 4)
    cv2.circle(img, (int((c[0] - r * 0.2) * 16), int((c[1] - r * 0.25) * 16)), int(r * 0.75 * 16),
               tuple(float(v) for v in np.clip(col * 1.05, 0, 255)), -1, cv2.LINE_AA, 4)
    if r > 4:
        for k in range(6):
            ang = k * 1.047 + 0.4
            hc = c + np.array([math.cos(ang), math.sin(ang)]) * r * 0.55
            cv2.circle(img, (int(hc[0] * 16), int(hc[1] * 16)), int(r * 0.12 * 16), tuple(float(v) for v in col * 0.45),
                       -1, cv2.LINE_AA, 4)


# ---------------------------------------------------------------------------
# Backgrounds
# ---------------------------------------------------------------------------

_BG_FILES = None


def background_files():
    global _BG_FILES
    if _BG_FILES is None:
        files = []
        for d in BG_DIRS:
            if d.exists():
                files += [p for p in d.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
        _BG_FILES = files
    return _BG_FILES


def procedural_background(w, h, rng):
    top = rng.uniform(120, 245, 3)
    bottom = rng.uniform(40, 200, 3)
    t = np.linspace(0, 1, h)[:, None, None]
    img = (top * (1 - t) + bottom * t) * np.ones((1, w, 1))
    img = img.astype(np.float32)
    # ceiling light panels / windows / furniture-like rectangles
    for _ in range(int(rng.integers(3, 18))):
        x0, y0 = rng.integers(0, w), rng.integers(0, h)
        ww, hh = rng.integers(w // 30, w // 3), rng.integers(h // 30, h // 3)
        col = rng.choice([rng.uniform(200, 255, 3), rng.uniform(0, 255, 3), rng.uniform(20, 90, 3)])
        cv2.rectangle(img, (int(x0), int(y0)), (int(x0 + ww), int(y0 + hh)), tuple(float(c) for c in col), -1)
    for _ in range(int(rng.integers(0, 10))):
        p = rng.integers(0, max(w, h), 4)
        cv2.line(img, (int(p[0]), int(p[1])), (int(p[2]), int(p[3])), tuple(float(c) for c in rng.uniform(0, 255, 3)),
                 int(rng.integers(1, 8)))
    noise = cv2.resize(rng.normal(0, 1, (max(2, h // 32), max(2, w // 32))).astype(np.float32), (w, h))
    img += noise[..., None] * rng.uniform(0, 25)
    img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.5, 3))
    return np.clip(img, 0, 255).astype(np.uint8)


def load_background(w, h, rng):
    files = background_files()
    if files and rng.random() < 0.82:
        for _ in range(4):
            path = files[int(rng.integers(len(files)))]
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if img is None:
                continue
            ih, iw = img.shape[:2]
            scale = max(w / iw, h / ih) * rng.uniform(1.0, 1.8)
            img = cv2.resize(img, (max(w, int(iw * scale + 1)), max(h, int(ih * scale + 1))), interpolation=cv2.INTER_AREA)
            ih, iw = img.shape[:2]
            x0 = int(rng.integers(0, iw - w + 1))
            y0 = int(rng.integers(0, ih - h + 1))
            img = img[y0:y0 + h, x0:x0 + w]
            if rng.random() < 0.5:
                img = img[:, ::-1]
            return np.ascontiguousarray(img)
    return procedural_background(w, h, rng)


# ---------------------------------------------------------------------------
# Scene sampling and rendering
# ---------------------------------------------------------------------------

def sample_scene(rng):
    """World: y up, floor at y = 0, inches."""
    cells, extras = [], []
    pivot_h = rng.uniform(42, 64)
    n_hives = 1 if rng.random() < 0.65 else 2
    first = "red" if rng.random() < 0.5 else "blue"
    yaw0 = rng.uniform(0, 2 * math.pi)
    for k in range(n_hives):
        color = first if k == 0 else ("blue" if first == "red" else "red")
        offset = np.array([0.0, 0.0, 0.0]) if k == 0 else np.array([rng.choice([-1, 1]) * rng.uniform(26, 44), 0, 0])
        Ry = rot([0, 1, 0], yaw0 + (0 if k == 0 else rng.uniform(-0.15, 0.15)))
        pivot = Ry @ offset + np.array([0, pivot_h, 0])
        tilt = rng.uniform(-0.75, 0.75)
        Rh = Ry @ rot([1, 0, 0], tilt)
        single = rng.random() < 0.18
        gap = 18.84
        drop = rng.uniform(1.0, 5.0)
        cell_tilt = rng.uniform(0, 0.55)
        for side in ((1,) if single else (1, -1)):
            zc = side * (gap / 2 + ZEND)
            Rc = Rh @ rot([1, 0, 0], side * cell_tilt * (1 if rng.random() < 0.7 else -1))
            if single and rng.random() < 0.6:
                Rc = rot(rng.normal(0, 1, 3), rng.uniform(0, math.pi)) @ Rc
            tm = pivot + Rh @ np.array([0, -drop - 1.6, zc]) + Rc @ np.array([0, 1.6, 0])
            closed = -1 if side == 1 else 1
            if rng.random() < 0.1:
                closed = 0
            cells.append(Cell(color, Rc, tm, closed, rng))
        extras.append(("hive_arm", pivot, Rh, gap, drop, single))
    return cells, extras, pivot_h


def render_sample(rng, size=None):
    if size is None:
        size = [(640, 480), (480, 640), (640, 360), (512, 512), (640, 400), (480, 360)][int(rng.integers(6))]
    W, H = size
    cells, extras, pivot_h = sample_scene(rng)
    # Camera: look at one cell (mostly) from a random direction.
    target_cell = cells[int(rng.integers(len(cells)))]
    target = target_cell.tm + target_cell.Rm @ np.array([0, 6.5, 0]) + rng.normal(0, 6, 3)
    for _ in range(40):
        u = rng.random()
        dist = rng.uniform(17, 34) if u < 0.3 else (math.exp(rng.uniform(math.log(34), math.log(150))) if u < 0.87
                                                   else rng.uniform(150, 380))
        az = rng.uniform(0, 2 * math.pi)
        cam_h = rng.choice([rng.uniform(5, 24), rng.uniform(24, 70), rng.uniform(40, 110)], p=[0.4, 0.45, 0.15])
        dy = cam_h - target[1]
        horiz = math.sqrt(max(dist ** 2 - dy ** 2, 4.0))
        eye = target + np.array([math.cos(az) * horiz, dy, math.sin(az) * horiz])
        if all(np.linalg.norm(Rm_inv_point(c, eye)) > 15 for c in cells):
            break
    hfov = math.radians(rng.uniform(48, 105))
    f = (W / 2) / math.tan(hfov / 2)
    aim = target + rng.uniform(-1, 1, 3) * dist * math.tan(hfov / 2) * 0.32
    if rng.random() < 0.04:  # negative: look away from the hives
        aim = eye + rng.normal(0, 1, 3) * 100
    roll = rng.normal(0, 0.12) if rng.random() < 0.85 else rng.uniform(-0.9, 0.9)
    R, t = look_at(eye, aim, roll)
    view = View(R, t, f * SS, W * SS / 2 + rng.normal(0, W * 0.01) * SS, H * SS / 2 + rng.normal(0, H * 0.01) * SS,
                W * SS, H * SS, eye)
    img = cv2.resize(load_background(W, H, rng), (W * SS, H * SS), interpolation=cv2.INTER_LINEAR)

    light = np.array([rng.normal(0, 0.6), 1.0, rng.normal(0, 0.6)])
    light /= np.linalg.norm(light)
    lp = (rng.uniform(0.5, 0.85), rng.uniform(0.25, 0.6), rng.uniform(0.05, 0.6), rng.uniform(6, 40))
    metal = np.array([rng.uniform(150, 215)] * 3) * rng.uniform(0.92, 1.0, 3)

    # Floor tiles (gray foam mat) as a textured plane.
    if rng.random() < 0.55:
        draw_floor(img, view, rng)

    # Draw list sorted by depth (far first).
    draws = []
    for cell in cells:
        eye_m = cell.Rm.T @ (eye - cell.tm)
        near_side = 1 if eye_m[2] >= 0 else -1
        far_d = float(view.cam(cell.world([0, 6, -near_side * ZF]))[0, 2])
        near_d = float(view.cam(cell.world([0, 6, near_side * ZF]))[0, 2])
        mid_d = (far_d + near_d) / 2
        draws.append((far_d, "frame", cell, -near_side))
        draws.append((mid_d + 0.001, "inside", cell, None))
        draws.append((near_d, "frame", cell, near_side))
    for ex in extras:
        _, pivot, Rh, gap, drop, single = ex
        d = float(view.cam(pivot)[0, 2])
        draws.append((d + 30, "arm", ex, None))
    n_balls = int(rng.integers(0, 9))
    for _ in range(n_balls):
        center = np.array([rng.uniform(-90, 90), 1.4, rng.uniform(-90, 90)]) + np.array([eye[0], 0, eye[2]]) * 0.0
        if rng.random() < 0.5 and cells:
            c = cells[int(rng.integers(len(cells)))]
            center = c.world([rng.uniform(-8, 8), 1.45, rng.uniform(-5, 5)])[0]
        col = [(40, 220, 245), (40, 40, 210), (200, 90, 30)][int(rng.integers(3))]
        draws.append((float(view.cam(center)[0, 2]), "ball", center, col))
    for _ in range(int(rng.integers(0, 4))):
        p = np.array([rng.uniform(-80, 80), 0, rng.uniform(-80, 80)])
        draws.append((float(view.cam(p + [0, 10, 0])[0, 2]), "distractor", p, int(rng.integers(3))))
    draws.sort(key=lambda d: -d[0])
    for depth, kind, obj, extra in draws:
        if depth < 1:
            continue
        if kind == "frame":
            draw_frame(img, view, obj, extra, light, lp)
        elif kind == "inside":
            for sx in (-1, 1):
                p0 = obj.world([sx * CHURRO_X, EAVE, -ZEND - 0.2])[0]
                p1 = obj.world([sx * CHURRO_X, EAVE, ZEND + 0.2])[0]
                draw_rod(img, view, p0, p1, 0.55, metal, light, lp, ties=(0.2, 0.4, 0.6, 0.8), tie_color=obj.tie_color)
            if obj.haze > 0.01:
                pc = view.cam(obj.corners_world())
                if np.all(pc[:, 2] > 1):
                    hull = cv2.convexHull(view.proj(pc).astype(np.float32)).reshape(-1, 2)
                    blend_poly(img, hull, (245, 245, 245), obj.haze)
            draw_apriltags(img, view, obj, rng)
            draw_skins(img, view, obj, light, rng)
        elif kind == "arm":
            draw_arm(img, view, obj, metal, light, lp, pivot_h, rng)
        elif kind == "ball":
            draw_ball(img, view, obj[None], 1.4, extra)
        elif kind == "distractor":
            draw_distractor(img, view, obj, extra, metal, light, lp, rng)

    img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
    labels = []
    for cell in cells:
        lab = cell_label(cell, view, W, H)
        if lab:
            labels.append(lab)
    img, labels = postprocess(img, labels, rng)
    return img, labels


def Rm_inv_point(cell, p):
    return cell.Rm.T @ (p - cell.tm) - np.array([0, 6, 0])


def draw_floor(img, view, rng):
    """Gray foam-tile floor (y = 0) by casting a ray per pixel at half resolution."""
    h, w = img.shape[:2]
    sw, sh = w // 6, h // 6
    xs = (np.arange(sw) + 0.5) * (w / sw)
    ys = (np.arange(sh) + 0.5) * (h / sh)
    u, v = np.meshgrid(xs.astype(np.float32), ys.astype(np.float32))
    d_c = np.stack([(u - view.cx) / view.f, (v - view.cy) / view.f, np.ones_like(u)], -1)
    d_w = d_c @ view.R.astype(np.float32)  # camera -> world directions (R is world->camera)
    eye = view.eye
    with np.errstate(divide="ignore", invalid="ignore"):
        t = -eye[1] / d_w[..., 1]
    valid = (d_w[..., 1] < -1e-4) & (t > 0) & (t < rng.uniform(250, 900))
    if not valid.any():
        return
    X = eye[0] + t * d_w[..., 0]
    Z = eye[2] + t * d_w[..., 2]
    tile = float(rng.choice([24.0, 36.0, 48.0]))
    lw = tile * rng.uniform(0.006, 0.02)
    gx = np.abs(((X + tile / 2) % tile) - tile / 2) < lw
    gz = np.abs(((Z + tile / 2) % tile) - tile / 2) < lw
    base = rng.uniform(60, 175) * rng.uniform(0.9, 1.1, 3)
    shade_ = np.where(gx | gz, rng.uniform(0.6, 0.85), 1.0)
    checker = (((np.floor(X / tile) + np.floor(Z / tile)) % 2) * rng.uniform(-0.06, 0.06)) + 1
    col = base[None, None] * (shade_ * checker)[..., None]
    col = cv2.resize(col.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    mask = cv2.resize(valid.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST).astype(bool)
    img[mask] = np.clip(col[mask], 0, 255).astype(np.uint8)


def draw_arm(img, view, ex, metal, light, lp, pivot_h, rng):
    _, pivot, Rh, gap, drop, single = ex
    ax = Rh
    # Beam under the cells along the hive axis, posts to each cell, and an A-frame stand.
    beam_c = pivot + Rh @ np.array([0, -drop - 9.0, 0])
    draw_box(img, view, beam_c, ax, np.array([0.5, 0.5, gap / 2 + ZEND]), metal, light, lp)
    for side in ((1,) if single else (1, -1)):
        post = pivot + Rh @ np.array([0, -drop - 5.0, side * (gap / 2 + ZF)])
        draw_box(img, view, post, ax, np.array([0.5, 4.2, 0.5]), metal, light, lp)
    for sx in (-1, 1):
        top = pivot + np.array([0, -1.0, 0])
        foot = np.array([pivot[0], 0.0, pivot[2]]) + Rh[:, 0] * sx * pivot_h * 0.45
        mid = (top + foot) / 2
        d = foot - top
        L = np.linalg.norm(d)
        zax = d / L
        xax = np.cross(zax, Rh[:, 2])
        if np.linalg.norm(xax) < 1e-6:
            continue
        xax /= np.linalg.norm(xax)
        yax = np.cross(zax, xax)
        draw_box(img, view, mid, np.column_stack([xax, yax, zax]), np.array([0.75, 0.75, L / 2]), metal * 0.95, light, lp)
    if rng.random() < 0.55:
        banner_c = np.array([pivot[0], pivot_h * 0.62, pivot[2]])
        axes = np.column_stack([Rh[:, 0], [0, 1, 0], np.cross(Rh[:, 0], [0, 1, 0])])
        draw_box(img, view, banner_c, axes, np.array([pivot_h * 0.33, 4.5, 0.2]), np.array([40, 215, 240.0]), light, lp)


def draw_distractor(img, view, p, kind, metal, light, lp, rng):
    if kind == 0:  # robot
        size = np.array([rng.uniform(7, 9), rng.uniform(4, 9), rng.uniform(7, 9)])
        yaw = rot([0, 1, 0], rng.uniform(0, 6.3))
        base = rng.choice([metal, np.array([40.0, 40, 40]), np.array([60.0, 60, 200]), np.array([200.0, 80, 40])])
        draw_box(img, view, p + [0, size[1], 0], yaw, size, base, light, lp)
    elif kind == 1:  # green flower post with an orange ring
        draw_box(img, view, p + [0, 11, 0], np.eye(3), np.array([1.0, 11, 1.0]), np.array([60.0, 190, 70]), light, lp)
        draw_box(img, view, p + [0, 22.5, 0], np.eye(3), np.array([2.6, 0.6, 2.6]), np.array([40.0, 140, 250]), light, lp)
    else:  # field wall segment
        yaw = rot([0, 1, 0], rng.uniform(0, 6.3))
        draw_box(img, view, p + [0, 6, 0], yaw, np.array([30, 6, 0.3]), np.array([210.0, 210, 215]), light, lp)


def cell_label(cell, view, W, H):
    eye_m = cell.Rm.T @ (view.eye - cell.tm)
    ids = hk.CORNER_IDS if eye_m[2] >= 0 else [hk.relabel_rot180(c) for c in hk.CORNER_IDS]
    world = cell.world(np.array([M.corners[c] for c in ids]))
    pc = view.cam(world)
    if np.any(pc[:, 2] < 1):
        return None
    kp = view.proj(pc) / SS
    # bounding box of the whole visible rib geometry (both frames, outer contour)
    outline = []
    for zc in (ZF, -ZF):
        for z in (zc - RIBD / 2, zc + RIBD / 2):
            outline.append(np.column_stack([OUTER_C, np.full(len(OUTER_C), z)]))
    ow = view.cam(cell.world(np.vstack(outline)))
    if np.any(ow[:, 2] < 1):
        return None
    op = view.proj(ow) / SS
    x0, y0 = op.min(0)
    x1, y1 = op.max(0)
    cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(W - 1, x1), min(H - 1, y1)
    if cx1 - cx0 < 12 or cy1 - cy0 < 12:
        return None
    inside = (kp[:, 0] >= 0) & (kp[:, 0] <= W - 1) & (kp[:, 1] >= 0) & (kp[:, 1] <= H - 1)
    if inside.sum() < 3:
        return None
    return {"cls": 0 if cell.color == "red" else 1, "box": [cx0, cy0, cx1, cy1], "kp": kp, "vis": inside,
            "full_box": [x0, y0, x1, y1]}


_VIG, _DIST = {}, {}
K1_LEVELS = np.round(np.linspace(-0.28, 0.08, 10), 3)


def postprocess(img, labels, rng):
    H, W = img.shape[:2]
    out = img.astype(np.float32)
    field = cv2.resize(rng.normal(0, 1, (3, 4)).astype(np.float32), (W, H), interpolation=cv2.INTER_CUBIC)
    out *= (1 + field * rng.uniform(0, 0.18))[..., None]
    out *= (rng.uniform(0.85, 1.15, 3) * rng.uniform(0.75, 1.2)).astype(np.float32)
    if rng.random() < 0.4:
        key = (W, H)
        if key not in _VIG:
            yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
            _VIG[key] = ((xx - W / 2) ** 2 + (yy - H / 2) ** 2) / ((W / 2) ** 2 + (H / 2) ** 2)
        out *= (1 - rng.uniform(0.1, 0.45) * _VIG[key])[..., None]
    if rng.random() < 0.75:
        out += rng.standard_normal(out.shape, dtype=np.float32) * rng.uniform(1, 7)
    out = np.clip(out, 0, 255).astype(np.uint8)
    gamma, contrast = rng.uniform(0.75, 1.35), rng.uniform(0.8, 1.25)
    lut = np.clip((((np.arange(256) / 255.0) ** gamma) - 0.5) * contrast * 255 + 127.5 + rng.uniform(-18, 18), 0, 255)
    out = cv2.LUT(out, lut.astype(np.uint8))
    if rng.random() < 0.4:
        k = int(rng.integers(3, 13))
        kernel = np.zeros((k, k), np.float32)
        ang, c = rng.uniform(0, math.pi), (k - 1) / 2
        cv2.line(kernel, (int(c - math.cos(ang) * c), int(c - math.sin(ang) * c)),
                 (int(c + math.cos(ang) * c), int(c + math.sin(ang) * c)), 1.0, 1)
        out = cv2.filter2D(out, -1, kernel / max(kernel.sum(), 1))
    if rng.random() < 0.45:
        out = cv2.GaussianBlur(out, (0, 0), rng.uniform(0.3, 1.5))
    if rng.random() < 0.45:
        out, labels = lens_distort(out, labels, float(K1_LEVELS[int(rng.integers(len(K1_LEVELS)))]))
    return out, labels


def lens_distort(img, labels, k1):
    H, W = img.shape[:2]
    f = max(W, H) * 0.8
    key = (W, H, k1)
    if key not in _DIST:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        xd, yd = (xx - W / 2) / f, (yy - H / 2) / f
        xu, yu = xd.copy(), yd.copy()
        for _ in range(6):  # invert x_d = x_u (1 + k1 r_u^2)
            r2 = xu * xu + yu * yu
            xu, yu = xd / (1 + k1 * r2), yd / (1 + k1 * r2)
        if len(_DIST) > 40:
            _DIST.clear()
        _DIST[key] = (xu * f + W / 2, yu * f + H / 2)
    mx, my = _DIST[key]
    out = cv2.remap(img, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

    def fwd(pts):
        x, y = (pts[:, 0] - W / 2) / f, (pts[:, 1] - H / 2) / f
        r2 = x * x + y * y
        return np.stack([x * (1 + k1 * r2) * f + W / 2, y * (1 + k1 * r2) * f + H / 2], 1)

    def monotonic(pts):  # beyond this radius the distortion folds points back into the picture
        x, y = (pts[:, 0] - W / 2) / f, (pts[:, 1] - H / 2) / f
        return (1 + 3 * k1 * (x * x + y * y)) > 0.15

    new = []
    for lab in labels:
        kp = fwd(lab["kp"])
        x0, y0, x1, y1 = lab["full_box"]
        edge = np.array([[x, y] for x in np.linspace(x0, x1, 9) for y in (y0, y1)] +
                        [[x, y] for y in np.linspace(y0, y1, 9) for x in (x0, x1)])
        e = fwd(edge)
        bx0, by0 = np.maximum(e.min(0), 0)
        bx1, by1 = np.minimum(e.max(0), [W - 1, H - 1])
        if bx1 - bx0 < 12 or by1 - by0 < 12:
            continue
        inside = (kp[:, 0] >= 0) & (kp[:, 0] <= W - 1) & (kp[:, 1] >= 0) & (kp[:, 1] <= H - 1)
        inside &= monotonic(lab["kp"])
        if inside.sum() < 3 or not monotonic(edge).all():
            continue
        new.append({**lab, "kp": kp, "vis": inside, "box": [bx0, by0, bx1, by1]})
    return out, new


def yolo_lines(labels, W, H):
    lines = []
    for lab in labels:
        x0, y0, x1, y1 = lab["box"]
        vals = [lab["cls"], (x0 + x1) / 2 / W, (y0 + y1) / 2 / H, (x1 - x0) / W, (y1 - y0) / H]
        for (x, y), v in zip(lab["kp"], lab["vis"]):
            vals += [x / W, y / H, 2] if v else [0, 0, 0]
        lines.append(" ".join(f"{v:.6f}" if isinstance(v, float) else str(v) for v in vals))
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def draw_overlay(img, labels):
    out = img.copy()
    for lab in labels:
        x0, y0, x1, y1 = [int(v) for v in lab["box"]]
        cv2.rectangle(out, (x0, y0), (x1, y1), (0, 255, 255) if lab["cls"] == 0 else (255, 255, 0), 1)
        kp = lab["kp"]
        for a, b in [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0)]:
            cv2.line(out, tuple(int(v) for v in kp[a]), tuple(int(v) for v in kp[b]), (0, 255, 0), 1, cv2.LINE_AA)
            cv2.line(out, tuple(int(v) for v in kp[a + 5]), tuple(int(v) for v in kp[b + 5]), (255, 0, 255), 1, cv2.LINE_AA)
        for i, (x, y) in enumerate(kp):
            cv2.circle(out, (int(x), int(y)), 3, (0, 255, 0) if i < 5 else (255, 0, 255), -1)
            cv2.putText(out, hk.CORNER_IDS[i], (int(x) + 3, int(y) - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.32, (255, 255, 255), 1)
    return out


def cmd_preview(args):
    rng = np.random.default_rng(args.seed)
    tiles = []
    t0 = time.time()
    for _ in range(args.n):
        img, labels = render_sample(rng)
        tile = draw_overlay(img, labels) if args.overlay else img
        tile = cv2.resize(tile, (480, int(480 * tile.shape[0] / tile.shape[1])))
        canvas = np.zeros((480, 480, 3), np.uint8)
        canvas[:tile.shape[0], :tile.shape[1]] = tile[:480]
        tiles.append(canvas)
    dt = (time.time() - t0) / args.n
    cols = 4
    rows = [np.hstack(tiles[i:i + cols] + [np.zeros_like(tiles[0])] * (cols - len(tiles[i:i + cols])))
            for i in range(0, len(tiles), cols)]
    cv2.imwrite(args.out, np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(json.dumps({"ok": True, "out": args.out, "sec_per_image": round(dt, 3)}))


def worker(job):
    out, split, start, count, seed = job
    rng = np.random.default_rng(seed)
    img_dir = Path(out) / "images" / split
    lab_dir = Path(out) / "labels" / split
    img_dir.mkdir(parents=True, exist_ok=True)
    lab_dir.mkdir(parents=True, exist_ok=True)
    n_cells = 0
    for i in range(start, start + count):
        name = f"syn_{i:06d}"
        if (lab_dir / f"{name}.txt").exists():
            continue
        try:
            img, labels = render_sample(rng)
        except Exception as exc:  # noqa: BLE001
            print("render error", i, exc, flush=True)
            continue
        H, W = img.shape[:2]
        cv2.imwrite(str(img_dir / f"{name}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(55, 96))])
        (lab_dir / f"{name}.txt").write_text("\n".join(yolo_lines(labels, W, H)) + ("\n" if labels else ""))
        n_cells += len(labels)
    return count, n_cells


def cmd_make(args):
    from multiprocessing import Pool
    if args.low_priority:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)  # workers inherit it
    per = 500
    jobs = [(args.out, args.split, s, min(per, args.count - s), args.seed * 100003 + s)
            for s in range(args.offset, args.offset + args.count, per)]
    jobs = [(o, sp, s, min(per, args.offset + args.count - s), sd) for (o, sp, s, _, sd) in jobs]
    t0, done, cells = time.time(), 0, 0
    with Pool(args.workers) as pool:
        for count, n_cells in pool.imap_unordered(worker, jobs):
            done += count
            cells += n_cells
            print(f"{done}/{args.count} images, {cells} cells, {time.time() - t0:.0f}s", flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preview")
    p.add_argument("--n", type=int, default=12)
    p.add_argument("--out", default="preview.jpg")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--overlay", action="store_true")
    p.set_defaults(func=cmd_preview)
    p = sub.add_parser("make")
    p.add_argument("--out", default=str(ROOT / "dataset" / "yolo"))
    p.add_argument("--split", default="synth")
    p.add_argument("--count", type=int, default=50000)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--low-priority", action="store_true")
    p.set_defaults(func=cmd_make)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
