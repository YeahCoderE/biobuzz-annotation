"""Build docs/images.json and small thumbnails for the annotation site.

Line definitions live in docs/data/hive_model.json (single source of truth, matching
LINES_INPUT_HE in main.py). They are copied into images.json for older tools.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
IMAGE_DIR = ROOT / "docs" / "images"
THUMB_DIR = ROOT / "docs" / "thumbs"
MODEL_FILE = ROOT / "docs" / "data" / "hive_model.json"
OUTPUT = ROOT / "docs" / "images.json"
THUMB_SIZE = (240, 180)


def load_lines() -> list[dict]:
    model = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
    corners = model["corners"]

    def corner_text(corner_id: str) -> str:
        info = corners[corner_id]
        return f"{info['name']} · מסגרת {info['frame']}"

    return [
        {
            "id": line["id"],
            "name": line["name"],
            "explanation": line["explanation"],
            "start": corner_text(line["a"]),
            "end": corner_text(line["b"]),
            "a": line["a"],
            "b": line["b"],
            "kind": line["kind"],
        }
        for line in model["lines"]
    ]


def make_thumbnail(path: Path, image: Image.Image) -> str:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumb_name = f"{path.stem}_{path.suffix[1:].lower()}.jpg"
    thumb_path = THUMB_DIR / thumb_name
    if not thumb_path.exists() or thumb_path.stat().st_mtime < path.stat().st_mtime:
        thumb = image.convert("RGB")
        thumb.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        thumb.save(thumb_path, "JPEG", quality=78, optimize=True)
    return thumb_name


def main() -> None:
    images = []
    expected_thumbs = set()
    for path in sorted(IMAGE_DIR.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            thumb = make_thumbnail(path, image)
            expected_thumbs.add(thumb)
            images.append({"name": path.name, "width": image.width, "height": image.height, "thumb": thumb})
    if THUMB_DIR.exists():
        for stale in THUMB_DIR.glob("*.jpg"):
            if stale.name not in expected_thumbs:
                stale.unlink()
    payload = {"images": images, "lines": load_lines()}
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(images)} images and thumbnails to {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
