"""Shared geometry, camera fitting and label format for the BIOBUZZ hive cell.

Python port of docs/geometry.js plus a pinhole camera fit. Everything that needs the
cell's real shape (labelling tools, synthetic data, training targets, the detector's
geometric refinement) imports it from here, so the definitions exist exactly once:
docs/data/hive_model.json.

Coordinates
    Cell frame (inches): x to the right, y up (roof), z towards the viewer. The FRONT
    frame is at z = +z_front, the BACK frame at z = -z_front. "Front" means the frame that
    is closer to the camera, so a correctly labelled cell always has the camera at z > 0.
    Image: pixels, x right, y down.
"""
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MODEL_JSON = ROOT / "docs" / "data" / "hive_model.json"

MAX_HIVES = 4
LINES_PER_HIVE = 14
POINTS_PER_HIVE = 28
VALUES_PER_HIVE = 60
OUTPUT_SIZE = MAX_HIVES * VALUES_PER_HIVE


def _intersect(p, r, q, s):
    cross = r[0] * s[1] - r[1] * s[0]
    if abs(cross) < 1e-9:
        return [q[0], q[1]]
    t = ((q[0] - p[0]) * s[1] - (q[1] - p[1]) * s[0]) / cross
    return [p[0] + r[0] * t, p[1] + r[1] * t]


def offset_polygon(points, d):
    """Offset a convex counter-clockwise polygon outward by d."""
    n = len(points)
    lines = []
    for i, p in enumerate(points):
        q = points[(i + 1) % n]
        dx, dy = q[0] - p[0], q[1] - p[1]
        length = math.hypot(dx, dy)
        nx, ny = dy / length, -dx / length
        lines.append(((p[0] + nx * d, p[1] + ny * d), (dx / length, dy / length)))
    return [_intersect(lines[i - 1][0], lines[i - 1][1], lines[i][0], lines[i][1]) for i in range(n)]


class HiveModel:
    def __init__(self, path=MODEL_JSON):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        d = raw["dimensions"]
        half = d["openingWidth"] / 2
        self.inner = [[-half, 0], [half, 0], [half, d["eaveHeight"]], [0, d["peakHeight"]], [-half, d["eaveHeight"]]]
        self.center = offset_polygon(self.inner, d["ribWidth"] / 2)
        self.outer = offset_polygon(self.inner, d["ribWidth"])
        self.z_front = d["cellDepth"] / 2 - d["ribDepth"] / 2
        keys = ["BL", "BR", "ER", "P", "EL"]
        self.corner_ids = list(raw["cornerOrder"])
        self.corners = {}
        for frame, z in (("F", self.z_front), ("B", -self.z_front)):
            for key, (x, y) in zip(keys, self.center):
                self.corners[f"{frame}.{key}"] = (x, y, z)
        self.churro_x = half - d["churroInset"]
        self.lines = []
        for index, line in enumerate(raw["lines"]):
            world = []
            for cid in (line["a"], line["b"]):
                x, y, z = self.corners[cid]
                world.append((math.copysign(self.churro_x, x), d["eaveHeight"], z) if line["kind"] == "iron" else (x, y, z))
            self.lines.append({**line, "index": index, "world": world})
        self.point_corner = [cid for line in self.lines for cid in (line["a"], line["b"])]
        self.point_world = np.array([w for line in self.lines for w in line["world"]], dtype=float)
        self.corner_world = np.array([self.corners[c] for c in self.corner_ids], dtype=float)
        self.corner_index = {cid: i for i, cid in enumerate(self.corner_ids)}
        self.point_corner_index = np.array([self.corner_index[c] for c in self.point_corner])
        self.line_names = [line["name"] for line in raw["lines"]]
        self.dims = d
        self.raw = raw
        # Size of the cell used to express errors relative to the object (inches).
        self.diagonal = float(np.linalg.norm(self.corner_world.max(0) - self.corner_world.min(0)))


MODEL = HiveModel()
CORNER_IDS = MODEL.corner_ids
_SWAP = {"BL": "BR", "BR": "BL", "ER": "EL", "EL": "ER", "P": "P"}
# Horizontal image flip keeps front/back and swaps left/right.
FLIP_INDEX = [CORNER_IDS.index(f"{c.split('.')[0]}.{_SWAP[c.split('.')[1]]}") for c in CORNER_IDS]


def relabel_rot180(cid):
    """The same physical corner when the OTHER frame is taken as the front one."""
    frame, key = cid.split(".")
    return ("B" if frame == "F" else "F") + "." + _SWAP[key]


def relabel_mirror(cid):
    """Left/right swapped inside the same frame (a mirrored, physically wrong labelling)."""
    frame, key = cid.split(".")
    return frame + "." + _SWAP[key]


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

def rodrigues(rvec):
    theta = float(np.linalg.norm(rvec))
    if theta < 1e-12:
        return np.eye(3)
    k = np.asarray(rvec, dtype=float) / theta
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(theta) * K + (1 - math.cos(theta)) * (K @ K)


