"""Local-first BioBuzz annotation review UI."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw, ImageFont, ImageOps


PROJECT_DIR = Path(__file__).resolve().parent
IMAGE_DIR = PROJECT_DIR / "images"
DATASET_DIR = PROJECT_DIR / "dataset"
TRUSTED_ANNOTATIONS = DATASET_DIR / "annotations.json"
REVIEW_ANNOTATIONS = DATASET_DIR / "annotations_review.json"
PREVIEW_DIR = IMAGE_DIR / "manual_review"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
LINES_PER_HIVE = 14
MAX_HIVES = 4
VALUES_PER_HIVE = 60
OUTPUT_SIZE = VALUES_PER_HIVE * MAX_HIVES
LINE_NAMES = [
    "שמאל קדמי",
    "שמאל אחורי",
    "ימין קדמי",
    "ימין אחורי",
    "תחתון קדמי",
    "תחתון אחורי",
    "עליון קדמי",
    "עליון אחורי",
    "פינה עליונה שמאלית",
    "פינה עליונה ימנית",
    "ברזל תחתון — צד שמאל",
    "ברזל תחתון — צד ימין",
    "ברזל צד שמאל",
    "ברזל צד ימין",
]
LINE_EXPLANATIONS = [
    ("הקו האדום האנכי בפינה השמאלית של הצד שפונה למצלמה.",
     "הפינה העליונה השמאלית בצד הקרוב", "הפינה התחתונה השמאלית בצד הקרוב"),
    ("הקו האדום האנכי בפינה השמאלית של הצד הרחוק מהמצלמה.",
     "הפינה העליונה השמאלית בצד הרחוק", "הפינה התחתונה השמאלית בצד הרחוק"),
    ("הקו האדום האנכי בפינה הימנית של הצד שפונה למצלמה.",
     "הפינה העליונה הימנית בצד הקרוב", "הפינה התחתונה הימנית בצד הקרוב"),
    ("הקו האדום האנכי בפינה הימנית של הצד הרחוק מהמצלמה.",
     "הפינה העליונה הימנית בצד הרחוק", "הפינה התחתונה הימנית בצד הרחוק"),
    ("הקו האדום התחתון לרוחב הצד שפונה למצלמה.",
     "הפינה התחתונה השמאלית הקרובה", "הפינה התחתונה הימנית הקרובה"),
    ("הקו האדום התחתון לרוחב הצד הרחוק מהמצלמה.",
     "הפינה התחתונה השמאלית הרחוקה", "הפינה התחתונה הימנית הרחוקה"),
    ("הקו האדום העליון לרוחב הצד שפונה אליכם — הצד הקרוב למצלמה. הוא מחבר בין הפינות העליונות הקרובות; אל תבחרו את הקו המקביל שמאחור.",
     "הפינה העליונה השמאלית הקרובה אליכם", "הפינה העליונה הימנית הקרובה אליכם"),
    ("הקו האדום העליון המקביל, בצד הרחוק מכם — מאחורי המסגרת. הוא מחבר בין שתי הפינות העליונות הרחוקות.",
     "הפינה העליונה השמאלית הרחוקה מכם", "הפינה העליונה הימנית הרחוקה מכם"),
    ("הקו הקצר בחלק העליון שמחבר את הפינה השמאלית הקרובה אליכם לפינה השמאלית הרחוקה — בכיוון העומק.",
     "הפינה העליונה השמאלית הקרובה אליכם", "הפינה העליונה השמאלית הרחוקה מכם"),
    ("הקו הקצר בחלק העליון שמחבר את הפינה הימנית הקרובה אליכם לפינה הימנית הרחוקה — בכיוון העומק.",
     "הפינה העליונה הימנית הקרובה אליכם", "הפינה העליונה הימנית הרחוקה מכם"),
    ("מוט הברזל התחתון משמאל; סמנו את שני קצות המוט, ואם חלק ממנו מוסתר סמנו את הקצוות הגלויים בלבד.",
     "קצה גלוי אחד של המוט", "הקצה הגלוי השני של המוט"),
    ("מוט הברזל התחתון מימין; סמנו את שני קצות המוט, ואם חלק ממנו מוסתר סמנו את הקצוות הגלויים בלבד.",
     "קצה גלוי אחד של המוט", "הקצה הגלוי השני של המוט"),
    ("מוט הברזל בצד שמאל של המסגרת. סמנו את שני הקצוות הנראים שלו.",
     "קצה גלוי אחד של המוט", "הקצה הגלוי השני של המוט"),
    ("מוט הברזל בצד ימין של המסגרת. סמנו את שני הקצוות הנראים שלו.",
     "קצה גלוי אחד של המוט", "הקצה הגלוי השני של המוט"),
]


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as file:
        records = json.load(file)
    if not isinstance(records, list):
        raise ValueError(f"{path.name} must contain a JSON list.")
    return records


def image_path_for_name(name: str) -> Path:
    if Path(name).name != name or Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError("Invalid image name.")
    path = (IMAGE_DIR / name).resolve()
    if path.parent != IMAGE_DIR.resolve() or not path.is_file():
        raise FileNotFoundError(f"Image not found: {name}")
    return path


def image_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        return image.width, image.height


def record_by_name(records: list[dict]) -> dict[str, dict]:
    return {
        Path(record.get("image_path", "")).name: record
        for record in records
        if record.get("image_path")
    }


def record_hives(record: dict | None, width: int, height: int) -> list[dict]:
    if not record:
        return []
    label = record.get("label")
    if not isinstance(label, list) or len(label) != OUTPUT_SIZE:
        return []
    hive_count = max(0, min(MAX_HIVES, int(record.get("hive_count", 0))))
    hives = []
    for hive_index in range(hive_count):
        offset = hive_index * VALUES_PER_HIVE
        metadata = label[offset + 1:offset + 4]
        coordinates = label[offset + 4:offset + VALUES_PER_HIVE]
        if len(metadata) != 3 or len(coordinates) != 56:
            continue
        points = []
        normalized = all(
            isinstance(value, (int, float)) and 0 <= value <= 1
            for value in coordinates
        )
        for point_index in range(0, len(coordinates), 2):
            x, y = coordinates[point_index:point_index + 2]
            if normalized:
                x, y = x * width, y * height
            points.append([round(x), round(y)])
        if (
            max(point[0] for point in points) - min(point[0] for point in points) < max(20, width * 0.03)
            or max(point[1] for point in points) - min(point[1] for point in points) < max(20, height * 0.03)
        ):
            continue
        hives.append({"metadata": metadata, "points": points})
    return hives


def make_label(hives: list[dict], width: int, height: int) -> list[float]:
    if not isinstance(hives, list) or not hives or len(hives) > MAX_HIVES:
        raise ValueError(f"Choose between 1 and {MAX_HIVES} frames.")
    label = [0.0] * OUTPUT_SIZE
    for hive_index, hive in enumerate(hives):
        if not isinstance(hive, dict):
            raise ValueError(f"Frame {hive_index + 1} must be an object.")
        points = hive.get("points")
        metadata = hive.get("metadata")
        if not isinstance(points, list) or len(points) != LINES_PER_HIVE * 2:
            raise ValueError(f"Frame {hive_index + 1} must have 28 endpoints.")
        if not isinstance(metadata, list) or len(metadata) != 3:
            raise ValueError(f"Frame {hive_index + 1} has invalid metadata.")
        if any(value not in (0, 1, False, True) for value in metadata):
            raise ValueError(f"Frame {hive_index + 1} metadata must use 0 or 1.")
        offset = hive_index * VALUES_PER_HIVE
        label[offset] = 1.0
        label[offset + 1:offset + 4] = [
            1.0 if bool(value) else 0.0 for value in metadata
        ]
        for point_index, point in enumerate(points):
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError(f"Endpoint {point_index + 1} is missing.")
            x, y = point
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                raise ValueError(f"Endpoint {point_index + 1} must be a number.")
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError(f"Endpoint {point_index + 1} is invalid.")
            if not (0 <= x < width and 0 <= y < height):
                raise ValueError(f"Endpoint {point_index + 1} is outside the image.")
            coordinate_offset = offset + 4 + point_index * 2
            label[coordinate_offset] = x / width
            label[coordinate_offset + 1] = y / height
    return label


def write_preview(image_path: Path, hives: list[dict], output_path: Path) -> None:
    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    width, height = image.size
    for hive_index, hive in enumerate(hives):
        points = hive["points"]
        for line_index in range(LINES_PER_HIVE):
            start = points[line_index * 2]
            end = points[line_index * 2 + 1]
            color = (255, 165, 0) if line_index >= 10 else (0, 255, 0)
            draw.line((tuple(start), tuple(end)), fill=color, width=5)
            for x, y in (start, end):
                draw.ellipse((x - 8, y - 8, x + 8, y + 8), fill=(0, 0, 255))
            midpoint = ((start[0] + end[0]) // 2, (start[1] + end[1]) // 2)
            draw.text(
                midpoint,
                f"{hive_index + 1}.{line_index + 1}" if len(hives) > 1 else str(line_index + 1),
                font=font,
                fill=color,
                stroke_width=2,
                stroke_fill=(0, 0, 0),
            )
    draw.rectangle((0, 0, width, 48), fill=(25, 25, 25))
    draw.text((12, 15), "MANUAL DRAFT - REVIEW BEFORE TRAINING", font=font, fill=(255, 255, 0))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, quality=95)


class BioBuzzHandler(BaseHTTPRequestHandler):
    server_version = "BioBuzzLocal/1.0"

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def send_json(self, payload: dict, status: int = 200) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_file(PROJECT_DIR / "docs" / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/app.css":
            self.send_file(PROJECT_DIR / "docs" / "app.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self.send_file(PROJECT_DIR / "docs" / "app.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self.send_state()
            return
        if parsed.path == "/api/image":
            query = parse_qs(parsed.query)
            try:
                name = query.get("name", [""])[0]
                self.send_file(image_path_for_name(name))
            except (ValueError, FileNotFoundError) as exc:
                self.send_json({"error": str(exc)}, 404)
            return
        if parsed.path == "/api/preview":
            query = parse_qs(parsed.query)
            name = query.get("name", [""])[0]
            path = (PREVIEW_DIR / name).resolve()
            if path.parent != PREVIEW_DIR.resolve() or path.suffix.lower() != ".jpg" or not path.is_file():
                self.send_json({"error": "Preview not found."}, 404)
                return
            self.send_file(path)
            return
        if parsed.path == "/api/export":
            content = json.dumps(
                read_records(REVIEW_ANNOTATIONS),
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="annotations_review.json"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_json({"error": "Not found."}, 404)

    def send_state(self) -> None:
        images = sorted(
            path for path in IMAGE_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        trusted = record_by_name(read_records(TRUSTED_ANNOTATIONS))
        review = record_by_name(read_records(REVIEW_ANNOTATIONS))
        payload_images = []
        for path in images:
            width, height = image_dimensions(path)
            draft = review.get(path.name)
            trusted_record = trusted.get(path.name)
            draft_hives = record_hives(draft, width, height) if draft else []
            trusted_hives = record_hives(trusted_record, width, height) if trusted_record else []
            preview_path = Path(draft.get("preview_path", "")) if draft else Path()
            preview_available = (
                bool(draft)
                and preview_path.parent.resolve() == PREVIEW_DIR.resolve()
                and preview_path.is_file()
            )
            payload_images.append({
                "name": path.name,
                "width": width,
                "height": height,
                "status": (
                    "invalid" if draft and not draft_hives
                    else "review" if draft
                    else "trusted" if trusted_record else "todo"
                ),
                "hives": draft_hives or trusted_hives,
                "review_status": draft.get("review_status") if draft else None,
                "preview_url": (
                    f"/api/preview?name={preview_path.name}"
                    if preview_available else None
                ),
            })
        self.send_json({
            "images": payload_images,
            "total": len(payload_images),
            "drafts": len(review),
            "trusted": len(trusted),
            "lines": [
                {
                    "name": name,
                    "explanation": explanation,
                    "start": start,
                    "end": end,
                }
                for name, (explanation, start, end) in zip(LINE_NAMES, LINE_EXPLANATIONS)
            ],
        })

    def send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.is_file():
            self.send_json({"error": "File not found."}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/save", "/api/import"):
            self.send_json({"error": "Not found."}, 404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= 2_000_000:
                raise ValueError("Request body must be between 1 byte and 2 MB.")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if path == "/api/import":
                self.import_drafts(payload)
                return
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            self.save_draft(payload)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except OSError as exc:
            self.send_json({"error": f"Could not save draft: {exc}"}, 500)

    def import_drafts(self, payload: object) -> None:
        if not isinstance(payload, list):
            raise ValueError("Draft import must be a JSON list.")
        records = read_records(REVIEW_ANNOTATIONS)
        imported = 0
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("Each imported draft must be an object.")
            image_path = item.get("image_path")
            if not isinstance(image_path, str):
                raise ValueError("Each imported draft needs an image_path.")
            source = image_path_for_name(Path(image_path).name)
            label = item.get("label")
            if not isinstance(label, list) or len(label) != OUTPUT_SIZE:
                raise ValueError(f"{source.name} has an invalid label shape.")
            hive_count = int(item.get("hive_count", 0))
            if not 1 <= hive_count <= MAX_HIVES:
                raise ValueError(f"{source.name} has an invalid frame count.")
            width, height = image_dimensions(source)
            hives = record_hives(item, width, height)
            if len(hives) != hive_count:
                raise ValueError(f"{source.name} has invalid or empty frame coordinates.")
            preview = PREVIEW_DIR / f"{source.stem}_manual_draft.jpg"
            write_preview(source, hives, preview)
            record = {
                **item,
                "image_path": str(source.resolve()),
                "review_status": "awaiting_user_approval",
                "annotation_method": "manual_user_review_ui_not_model_prediction",
                "preview_path": str(preview.resolve()),
            }
            for index, existing in enumerate(records):
                if Path(existing.get("image_path", "")).name == source.name:
                    records[index] = record
                    break
            else:
                records.append(record)
            imported += 1
        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        temporary_path = REVIEW_ANNOTATIONS.with_suffix(".json.tmp")
        temporary_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary_path, REVIEW_ANNOTATIONS)
        self.send_json({"ok": True, "imported": imported})

    def save_draft(self, payload: dict) -> None:
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValueError("Image name is required.")
        source = image_path_for_name(name)
        width, height = image_dimensions(source)
        hives = payload.get("hives")
        label = make_label(hives, width, height)
        record = {
            "image_path": str(source.resolve()),
            "label": label,
            "hive_count": len(hives),
            "calibration": None,
            "line_names": LINE_NAMES,
            "review_status": "awaiting_user_approval",
            "annotation_method": "manual_user_review_ui_not_model_prediction",
            "review_note": "Saved in the review-only UI. Not merged into training annotations.",
        }
        records = read_records(REVIEW_ANNOTATIONS)
        found = False
        for index, current in enumerate(records):
            if Path(current.get("image_path", "")).name == name:
                records[index] = record
                found = True
                break
        if not found:
            records.append(record)
        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        preview = PREVIEW_DIR / f"{source.stem}_manual_draft.jpg"
        write_preview(source, hives, preview)
        record["preview_path"] = str(preview.resolve())
        temporary_path = REVIEW_ANNOTATIONS.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, REVIEW_ANNOTATIONS)
        self.send_json({
            "ok": True,
            "status": record["review_status"],
            "preview": f"/api/preview?name={preview.name}",
        })


def run_server(host: str, port: int, open_browser: bool) -> None:
    server = ThreadingHTTPServer((host, port), BioBuzzHandler)
    address = f"http://{host}:{server.server_port}"
    print("BioBuzz local review UI")
    print(f"Open: {address}")
    print("Images stay on this computer; drafts are saved separately from training labels.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping BioBuzz UI.")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local BioBuzz annotation UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        parser.error("For privacy, the UI only binds to this computer.")
    run_server(args.host, args.port, not args.no_browser)


if __name__ == "__main__":
    main()
