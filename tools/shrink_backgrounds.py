"""Copy dataset/backgrounds/**.jpg to dataset/backgrounds_small at <= 800 px wide (fast decoding)."""
import sys
from pathlib import Path
import cv2
ROOT = Path(__file__).resolve().parent.parent
src, dst = ROOT / "dataset" / "backgrounds", ROOT / "dataset" / "backgrounds_small"
n = 0
for p in src.rglob("*.jpg"):
    out = dst / p.parent.name / p.name
    if out.exists():
        continue
    img = cv2.imread(str(p))
    if img is None:
        continue
    h, w = img.shape[:2]
    if w > 800:
        img = cv2.resize(img, (800, int(round(h * 800 / w))), interpolation=cv2.INTER_AREA)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    n += 1
print("shrunk", n)
