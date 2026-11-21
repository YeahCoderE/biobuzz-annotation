#!/usr/bin/env python
"""Draw the app icon (amber hexagon with a camera lens) and write the .ico for the PC app and
the launcher PNGs for the Android app. Run with the pc\\.venv python (needs Pillow)."""
import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent


def draw(size: int) -> Image.Image:
    s = size * 4  # supersample
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=(20, 23, 28, 255))
    cx = cy = s / 2
    r = s * 0.40
    hexa = [(cx + r * math.cos(math.radians(60 * i + 30)), cy + r * math.sin(math.radians(60 * i + 30))) for i in range(6)]
    d.polygon(hexa, fill=(245, 165, 36, 255))
    for rad, col in ((0.20, (20, 23, 28, 255)), (0.14, (60, 72, 90, 255)), (0.05, (200, 215, 235, 255))):
        d.ellipse([cx - s * rad, cy - s * rad, cx + s * rad, cy + s * rad], fill=col)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    (ROOT / "pc").mkdir(exist_ok=True)
    draw(256).save(ROOT / "pc" / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    draw(128).save(ROOT / "pc" / "icon.png")
    res = ROOT / "android" / "app" / "src" / "main" / "res"
    for folder, px in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)):
        out = res / f"mipmap-{folder}"
        out.mkdir(parents=True, exist_ok=True)
        draw(px).save(out / "ic_launcher.png")
    print("icons written")


if __name__ == "__main__":
    main()
