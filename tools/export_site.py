#!/usr/bin/env python
"""Write the verified photo labels into the annotation site (docs/data/annotations.json).

    .venv/Scripts/python -I tools/export_site.py [--detections runs/detect_web]

Only cells that passed the visual review are exported (the same rules as build_yolo.py).
Points are not marked as estimated: every corner comes from the 3D cell fitted to the photo.
Optionally adds detector results (JSON files written by tools/detect.py) for other images,
marked as estimated so they show dashed in the site until someone reviews them.
"""
import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hivekit as hk  # noqa: E402
from build_yolo import DROP, PARTIAL  # noqa: E402

ROOT = HERE.parent
OUT = ROOT / "docs" / "data" / "annotations.json"


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--detections", nargs="*", default=[])
    args = ap.parse_args()
    stamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
    records = []
    for path in sorted((ROOT / "dataset" / "labels").glob("*.json")):
        if path.stem in DROP or path.stem in PARTIAL:
            continue
        label = json.loads(path.read_text(encoding="utf-8"))
        if not label["image"].startswith("docs/images/"):
            continue
        cells = [c for c in label["cells"] if "camera" in c]
        if path.stem in PARTIAL:
            cells = [c for c in cells if c.get("fit", {}).get("iou_roi", 0) >= 0.35]
        if not cells:
            continue
        rec = hk.label_to_record({**label, "cells": cells}, image_path=Path(label["image"]).name)
        rec.update(estimated_points=[[] for _ in range(rec["hive_count"])], review_status="fitted_3d_model",
                   annotation_method="claude: rough box + 3D cell fitted to the rib pixels, visually checked",
                   annotator="Claude", saved_at=stamp)
        records.append(rec)
    for d in args.detections:
        for f in sorted(Path(d).glob("*.json")):
            res = json.loads(f.read_text(encoding="utf-8"))
            rec = res["record"]
            name = Path(res["image"]).name
            if not (ROOT / "docs" / "images" / name).exists() or not rec["hive_count"]:
                continue
            if any(Path(r["image_path"]).name == name for r in records):
                continue
            rec.update(image_path=name, estimated_points=[list(range(28)) for _ in range(rec["hive_count"])],
                       review_status="model_prediction", annotation_method="biobuzz detector (not reviewed)",
                       annotator="BioBuzz model", saved_at=stamp)
            records.append(rec)
    OUT.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"records": len(records), "cells": int(sum(r["hive_count"] for r in records)), "file": str(OUT)}))


if __name__ == "__main__":
    main()
