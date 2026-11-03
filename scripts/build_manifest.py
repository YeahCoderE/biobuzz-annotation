from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
LINES = [
    ("שמאל קדמי", "הדופן האדומה בצד שמאל, מהקצה הקדמי אל הקצה התחתון."),
    ("שמאל אחורי", "הדופן האדומה בצד שמאל, מהקצה האחורי אל הקצה התחתון."),
    ("ימין קדמי", "הדופן האדומה בצד ימין, מהקצה הקדמי אל הקצה התחתון."),
    ("ימין אחורי", "הדופן האדומה בצד ימין, מהקצה האחורי אל הקצה התחתון."),
    ("תחתון קדמי", "השפה האדומה התחתונה בצד הקדמי של המסגרת."),
    ("תחתון אחורי", "השפה האדומה התחתונה בצד האחורי של המסגרת."),
    ("עליון קדמי", "השפה האדומה העליונה בצד הקדמי של המסגרת."),
    ("עליון אחורי", "השפה האדומה העליונה בצד האחורי של המסגרת."),
    ("פינה עליונה שמאלית", "החיבור האדום העליון בפינה השמאלית."),
    ("פינה עליונה ימנית", "החיבור האדום העליון בפינה הימנית."),
    ("ברזל תחתון — צד שמאל", "מוט הברזל התחתון משמאל; אם מוסתר, סמנו את הקטעים הגלויים בלבד."),
    ("ברזל תחתון — צד ימין", "מוט הברזל התחתון מימין; אם מוסתר, סמנו את הקטעים הגלויים בלבד."),
    ("ברזל צד שמאל", "מוט הברזל בצד שמאל של המסגרת."),
    ("ברזל צד ימין", "מוט הברזל בצד ימין של המסגרת."),
]

IMAGE_DIR = ROOT / "docs" / "images"
OUTPUT = ROOT / "docs" / "images.json"


def main() -> None:
    images = []
    for path in sorted(IMAGE_DIR.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            images.append({
                "name": path.name,
                "width": image.width,
                "height": image.height,
            })
    payload = {
        "images": images,
        "lines": [
            {"name": name, "explanation": explanation}
            for name, explanation in LINES
        ],
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(images)} images to {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
