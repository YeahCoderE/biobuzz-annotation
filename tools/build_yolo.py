#!/usr/bin/env python
"""Build the YOLO-pose dataset from the fitted real labels plus the synthetic images.

    .venv/Scripts/python -I tools/build_yolo.py [--min-iou 0.45] [--real-repeat 30]

Writes
    dataset/yolo/images/real_train, real_test (+ labels/...)   real photos, max side 1280
    dataset/yolo/train.txt, test.txt                            image lists (real train photos repeated)
    dataset/yolo/biobuzz.yaml                                   Ultralytics data file
Held-out test photos are split by burst: shots taken seconds apart go to the same side.
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hivekit as hk  # noqa: E402
import render as rd  # noqa: E402

ROOT = HERE.parent
YOLO = ROOT / "dataset" / "yolo"
LABELS = ROOT / "dataset" / "labels"
TEST_BURSTS = [
    ["20261031_191810"], ["20261031_192031"],
    ["20261031_192128", "20261031_192131", "20261031_192138", "20261031_192158"],
    ["20261031_204732", "20261031_204733"], ["20261031_205907"],
]
# Visual review of every fit (overlay sheets): photos dropped because a fit was wrong or unsure,
# and photos where only some cells are trusted (the others are painted out for training).
DROP = {"20261031_191822", "20261031_192124", "20261031_192449", "20261031_204741", "20261031_205032",
        "20261031_205204"}
PARTIAL = {"20261031_204724", "20261031_204756", "20261031_205314"}  # mask cells with fit IoU < 0.35
TEST = {s for burst in TEST_BURSTS for s in burst}
# Unlabelled real cells visible in test photos (cut off by the frame edge or seen through the near
# cell): detections there are neither right nor wrong. Boxes in original image pixels.
TEST_IGNORE = {
    "20261031_192031": [[700, 190, 1350, 590]],
    "20261031_192128": [[270, 1150, 1130, 1500]],
    "20261031_192131": [[0, 1300, 1998, 1499]],
    "20261031_192138": [[0, 1350, 1989, 1492]],
    "20261031_192158": [[600, 1140, 1150, 1421]],
    "20261031_204732": [[0, 880, 290, 1070]],
    "20261031_204733": [[0, 890, 240, 1050]],
    "20261031_205907": [[1150, 690, 1401, 1130]],
}
OUTLINE = np.vstack([np.column_stack([rd.OUTER_C, np.full(len(rd.OUTER_C), z)])
                     for zc in (hk.MODEL.z_front, -hk.MODEL.z_front) for z in (zc - rd.RIBD / 2, zc + rd.RIBD / 2)])


def cell_rows(cell, W, H, scale):
    cam = hk.Camera.from_dict(cell["camera"])
    pts = cam.project(OUTLINE) * scale
    x0, y0 = np.clip(pts.min(0), 0, [W - 1, H - 1])
    x1, y1 = np.clip(pts.max(0), 0, [W - 1, H - 1])
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    vals = [0 if cell["color"] == "red" else 1, (x0 + x1) / 2 / W, (y0 + y1) / 2 / H, (x1 - x0) / W, (y1 - y0) / H]
    for cid in hk.CORNER_IDS:
        x, y, _ = cell["corners"][cid]
        x, y = x * scale, y * scale
        vals += [x / W, y / H, 2] if (0 <= x <= W - 1 and 0 <= y <= H - 1) else [0, 0, 0]
    return " ".join(f"{v:.6f}" if isinstance(v, float) else str(v) for v in vals)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-iou", type=float, default=0.45)
    ap.add_argument("--real-repeat", type=int, default=30)
    ap.add_argument("--extra-train", nargs="*", default=[], help="extra image dirs (with labels/) for training")
    ap.add_argument("--negatives", type=int, default=0, help="background frames without hives (empty labels)")
    ap.add_argument("--synth-count", type=int, default=0, help="random subset of the synthetic images (0 = all)")
    ap.add_argument("--val-count", type=int, default=400, help="synthetic images (not in train) used by Ultralytics to validate")
    ap.add_argument("--synth-new-share", type=float, default=0.6, help="share of the subset taken from syn_020000+")
    args = ap.parse_args()
    report = {"train": [], "test": [], "skipped": []}
    for split in ("real_train", "real_test"):
        for kind in ("images", "labels"):
            d = YOLO / kind / split
            d.mkdir(parents=True, exist_ok=True)
            for f in d.iterdir():
                f.unlink()
    for path in sorted(LABELS.glob("*.json")):
        label = json.loads(path.read_text(encoding="utf-8"))
        cells = [c for c in label.get("cells", []) if "camera" in c]
        if path.stem in DROP or not cells:
            report["skipped"].append(path.stem)
            continue
        masked = []
        if path.stem in PARTIAL:
            masked = [c for c in cells if c.get("fit", {}).get("iou_roi", 0) < 0.35]
            cells = [c for c in cells if c not in masked]
        img = cv2.imread(str(ROOT / label["image"]))
        H0, W0 = img.shape[:2]
        for c in masked:  # paint out cells whose labels are not trusted
            x0, y0, x1, y1 = [int(v) for v in c["box"]]
            pad = int(0.08 * max(x1 - x0, y1 - y0))
            x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(W0, x1 + pad), min(H0, y1 + pad)
            roi = img[y0:y1, x0:x1]
            img[y0:y1, x0:x1] = cv2.GaussianBlur(cv2.resize(cv2.resize(roi, (8, 8)), (x1 - x0, y1 - y0)), (0, 0), 9)
        scale = min(1.0, 1280 / max(H0, W0))
        if scale < 1:
            img = cv2.resize(img, (int(round(W0 * scale)), int(round(H0 * scale))), interpolation=cv2.INTER_AREA)
        H, W = img.shape[:2]
        rows = [r for r in (cell_rows(c, W, H, scale) for c in cells) if r]
        split = "real_test" if path.stem in TEST else "real_train"
        if split == "real_test" and masked:
            report["skipped"].append(path.stem)
            continue
        cv2.imwrite(str(YOLO / "images" / split / f"{path.stem}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        (YOLO / "labels" / split / f"{path.stem}.txt").write_text("\n".join(rows) + "\n")
        report["test" if split == "real_test" else "train"].append(path.stem)
        if split == "real_test":
            meta = []
            for c in cells:
                cen = hk.Camera.from_dict(c["camera"]).center
                meta.append({"side_view": bool(abs(cen[2]) < 0.3 * float(np.linalg.norm(cen)))})
            (YOLO / "labels" / "real_test_meta").mkdir(parents=True, exist_ok=True)
            (YOLO / "labels" / "real_test_meta" / f"{path.stem}.json").write_text(json.dumps(meta))
            ign = [[v * scale for v in b] for b in TEST_IGNORE.get(path.stem, [])]
            (YOLO / "labels" / "real_test_ignore").mkdir(parents=True, exist_ok=True)
            (YOLO / "labels" / "real_test_ignore" / f"{path.stem}.json").write_text(json.dumps(ign))
    synth = sorted(str(p) for p in (YOLO / "images" / "synth").glob("*.jpg"))
    if args.synth_count and args.synth_count < len(synth):
        import random as _rs
        new = [p for p in synth if int(Path(p).stem.split("_")[1]) >= 20000]
        old = [p for p in synth if int(Path(p).stem.split("_")[1]) < 20000]
        k_new = min(len(new), int(args.synth_count * args.synth_new_share))
        synth = _rs.Random(11).sample(new, k_new) + _rs.Random(12).sample(old, min(len(old), args.synth_count - k_new))
    negatives = []
    if args.negatives:
        import random as _r
        neg_img, neg_lab = YOLO / "images" / "negatives", YOLO / "labels" / "negatives"
        neg_img.mkdir(parents=True, exist_ok=True)
        neg_lab.mkdir(parents=True, exist_ok=True)
        pool = sorted((ROOT / "dataset" / "backgrounds_small").rglob("*.jpg"))
        _r.Random(5).shuffle(pool)
        for src in pool[:args.negatives]:
            name = f"neg_{src.parent.name}_{src.stem}"
            dst = neg_img / f"{name}.jpg"
            if not dst.exists():
                dst.write_bytes(src.read_bytes())
                (neg_lab / f"{name}.txt").write_text("")
            negatives.append(str(dst))
    real_train = sorted(str(p) for p in (YOLO / "images" / "real_train").glob("*.jpg"))
    extra = []
    for d in args.extra_train:
        extra += sorted(str(p) for p in Path(d).glob("*.jpg"))
    train = synth + extra + negatives + real_train * args.real_repeat
    import random
    random.Random(7).shuffle(train)
    test = sorted(str(p) for p in (YOLO / "images" / "real_test").glob("*.jpg"))
    # Ultralytics' own validation (and its best.pt choice) uses synthetic images that are not in the
    # training list; the held-out real test photos are only ever read by tools/evaluate.py.
    all_synth = sorted(str(p) for p in (YOLO / "images" / "synth").glob("*.jpg"))
    in_train = set(train)
    import random as _rv
    spare = [p for p in all_synth if p not in in_train]
    if len(spare) < args.val_count:  # every synthetic image is in training: hold some out for validation
        val = _rv.Random(21).sample(all_synth, args.val_count)
        held = set(val)
        train = [p for p in train if p not in held]
    else:
        val = _rv.Random(21).sample(spare, args.val_count)
    (YOLO / "val.txt").write_text("\n".join(val) + "\n")
    (YOLO / "train.txt").write_text("\n".join(train) + "\n")
    (YOLO / "test.txt").write_text("\n".join(test) + "\n")
    flip = hk.FLIP_INDEX
    yaml = (f"path: {YOLO.as_posix()}\ntrain: train.txt\nval: val.txt\n"
            f"kpt_shape: [10, 3]\nflip_idx: {flip}\nnames:\n  0: red_cell\n  1: blue_cell\n")
    (YOLO / "biobuzz.yaml").write_text(yaml)
    print(json.dumps({"synth": len(synth), "extra": len(extra), "real_train_photos": len(real_train),
                      "train_list": len(train), "test_photos": len(test), "test": report["test"],
                      "skipped": report["skipped"]}, indent=1))


if __name__ == "__main__":
    main()