class Camera:
    """Pinhole camera with square pixels. Xc = R Xw + t ; u = f x/z + cx ; v = f y/z + cy."""

    def __init__(self, rvec, tvec, f, cx, cy):
        self.rvec = np.asarray(rvec, dtype=float).reshape(3)
        self.tvec = np.asarray(tvec, dtype=float).reshape(3)
        self.f, self.cx, self.cy = float(f), float(cx), float(cy)
        self.R = rodrigues(self.rvec)

    def camera_points(self, world):
        return np.asarray(world, dtype=float).reshape(-1, 3) @ self.R.T + self.tvec

    def project(self, world):
        pc = self.camera_points(world)
        z = np.where(np.abs(pc[:, 2]) < 1e-9, 1e-9, pc[:, 2])
        return np.stack([self.f * pc[:, 0] / z + self.cx, self.f * pc[:, 1] / z + self.cy], axis=1)

    @property
    def center(self):
        """Camera position in cell coordinates."""
        return -self.R.T @ self.tvec

    def to_dict(self):
        return {"rvec": [round(v, 6) for v in self.rvec], "tvec": [round(v, 4) for v in self.tvec],
                "f": round(self.f, 2), "cx": round(self.cx, 2), "cy": round(self.cy, 2)}

    @staticmethod
    def from_dict(d):
        return Camera(d["rvec"], d["tvec"], d["f"], d["cx"], d["cy"])


def fit_camera(world, image, size, weights=None, f_prior_weight=2.0, robust=True):
    """Fit rotation, translation and focal length (principal point = image centre).

    world: (N,3) cell coordinates, image: (N,2) pixels, size: (width, height).
    Returns (camera, residuals_px) or (None, None) when it cannot be solved (N < 4).
    """
    import cv2
    from scipy.optimize import least_squares

    world = np.asarray(world, dtype=float).reshape(-1, 3)
    image = np.asarray(image, dtype=float).reshape(-1, 2)
    n = len(world)
    if n < 4:
        return None, None
    width, height = size
    cx, cy = width / 2.0, height / 2.0
    big = float(max(width, height))
    w = np.ones(n) if weights is None else np.asarray(weights, dtype=float)
    f0 = 0.8 * big
    candidates = []
    for factor in (0.35, 0.45, 0.6, 0.8, 1.0, 1.3, 1.8, 2.6, 4.0):
        f = factor * big
        K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])
        for flag in (cv2.SOLVEPNP_SQPNP, cv2.SOLVEPNP_EPNP):
            try:
                ok, rvec, tvec = cv2.solvePnP(world.reshape(-1, 1, 3), image.reshape(-1, 1, 2), K, None, flags=flag)
            except cv2.error:
                continue
            if not ok:
                continue
            cam = Camera(rvec, tvec, f, cx, cy)
            if np.any(cam.camera_points(world)[:, 2] <= 0):
                continue
            err = np.linalg.norm(cam.project(world) - image, axis=1)
            candidates.append((float(np.sqrt(np.mean(err ** 2))), rvec.reshape(3), tvec.reshape(3), f))
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: c[0])
    scale = max(3.0, 0.012 * float(np.linalg.norm(image.max(0) - image.min(0))))

    def residual(params):
        cam = Camera(params[0:3], params[3:6], math.exp(params[6]), cx, cy)
        pc = cam.camera_points(world)
        z = np.where(pc[:, 2] < 1e-3, 1e-3, pc[:, 2])
        u = cam.f * pc[:, 0] / z + cx
        v = cam.f * pc[:, 1] / z + cy
        res = np.concatenate([(u - image[:, 0]) * w, (v - image[:, 1]) * w])
        return np.append(res, f_prior_weight * (params[6] - math.log(f0)))

    best = None
    for _, rvec, tvec, f in candidates[:3]:
        x0 = np.concatenate([rvec, tvec, [math.log(f)]])
        try:
            sol = least_squares(residual, x0, loss="soft_l1" if robust else "linear", f_scale=scale, max_nfev=400)
        except Exception:  # noqa: BLE001
            continue
        if best is None or sol.cost < best.cost:
            best = sol
    if best is None:
        return None, None
    cam = Camera(best.x[0:3], best.x[3:6], math.exp(best.x[6]), cx, cy)
    return cam, np.linalg.norm(cam.project(world) - image, axis=1)


def _rms(values):
    values = np.asarray(values, dtype=float)
    return float(np.sqrt(np.mean(values ** 2))) if len(values) else float("nan")


