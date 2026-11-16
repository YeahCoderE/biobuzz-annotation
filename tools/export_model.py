#!/usr/bin/env python
"""Export a trained run to ONNX for detect.py, main.py and the site, and verify it.

    .venv/Scripts/python -I tools/export_model.py --weights runs/v1/weights/best.pt [--imgsz 448] [--eval runs/eval.json]

Writes models/biobuzz_hive.pt, models/biobuzz_hive.onnx, models/biobuzz_hive.json and copies
the ONNX + JSON to docs/models/ so the annotation site can run the detector in the browser.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hivekit as hk  # noqa: E402

ROOT = HERE.parent


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", required=True)
    ap.add_argument("--imgsz", type=int, default=448)
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--eval", default=None, help="evaluate.py JSON to store the measured accuracy")
    ap.add_argument("--version", default="v1")
    args = ap.parse_args()
    from ultralytics import YOLO
    models = ROOT / "models"
    models.mkdir(exist_ok=True)
    pt = models / "biobuzz_hive.pt"
    shutil.copyfile(args.weights, pt)
    onnx_path = Path(YOLO(str(pt)).export(format="onnx", imgsz=args.imgsz, opset=17, simplify=True, dynamic=False))
    target = models / "biobuzz_hive.onnx"
    if onnx_path.resolve() != target.resolve():
        shutil.move(str(onnx_path), target)
    # verify: ONNX output == PyTorch output on a real test photo
    import detect as dt
    test = sorted((ROOT / "dataset" / "yolo" / "images" / "real_test").glob("*.jpg"))[:3]
    on, ul = dt.load_model(target, args.imgsz), dt.load_model(pt, args.imgsz)
    diffs = []
    for p in test:
        img = dt.load_image(p)
        a, b = on(img, conf=0.3), ul(img, conf=0.3)
        if a and b:
            ka, kb = a[0]["kpts"][:, :2], b[0]["kpts"][:, :2]
            diffs.append(float(np.abs(ka - kb).max()))
    meta = {
        "version": args.version, "imgsz": args.imgsz, "conf": args.conf, "classes": ["red_cell", "blue_cell"],
        "corner_order": hk.CORNER_IDS, "line_names": hk.MODEL.line_names,
        "onnx_vs_pytorch_max_px": round(max(diffs), 3) if diffs else None,
    }
    if args.eval and Path(args.eval).exists():
        meta["accuracy_on_held_out_real_photos"] = json.loads(Path(args.eval).read_text())["summary"]
    (models / "biobuzz_hive.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    site = ROOT / "docs" / "models"
    site.mkdir(exist_ok=True)
    shutil.copyfile(target, site / "biobuzz_hive.onnx")
    shutil.copyfile(models / "biobuzz_hive.json", site / "biobuzz_hive.json")
    print(json.dumps({"onnx": str(target), "size_mb": round(target.stat().st_size / 1e6, 1), **meta}, ensure_ascii=False))


if __name__ == "__main__":
    main()