def fit_cell(measured, size):
    """Fit the camera to the measured corners of one cell and judge the labelling.

    measured: {corner_id: (x, y)} for the corners that were actually seen.
    Returns a dict: ok, camera, rms, residuals{id: px}, mirror_rms, cam_center,
    behind (camera on the back side -> front/back should be swapped), n.
    """
    ids = [c for c in CORNER_IDS if c in measured]
    result = {"ok": False, "n": len(ids)}
    front = sum(1 for c in ids if c.startswith("F"))
    if len(ids) < 4:
        result["reason"] = "few"
        return result
    image = np.array([measured[c] for c in ids], dtype=float)
    world = np.array([MODEL.corners[c] for c in ids])
    cam, res = fit_camera(world, image, size)
    if cam is None:
        result["reason"] = "nofit"
        return result
    mirror_world = np.array([MODEL.corners[relabel_mirror(c)] for c in ids])
    mcam, mres = fit_camera(mirror_world, image, size)
    center = cam.center
    result.update(ok=True, camera=cam, rms=_rms(res), residuals={c: float(r) for c, r in zip(ids, res)},
                  mirror_rms=_rms(mres) if mcam is not None else float("inf"), cam_center=[float(v) for v in center],
                  behind=bool(center[2] < 0), planar=bool(front < 2 or len(ids) - front < 2 or len(ids) < 6),
                  span=float(np.linalg.norm(image.max(0) - image.min(0))))
    return result


def project_cell(camera):
    """All 10 corners and all 28 line endpoints of a cell seen by a camera."""
    return camera.project(MODEL.corner_world), camera.project(MODEL.point_world)


def points28_from_corners(corners, camera=None):
    """28 line endpoints (main.py order) from the 10 corners (array 10x2).

    The iron-rod (Churro) endpoints sit slightly inside the eave corners. With a camera we
    add that small projected offset to the corner; without one they coincide with it.
    """
    corners = np.asarray(corners, dtype=float).reshape(10, 2)
    points = corners[MODEL.point_corner_index].copy()
    if camera is not None:
        proj_corner, proj_points = project_cell(camera)
        points += proj_points - proj_corner[MODEL.point_corner_index]
    return points


# ---------------------------------------------------------------------------
# Label files (dataset/labels/<stem>.json)
# ---------------------------------------------------------------------------

LABEL_DIR = ROOT / "dataset" / "labels"


def load_label(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_label(path, label):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(label, ensure_ascii=False, indent=1), encoding="utf-8")


def cell_corner_array(cell):
    """(10,2) coordinates and a list of states ('v' seen, 'h' estimated, None missing)."""
    xy = np.full((10, 2), np.nan)
    states = [None] * 10
    for i, cid in enumerate(CORNER_IDS):
        entry = cell.get("corners", {}).get(cid)
        if entry:
            xy[i] = entry[0], entry[1]
            states[i] = entry[2] if len(entry) > 2 else "v"
    return xy, states


def cell_camera(cell):
    return Camera.from_dict(cell["camera"]) if cell.get("camera") else None


def cell_center(cell):
    xy, _ = cell_corner_array(cell)
    return np.nanmean(xy, axis=0)


def cell_metadata(cell, all_centers, size):
    """[blue, right, top] as main.py stores it. Side/level are judged inside the picture:
    against the other labelled cells, or against the image centre when the cell is alone."""
    center = cell_center(cell)
    if len(all_centers) > 1:
        ref = np.mean(np.asarray(all_centers), axis=0)
    else:
        ref = np.array([size[0] / 2.0, size[1] / 2.0])
    return [1.0 if cell.get("color") == "blue" else 0.0, 1.0 if center[0] > ref[0] else 0.0, 1.0 if center[1] < ref[1] else 0.0]


def label_to_record(label, image_path=None):
    """Convert a label file to the record format of main.py / the annotation site."""
    width, height = label["width"], label["height"]
    cells = [c for c in label.get("cells", []) if not np.isnan(cell_corner_array(c)[0]).any()]
    cells = sorted(cells, key=lambda c: -cell_span(c))[:MAX_HIVES]
    cells = sorted(cells, key=lambda c: float(cell_center(c)[0]))
    centers = [cell_center(c) for c in cells]
    values = np.zeros(OUTPUT_SIZE, dtype=np.float32)
    estimated = []
    for slot, cell in enumerate(cells):
        xy, states = cell_corner_array(cell)
        points = points28_from_corners(xy, cell_camera(cell))
        points[:, 0] = np.clip(points[:, 0], 0, width - 1) / width
        points[:, 1] = np.clip(points[:, 1], 0, height - 1) / height
        offset = slot * VALUES_PER_HIVE
        values[offset] = 1.0
        values[offset + 1:offset + 4] = cell_metadata(cell, centers, (width, height))
        values[offset + 4:offset + VALUES_PER_HIVE] = points.reshape(-1)
        estimated += [slot * POINTS_PER_HIVE + p for p in range(POINTS_PER_HIVE)
                      if states[MODEL.point_corner_index[p]] != "v"]
    return {
        "image_path": image_path or label["image"],
        "label": [round(float(v), 6) for v in values],
        "hive_count": len(cells),
        "calibration": None,
        "line_names": MODEL.line_names,
        "estimated_points": estimated,
        "model_version": 2,
        "annotator": label.get("annotator", "claude"),
    }


def cell_span(cell):
    xy, _ = cell_corner_array(cell)
    return float(np.linalg.norm(np.nanmax(xy, axis=0) - np.nanmin(xy, axis=0)))
