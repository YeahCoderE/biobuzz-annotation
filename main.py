"""
BioBuzz ML - labeling, editing, metric calibration, training and ONNX inference.

Install:
    pip install opencv-python numpy pillow torch onnx onnxruntime

Run:
    python main.py

Important:
- The model predicts 4 hive slots. Each slot has 60 values:
  presence, color, side, top, and 28 normalized points (56 coordinates).
- 14 lines are labeled per hive; each line has two endpoints.
- Calibration converts pixel distances to a real-world unit using the known
  perpendicular distance between the two forward edge lines. This is a local
  scale estimate, not a full perspective correction for a 3D object.
"""

import json
import importlib.util
import math
import random
import sys
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

import cv2
import numpy as np
from PIL import Image, ImageOps

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, random_split


# ============================================================
# Configuration
# ============================================================

IMAGE_SIZE = 256
MAX_HIVES = 4
LINES_PER_HIVE = 14
POINTS_PER_LINE = 2
COORDINATES_PER_HIVE = LINES_PER_HIVE * POINTS_PER_LINE * 2  # 56
VALUES_PER_HIVE = 1 + 3 + COORDINATES_PER_HIVE  # presence + metadata + points = 60
OUTPUT_SIZE = MAX_HIVES * VALUES_PER_HIVE  # 240

BATCH_SIZE = 8
EPOCHS = 60
LEARNING_RATE = 1e-4
VALIDATION_FRACTION = 0.2
RANDOM_SEED = 42
PRESENCE_THRESHOLD = 0.5

PROJECT_DIR = Path(__file__).resolve().parent
DATASET_DIR = PROJECT_DIR / "dataset"
ANNOTATIONS_FILE = DATASET_DIR / "annotations.json"
MODEL_FILE = PROJECT_DIR / "BioBuzzPoseNet.pth"
ONNX_FILE = PROJECT_DIR / "BioBuzzPoseNet.onnx"

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"
}

# The two added bottom connection lines are included before the iron connectors.
LINES_INPUT_HE = [
    "קו קצה שמאלי - קדימה",          # 0
    "קו קצה שמאלי - אחורה",          # 1
    "קו קצה ימני - קדימה",           # 2
    "קו קצה ימני - אחורה",           # 3
    "קו תחתון - קדימה",              # 4
    "קו תחתון - אחורה",              # 5
    "קו קצה למעלה ימני - קדימה",     # 6
    "קו קצה למעלה ימני - אחורה",     # 7
    "קו קצה למעלה שמאלי - קדימה",    # 8
    "קו קצה למעלה שמאלי - אחורה",    # 9
    "קו חיבור תחתון שמאל",            # 10
    "קו חיבור תחתון ימין",            # 11
    "קו חיבור ברזל שמאל",             # 12
    "קו חיבור ברזל ימין",             # 13
]

mouse_position = None
mouse_event = None
mouse_flags = 0


# ============================================================
# Image loading and letterbox preprocessing
# ============================================================

def load_any_image_format(file_path):
    pil_error = None
    try:
        with Image.open(file_path) as pil_image:
            rgb = np.array(ImageOps.exif_transpose(pil_image).convert("RGB"))
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except (OSError, ValueError) as exc:
        pil_error = exc

    image = cv2.imread(str(file_path), cv2.IMREAD_COLOR)
    if image is not None:
        return image

    print(f"Image loading error ({file_path}): {pil_error}")
    return None


def letterbox_image(image):
    """Resize without distortion and return image plus transform metadata."""
    height, width = image.shape[:2]
    scale = min(IMAGE_SIZE / width, IMAGE_SIZE / height)

    resized_width = max(1, int(round(width * scale)))
    resized_height = max(1, int(round(height * scale)))

    resized = cv2.resize(
        image,
        (resized_width, resized_height),
        interpolation=cv2.INTER_AREA
    )

    canvas = np.zeros((IMAGE_SIZE, IMAGE_SIZE, 3), dtype=np.uint8)
    pad_x = (IMAGE_SIZE - resized_width) // 2
    pad_y = (IMAGE_SIZE - resized_height) // 2
    canvas[pad_y:pad_y + resized_height, pad_x:pad_x + resized_width] = resized

    return canvas, {
        "scale": scale,
        "pad_x": pad_x,
        "pad_y": pad_y,
        "original_width": width,
        "original_height": height,
    }


def image_to_tensor(image):
    letterboxed, transform = letterbox_image(image)
    array = letterboxed.astype(np.float32) / 255.0
    array = np.transpose(array, (2, 0, 1))
    tensor = torch.from_numpy(np.ascontiguousarray(array))
    return tensor, transform


def original_to_model_xy(x, y, transform):
    model_x = (x * transform["scale"] + transform["pad_x"]) / IMAGE_SIZE
    model_y = (y * transform["scale"] + transform["pad_y"]) / IMAGE_SIZE
    return model_x, model_y


def model_to_original_xy(x, y, transform):
    original_x = (
        x * IMAGE_SIZE - transform["pad_x"]
    ) / transform["scale"]
    original_y = (
        y * IMAGE_SIZE - transform["pad_y"]
    ) / transform["scale"]
    return original_x, original_y


# ============================================================
# Mouse handling
# ============================================================

def mouse_callback(event, x, y, flags, param):
    global mouse_position, mouse_event, mouse_flags
    mouse_position = (x, y)
    mouse_flags = flags
    if event in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_LBUTTONUP, cv2.EVENT_MOUSEMOVE):
        mouse_event = event


# ============================================================
# Label and annotation data helpers
# ============================================================

def ask_hive_metadata(root, hive_number):
    color = simpledialog.askstring(
        "מאפייני כוורת",
        f"כוורת {hive_number}: צבע (blue/red):",
        parent=root
    )
    if color is None:
        return None
    color = color.strip().lower()
    if color not in ("blue", "red"):
        messagebox.showerror("קלט לא תקין", "יש להזין blue או red.")
        return None

    side = simpledialog.askstring(
        "מאפייני כוורת",
        f"כוורת {hive_number}: צד (left/right):",
        parent=root
    )
    if side is None:
        return None
    side = side.strip().lower()
    if side not in ("left", "right"):
        messagebox.showerror("קלט לא תקין", "יש להזין left או right.")
        return None

    vertical = simpledialog.askstring(
        "מאפייני כוורת",
        f"כוורת {hive_number}: מיקום אנכי (top/bottom):",
        parent=root
    )
    if vertical is None:
        return None
    vertical = vertical.strip().lower()
    if vertical not in ("top", "bottom"):
        messagebox.showerror("קלט לא תקין", "יש להזין top או bottom.")
        return None

    return [
        1.0 if color == "blue" else 0.0,
        1.0 if side == "right" else 0.0,
        1.0 if vertical == "top" else 0.0,
    ]


def empty_label():
    return np.zeros((OUTPUT_SIZE,), dtype=np.float32)


def pack_label(hive_data, image_shape=None):
    """
    hive_data: list of dicts, each with metadata and points.
    points: [line0_start, line0_end, ..., line13_start, line13_end]
    image_shape: optional image (height, width, ...) to normalize pixel points.
    """
    label = empty_label()

    for hive_index, hive in enumerate(hive_data[:MAX_HIVES]):
        offset = hive_index * VALUES_PER_HIVE
        label[offset] = 1.0
        label[offset + 1:offset + 4] = np.asarray(
            hive["metadata"], dtype=np.float32
        )

        flat_points = np.asarray(hive["points"], dtype=np.float32).reshape(-1)
        if flat_points.shape != (COORDINATES_PER_HIVE,):
            raise ValueError("Each hive must have 28 points (56 x/y values).")

        if image_shape is not None:
            height, width = image_shape[:2]
            if width <= 0 or height <= 0:
                raise ValueError("Image dimensions must be positive.")
            flat_points = flat_points.copy()
            flat_points[0::2] /= width
            flat_points[1::2] /= height

        label[offset + 4:offset + VALUES_PER_HIVE] = flat_points

    return label


def unpack_label(label, hive_count=None):
    label = np.asarray(label, dtype=np.float32)
    hives = []

    for hive_index in range(MAX_HIVES):
        offset = hive_index * VALUES_PER_HIVE
        present = label[offset] >= PRESENCE_THRESHOLD

        if hive_count is not None:
            present = hive_index < hive_count

        if not present:
            continue

        metadata = label[offset + 1:offset + 4].tolist()
        coordinates = label[offset + 4:offset + VALUES_PER_HIVE]
        points = coordinates.reshape(LINES_PER_HIVE * 2, 2).tolist()

        hives.append({
            "metadata": metadata,
            "points": points,
        })

    return hives


# ============================================================
# Interactive line editor
# ============================================================

def point_segment_distance(point, start, end):
    p = np.asarray(point, dtype=np.float32)
    a = np.asarray(start, dtype=np.float32)
    b = np.asarray(end, dtype=np.float32)

    ab = b - a
    denom = float(np.dot(ab, ab))
    if denom < 1e-8:
        return float(np.linalg.norm(p - a))

    t = float(np.clip(np.dot(p - a, ab) / denom, 0.0, 1.0))
    projection = a + t * ab
    return float(np.linalg.norm(p - projection))


def edit_hive_points(image, points, hive_number, manual=False, source=""):
    """Review one hive in a screen-fitted image with a clickable line dashboard."""
    global mouse_position, mouse_event, mouse_flags
    mouse_position = None
    mouse_event = None
    mouse_flags = 0

    points = np.asarray(points, dtype=np.float32).reshape(
        LINES_PER_HIVE * 2, 2
    ).copy()
    valid = np.full((LINES_PER_HIVE * 2,), not manual, dtype=bool)
    history = [(points.copy(), valid.copy())]
    history_index = 0

    screen_width, screen_height = 1600, 900
    root = tk._default_root
    if root is not None:
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()

    panel_width = min(420, max(340, screen_width // 4))
    max_image_width = max(320, screen_width - panel_width - 60)
    max_image_height = max(360, screen_height - 150)
    image_height, image_width = image.shape[:2]
    scale = min(1.0, max_image_width / image_width, max_image_height / image_height)
    display_width = max(1, int(round(image_width * scale)))
    display_height = max(1, int(round(image_height * scale)))
    scale_x = display_width / image_width
    scale_y = display_height / image_height
    panel_height = max(display_height, min(720, screen_height - 100))
    row_height = max(24, min(34, (panel_height - 245) // LINES_PER_HIVE))
    panel_x = display_width

    selected_line = 0
    selected_point = None
    drag_mode = None
    drag_start = None
    line_original = None
    redraw_line = None
    redraw_clicks = []
    status = "Click the highlighted endpoint in the image"
    next_point = int(np.flatnonzero(~valid)[0]) if manual else None
    window = f"BioBuzz - Hive {hive_number} review"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, display_width + panel_width, panel_height)
    cv2.setMouseCallback(window, mouse_callback)

    def save_history():
        nonlocal history, history_index
        history = history[:history_index + 1]
        history.append((points.copy(), valid.copy()))
        history_index += 1

    def first_unset_point(line_index=None):
        indexes = range(len(valid)) if line_index is None else range(
            line_index * 2, line_index * 2 + 2
        )
        return next((index for index in indexes if not valid[index]), None)

    try:
        while True:
            collecting = manual and not valid.all()
            if collecting:
                missing = first_unset_point()
                if missing is not None:
                    next_point = missing
                    selected_line = missing // 2
                    selected_point = missing

            display_image = cv2.resize(
                image, (display_width, display_height),
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
            )
            canvas = np.zeros(
                (panel_height, display_width + panel_width, 3),
                dtype=np.uint8
            )
            canvas[:display_height, :display_width] = display_image

            for line_index in range(LINES_PER_HIVE):
                first = line_index * 2
                if valid[first] and valid[first + 1]:
                    p1 = tuple(np.round(points[first] * [scale_x, scale_y]).astype(int))
                    p2 = tuple(np.round(points[first + 1] * [scale_x, scale_y]).astype(int))
                    color = (0, 165, 255) if line_index == selected_line else (0, 220, 0)
                    cv2.line(canvas, p1, p2, color, 3 if line_index == selected_line else 2, cv2.LINE_AA)

                for endpoint in (first, first + 1):
                    if valid[endpoint]:
                        point = tuple(np.round(points[endpoint] * [scale_x, scale_y]).astype(int))
                        color = (0, 0, 255) if endpoint == selected_point else (255, 80, 0)
                        cv2.circle(canvas, point, 5, color, -1, cv2.LINE_AA)

            cv2.rectangle(
                canvas, (panel_x, 0),
                (display_width + panel_width, panel_height), (30, 30, 30), -1
            )
            cv2.putText(
                canvas, f"HIVE {hive_number} | {source[:28]}",
                (panel_x + 14, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                (0, 255, 255), 2, cv2.LINE_AA
            )
            mode = (
                "MANUAL LABEL" if collecting
                else (
                    "MODEL REVIEW" if source.startswith("Model proposal")
                    else "MANUAL REVIEW"
                )
            )
            current_line_label = LINES_INPUT_HE[selected_line]
            cv2.putText(
                canvas, f"{mode} | LINE {selected_line + 1}/{LINES_PER_HIVE}",
                (panel_x + 14, 61), cv2.FONT_HERSHEY_SIMPLEX, 0.48,
                (255, 255, 255), 1, cv2.LINE_AA
            )
            cv2.putText(
                canvas, current_line_label[::-1],
                (panel_x + 14, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.43,
                (0, 200, 255), 1, cv2.LINE_AA
            )
            cv2.putText(
                canvas, status[:48],
                (panel_x + 14, 112), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                (210, 210, 210), 1, cv2.LINE_AA
            )

            rows_top = 132
            for line_index, line_name in enumerate(LINES_INPUT_HE):
                y1 = rows_top + line_index * row_height
                y2 = y1 + row_height - 2
                if y2 >= panel_height - 100:
                    break
                done = valid[line_index * 2:line_index * 2 + 2].all()
                background = (90, 75, 35) if line_index == selected_line else (48, 48, 48)
                cv2.rectangle(canvas, (panel_x + 10, y1), (display_width + panel_width - 10, y2), background, -1)
                marker = "OK" if done else "--"
                label = f"{line_index + 1:02d} {marker} {line_name[::-1]}"
                cv2.putText(
                    canvas, label[:42], (panel_x + 18, y1 + row_height - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (235, 235, 235), 1, cv2.LINE_AA
                )

            help_y = panel_height - 74
            help_lines = (
                (
                    "Click image for highlighted endpoint",
                    "Z undo | ENTER when all endpoints done | ESC cancel",
                )
                if collecting
                else (
                    "Drag endpoint/line | R redraw selected line",
                    "Z undo | Y redo | ENTER accept | ESC cancel",
                )
            )
            for index, text in enumerate(help_lines):
                cv2.putText(
                    canvas, text, (panel_x + 14, help_y + index * 23),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.39, (180, 180, 180), 1, cv2.LINE_AA
                )

            cv2.imshow(window, canvas)
            key = cv2.waitKey(15) & 0xFF

            if key == 27:
                return None
            if key in (10, 13):
                if valid.all():
                    return points
                status = "Complete all 28 endpoints before accepting"
            elif key in (ord("z"), ord("Z")):
                if history_index > 0:
                    history_index -= 1
                    points, valid = history[history_index][0].copy(), history[history_index][1].copy()
                    status = "Undo"
            elif key in (ord("y"), ord("Y")):
                if history_index < len(history) - 1:
                    history_index += 1
                    points, valid = history[history_index][0].copy(), history[history_index][1].copy()
                    status = "Redo"
            elif key in (ord("r"), ord("R")):
                if not collecting:
                    redraw_line = selected_line
                    redraw_clicks = []
                    status = "Click the two new endpoints in the image"
            elif key in (ord("a"), ord("A"), 81):
                selected_line = max(0, selected_line - 1)
            elif key in (ord("d"), ord("D"), 83):
                selected_line = min(LINES_PER_HIVE - 1, selected_line + 1)

            if mouse_event == cv2.EVENT_LBUTTONDOWN and mouse_position is not None:
                x, y = mouse_position
                if x >= panel_x:
                    row = (y - rows_top) // row_height
                    if 0 <= row < LINES_PER_HIVE:
                        selected_line = int(row)
                        selected_point = None
                        status = f"Selected line {selected_line + 1}"
                        if manual and not valid.all():
                            next_point = first_unset_point(selected_line)
                            if next_point is not None:
                                selected_point = next_point
                                status = f"Place endpoint {next_point % 2 + 1}"
                    mouse_event = None
                    continue

                original_x = float(np.clip(x / scale_x, 0, image_width - 1))
                original_y = float(np.clip(y / scale_y, 0, image_height - 1))
                click_point = np.array([original_x, original_y], dtype=np.float32)

                if collecting:
                    point_index = next_point
                    if point_index is not None:
                        points[point_index] = click_point
                        valid[point_index] = True
                        save_history()
                        next_point = first_unset_point()
                        if next_point is None:
                            status = "All lines labeled. Review or press ENTER."
                            selected_point = None
                        else:
                            selected_line = next_point // 2
                            selected_point = next_point
                            status = f"Click endpoint {next_point % 2 + 1}"
                    mouse_event = None
                    continue

                if redraw_line is not None:
                    redraw_clicks.append(click_point)
                    if len(redraw_clicks) == 2:
                        points[redraw_line * 2:redraw_line * 2 + 2] = redraw_clicks
                        valid[redraw_line * 2:redraw_line * 2 + 2] = True
                        save_history()
                        status = f"Line {redraw_line + 1} redrawn"
                        redraw_line = None
                        redraw_clicks = []
                    mouse_event = None
                    continue

                valid_indexes = np.flatnonzero(valid)
                if valid_indexes.size:
                    distances = np.linalg.norm(points[valid_indexes] - click_point, axis=1)
                    nearest_point = int(valid_indexes[int(np.argmin(distances))])
                else:
                    nearest_point = -1
                    distances = np.array([float("inf")])

                point_radius = max(10, 18 / max(scale, 0.01))
                if nearest_point >= 0 and distances.min() <= point_radius:
                    selected_point = nearest_point
                    selected_line = nearest_point // 2
                    drag_mode = "point"
                    drag_start = click_point
                    status = "Dragging endpoint"
                else:
                    candidate_lines = [
                        line_index for line_index in range(LINES_PER_HIVE)
                        if valid[line_index * 2:line_index * 2 + 2].all()
                    ]
                    line_distances = [
                        point_segment_distance(
                            click_point, points[line_index * 2],
                            points[line_index * 2 + 1]
                        )
                        for line_index in candidate_lines
                    ]
                    if line_distances and min(line_distances) <= max(8, 14 / max(scale, 0.01)):
                        selected_line = candidate_lines[int(np.argmin(line_distances))]
                        selected_point = None
                        drag_mode = "line"
                        drag_start = click_point
                        line_original = points[selected_line * 2:selected_line * 2 + 2].copy()
                        status = "Dragging whole line"
                    else:
                        selected_point = None
                        drag_mode = None
                        status = "Select a line in the dashboard or press R"
                mouse_event = None

            elif mouse_event == cv2.EVENT_MOUSEMOVE and drag_mode and mouse_position:
                x, y = mouse_position
                current = np.array([x / scale_x, y / scale_y], dtype=np.float32)
                if drag_mode == "point" and selected_point is not None:
                    points[selected_point] = current
                elif drag_mode == "line":
                    delta = current - drag_start
                    points[selected_line * 2:selected_line * 2 + 2] = line_original + delta
                mouse_event = None

            elif mouse_event == cv2.EVENT_LBUTTONUP and drag_mode:
                save_history()
                drag_mode = None
                status = "Edit saved"
                mouse_event = None
    finally:
        cv2.destroyWindow(window)


# ============================================================
# Metric calibration
# ============================================================

def perpendicular_distance_between_lines(line_a, line_b):
    """Mean perpendicular pixel separation of two line segments."""
    a1 = np.asarray(line_a[0], dtype=np.float64)
    a2 = np.asarray(line_a[1], dtype=np.float64)
    b1 = np.asarray(line_b[0], dtype=np.float64)
    b2 = np.asarray(line_b[1], dtype=np.float64)

    direction = a2 - a1
    length = np.linalg.norm(direction)
    if length < 1e-8:
        return 0.0

    unit_normal = np.array([-direction[1], direction[0]]) / length
    d1 = abs(float(np.dot(b1 - a1, unit_normal)))
    d2 = abs(float(np.dot(b2 - a1, unit_normal)))
    return (d1 + d2) / 2.0


def ask_calibration(root, image, hive_points):
    """
    Reference lines:
      line 1 (index 0): left edge - forward
      line 3 (index 2): right edge - forward
    """
    pixels = perpendicular_distance_between_lines(
        hive_points[0:2],
        hive_points[4:6]
    )

    if pixels <= 1:
        messagebox.showwarning(
            "Calibration unavailable",
            "The two forward edge lines are too close or invalid. "
            "Calibration was not saved."
        )
        return None

    distance = simpledialog.askfloat(
        "Metric calibration",
        "Enter the real distance BETWEEN:\n"
        "Left edge - forward and Right edge - forward.\n\n"
        "Use the perpendicular distance between these lines.",
        parent=root,
        minvalue=0.001
    )
    if distance is None:
        return None

    unit = simpledialog.askstring(
        "Calibration unit",
        "Unit: mm, cm, or in",
        parent=root,
        initialvalue="cm"
    )
    if unit is None:
        return None

    unit = unit.strip().lower()
    if unit not in ("mm", "cm", "in"):
        messagebox.showerror("Invalid unit", "Choose mm, cm, or in.")
        return None

    # Store the actual user-entered unit. Conversion to other units is done later.
    return {
        "reference_lines": [0, 2],
        "pixel_distance": float(pixels),
        "real_distance": float(distance),
        "unit": unit,
        "pixels_per_unit": float(pixels / distance),
    }


def pixels_to_units(pixel_distance, calibration, output_unit=None):
    if not calibration:
        return None

    source_unit = calibration["unit"]
    target_unit = output_unit or source_unit

    # Convert the calibration's entered distance to millimeters.
    to_mm = {"mm": 1.0, "cm": 10.0, "in": 25.4}
    real_distance_mm = calibration["real_distance"] * to_mm[source_unit]
    mm_per_pixel = real_distance_mm / calibration["pixel_distance"]

    target_units_per_mm = {"mm": 1.0, "cm": 0.1, "in": 1.0 / 25.4}
    return pixel_distance * mm_per_pixel * target_units_per_mm[target_unit]


# ============================================================
# Annotation file
# ============================================================

def load_annotations():
    if not ANNOTATIONS_FILE.exists():
        return []

    with ANNOTATIONS_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError("annotations.json must contain a JSON list.")
    return data


def save_annotations(records):
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    with ANNOTATIONS_FILE.open("w", encoding="utf-8") as file:
        json.dump(records, file, ensure_ascii=False, indent=2)


def add_or_replace_record(records, record):
    path_key = str(Path(record["image_path"]).resolve())
    for index, existing in enumerate(records):
        if str(Path(existing["image_path"]).resolve()) == path_key:
            records[index] = record
            return
    records.append(record)


# ============================================================
# Labeling flow
# ============================================================

DETECTOR_FILE = PROJECT_DIR / "models" / "biobuzz_hive.onnx"
if not DETECTOR_FILE.exists():
    DETECTOR_FILE = PROJECT_DIR / "docs" / "models" / "biobuzz_hive.onnx"
_detector = None


def detector_proposals(image):
    """Hive proposals from the trained BIOBUZZ detector (models/biobuzz_hive.onnx), if present.

    Returns the same structure as predict_with_onnx: metadata [blue, right, top], 28 points in
    original pixels (main.py line order) and a confidence, or None when the detector is missing.
    """
    global _detector
    if not DETECTOR_FILE.exists():
        return None
    try:
        sys.path.insert(0, str(PROJECT_DIR / "tools"))
        import detect as hive_detect
        if _detector is None:
            _detector = hive_detect.load_model(DETECTOR_FILE)
        cells = hive_detect.detect_image(_detector, image)
    except Exception as exc:  # noqa: BLE001
        print(f"Detector unavailable: {exc}")
        return None
    proposals = []
    for cell in cells[:MAX_HIVES]:
        points = [point for line in cell["lines"] for point in (line["a"], line["b"])]
        proposals.append({
            "metadata": [
                1.0 if cell["color"] == "blue" else 0.0,
                1.0 if cell["side"] == "right" else 0.0,
                1.0 if cell["level"] == "top" else 0.0,
            ],
            "points": points,
            "confidence": cell["confidence"],
        })
    return proposals


def label_single_image(image_path, root):
    image = load_any_image_format(image_path)
    if image is None:
        messagebox.showerror("Image error", f"Cannot load:\n{image_path}")
        return None

    predicted_hives = detector_proposals(image) or []
    if predicted_hives:
        pass
    elif MODEL_FILE.exists() and not ONNX_FILE.exists():
        try:
            export_onnx()
        except Exception as exc:
            messagebox.showerror(
                "Model export failed",
                f"A trained checkpoint exists, but could not be prepared for prediction:\n{exc}"
            )
            return None

    if not predicted_hives and ONNX_FILE.exists():
        try:
            predicted_hives = predict_with_onnx(
                image, include_best_candidate=True
            )
        except Exception as exc:
            messagebox.showerror(
                "Model prediction failed",
                f"The ONNX model could not label this image:\n{exc}"
            )
            return None

    suggested_count = max(1, min(MAX_HIVES, len(predicted_hives)))
    hive_count = simpledialog.askinteger(
        "כמות כוורות",
        "כמה כוורות מופיעות בתמונה? (1-4)",
        parent=root,
        minvalue=1,
        maxvalue=MAX_HIVES,
        initialvalue=suggested_count
    )
    if hive_count is None:
        return None

    hives = []
    for hive_index in range(hive_count):
        metadata = ask_hive_metadata(root, hive_index + 1)
        if metadata is None:
            return None

        has_prediction = hive_index < len(predicted_hives)
        if has_prediction:
            predicted = predicted_hives[hive_index]
            points = np.asarray(predicted["points"], dtype=np.float32)
            source = f"Model proposal {predicted['confidence']:.2f}"
        else:
            points = np.zeros((LINES_PER_HIVE * 2, 2), dtype=np.float32)
            source = "No model proposal - click endpoints"

        edited_points = edit_hive_points(
            image,
            points,
            hive_index + 1,
            manual=not has_prediction,
            source=source
        )
        if edited_points is None:
            return None

        hives.append({
            "metadata": metadata,
            "points": edited_points.tolist(),
        })

    label = pack_label(hives, image.shape)

    calibration = None
    if messagebox.askyesno(
        "Metric calibration",
        "Do you know the real distance between the two forward edge lines?",
        parent=root
    ):
        calibration = ask_calibration(root, image, np.asarray(hives[0]["points"]))

    return {
        "image_path": str(Path(image_path).resolve()),
        "label": label.tolist(),
        "hive_count": hive_count,
        "calibration": calibration,
        "line_names": LINES_INPUT_HE,
    }


def label_folder(root):
    folder = filedialog.askdirectory(
        title="Select folder containing BioBuzz images",
        parent=root
    )
    if not folder:
        return

    image_paths = sorted(
        path for path in Path(folder).iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not image_paths:
        messagebox.showwarning("No images", "No supported images found.")
        return

    records = load_annotations()
    reviewed = 0
    learned = 0

    for image_path in image_paths:
        answer = messagebox.askyesnocancel(
            "Label images",
            f"Review this image?\n\n{image_path.name}\n\n"
            "The model will propose lines when a trained model exists.\n"
            "Yes = review/correct, No = skip, Cancel = stop.",
            parent=root
        )
        if answer is None:
            break
        if not answer:
            continue

        record = label_single_image(image_path, root)
        if record is None:
            continue

        add_or_replace_record(records, record)
        save_annotations(records)
        reviewed += 1

        messagebox.showinfo(
            "Correction saved",
            "The corrected image was saved to the training dataset.\n"
            "The model will now learn from all saved corrections.",
            parent=root
        )
        root.update_idletasks()
        try:
            learn_from_annotations(records)
            learned += 1
        except Exception as exc:
            messagebox.showerror(
                "Learning failed",
                "The annotation was saved, but model training/export failed.\n\n"
                f"{exc}",
                parent=root
            )
            break

    messagebox.showinfo(
        "Review complete",
        f"Images reviewed: {reviewed}\n"
        f"Model updates completed: {learned}\n"
        f"Total records: {len(records)}\n\n"
        f"Saved: {ANNOTATIONS_FILE}"
    )


# ============================================================
# Dataset and CNN
# ============================================================

class BioBuzzDataset(Dataset):
    def __init__(self, records):
        self.records = records

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        record = self.records[index]
        image = load_any_image_format(record["image_path"])
        if image is None:
            raise RuntimeError(f"Could not load: {record['image_path']}")

        tensor, transform = image_to_tensor(image)
        label = np.asarray(record["label"], dtype=np.float32).copy()

        if label.shape != (OUTPUT_SIZE,):
            raise ValueError(
                f"Expected {OUTPUT_SIZE} target values, got {label.shape}. "
                "Old 204-value datasets must be relabeled for the 14-line model."
            )

        # Older annotations stored pixel coordinates; newer ones store normalized coordinates.
        for hive_index in range(MAX_HIVES):
            offset = hive_index * VALUES_PER_HIVE
            if label[offset] < 0.5:
                continue

            coordinate_start = offset + 4
            coords = label[coordinate_start:offset + VALUES_PER_HIVE].reshape(-1, 2)
            if np.any(coords > 1.0) or np.any(coords < 0.0):
                coords[:, 0] /= transform["original_width"]
                coords[:, 1] /= transform["original_height"]

            # Convert normalized original-image points into letterboxed model coordinates.
            for point_index, (x_norm, y_norm) in enumerate(coords):
                x = x_norm * transform["original_width"]
                y = y_norm * transform["original_height"]
                coords[point_index] = original_to_model_xy(x, y, transform)

            label[coordinate_start:offset + VALUES_PER_HIVE] = coords.reshape(-1)

        return tensor, torch.from_numpy(label)


class BioBuzzPoseNet(nn.Module):
    def __init__(self):
        super().__init__()

        self.encoder = nn.Sequential(
            nn.Conv2d(3, 16, kernel_size=3, stride=2, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(16, 32, kernel_size=3, stride=2, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 64, kernel_size=3, stride=2, padding=1),
            nn.ReLU(inplace=True),
            nn.Flatten(),
        )

        self.fc_layers = nn.Sequential(
            nn.Linear(64 * 32 * 32, 512),
            nn.ReLU(inplace=True),
            nn.Dropout(0.2),
            nn.Linear(512, OUTPUT_SIZE),
            nn.Sigmoid(),
        )

    def forward(self, x):
        return self.fc_layers(self.encoder(x))


def masked_pose_loss(predictions, targets):
    """
    Presence and metadata are trained for every hive slot.
    Coordinate loss is only applied to slots where a hive exists.
    """
    prediction_slots = predictions.reshape(-1, MAX_HIVES, VALUES_PER_HIVE)
    target_slots = targets.reshape(-1, MAX_HIVES, VALUES_PER_HIVE)

    presence_loss = nn.functional.binary_cross_entropy(
        prediction_slots[:, :, 0],
        target_slots[:, :, 0]
    )

    metadata_loss = nn.functional.mse_loss(
        prediction_slots[:, :, 1:4],
        target_slots[:, :, 1:4]
    )

    presence_mask = target_slots[:, :, 0:1]
    coordinate_errors = (prediction_slots[:, :, 4:] - target_slots[:, :, 4:]) ** 2
    coordinate_loss = (
        (coordinate_errors * presence_mask).sum()
        / (presence_mask.sum() * COORDINATES_PER_HIVE + 1e-8)
    )

    return presence_loss + metadata_loss + coordinate_loss


def run_training_epoch(model, loader, optimizer, device):
    model.train()
    total = 0.0

    for images, labels in loader:
        images = images.to(device)
        labels = labels.to(device)

        optimizer.zero_grad(set_to_none=True)
        predictions = model(images)
        loss = masked_pose_loss(predictions, labels)
        loss.backward()
        optimizer.step()

        total += loss.item() * images.size(0)

    return total / len(loader.dataset)


@torch.no_grad()
def run_validation(model, loader, device):
    model.eval()
    total = 0.0

    for images, labels in loader:
        images = images.to(device)
        labels = labels.to(device)
        total += masked_pose_loss(model(images), labels).item() * images.size(0)

    return total / len(loader.dataset)


def learn_from_annotations(records):
    """Fine-tune once on the reviewed dataset and refresh the ONNX model."""
    incompatible = [
        record for record in records
        if len(record.get("label", [])) != OUTPUT_SIZE
    ]
    if incompatible:
        raise ValueError(
            f"{len(incompatible)} annotation(s) use an older label format. "
            "Relabel or remove those records before training."
        )
    if not records:
        raise ValueError("There are no reviewed annotations to learn from.")

    dataset = BioBuzzDataset(records)
    loader = DataLoader(
        dataset,
        batch_size=BATCH_SIZE,
        shuffle=True,
        num_workers=0
    )
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if MODEL_FILE.exists():
        model = load_checkpoint_model().to(device)
        learning_rate = LEARNING_RATE * 0.1
    else:
        model = BioBuzzPoseNet().to(device)
        learning_rate = LEARNING_RATE

    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    loss = run_training_epoch(model, loader, optimizer, device)
    model.to("cpu")
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "image_size": IMAGE_SIZE,
            "output_size": OUTPUT_SIZE,
            "lines": LINES_INPUT_HE,
            "latest_training_loss": loss,
            "training_records": len(dataset),
        },
        MODEL_FILE
    )

    print(
        f"Incremental learning: {len(dataset)} images, "
        f"loss={loss:.6f}, device={device}"
    )
    export_onnx()


def train_model():
    if not ANNOTATIONS_FILE.exists():
        messagebox.showerror("Dataset missing", "Label images first.")
        return

    records = load_annotations()
    if len(records) < 5:
        messagebox.showerror(
            "Not enough data",
            "At least 5 labeled images are required to run this training "
            "split; this is only a technical minimum. A useful model usually "
            "needs many more varied images."
        )
        return

    # Avoid training with incompatible old annotations.
    incompatible = [
        record for record in records
        if len(record.get("label", [])) != OUTPUT_SIZE
    ]
    if incompatible:
        messagebox.showerror(
            "Old dataset format",
            f"{len(incompatible)} annotation(s) use the old format. "
            "Relabel those images with this 14-line version."
        )
        return

    dataset = BioBuzzDataset(records)
    validation_size = max(1, int(len(dataset) * VALIDATION_FRACTION))
    training_size = len(dataset) - validation_size

    train_set, validation_set = random_split(
        dataset,
        [training_size, validation_size],
        generator=torch.Generator().manual_seed(RANDOM_SEED)
    )

    train_loader = DataLoader(
        train_set, batch_size=BATCH_SIZE, shuffle=True, num_workers=0
    )
    validation_loader = DataLoader(
        validation_set, batch_size=BATCH_SIZE, shuffle=False, num_workers=0
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if MODEL_FILE.exists():
        model = load_checkpoint_model().to(device)
    else:
        model = BioBuzzPoseNet().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)

    best_loss = run_validation(model, validation_loader, device)
    print(f"Starting validation loss: {best_loss:.6f}")
    for epoch in range(EPOCHS):
        train_loss = run_training_epoch(model, train_loader, optimizer, device)
        validation_loss = run_validation(model, validation_loader, device)

        print(
            f"Epoch {epoch + 1:03d}/{EPOCHS} | "
            f"train={train_loss:.6f} | val={validation_loss:.6f}"
        )

        if validation_loss < best_loss:
            best_loss = validation_loss
            torch.save(
                {
                    "model_state_dict": model.state_dict(),
                    "image_size": IMAGE_SIZE,
                    "output_size": OUTPUT_SIZE,
                    "lines": LINES_INPUT_HE,
                    "training_records": len(dataset),
                    "best_validation_loss": best_loss,
                },
                MODEL_FILE
            )

    export_onnx()
    messagebox.showinfo(
        "Training complete",
        f"Best validation loss: {best_loss:.6f}\n\n"
        f"ONNX model:\n{ONNX_FILE}"
    )


# ============================================================
# ONNX export and inference
# ============================================================

def load_checkpoint_model():
    if not MODEL_FILE.exists():
        raise FileNotFoundError(
            "Trained model not found. Label images and train first."
        )

    checkpoint = torch.load(
        MODEL_FILE,
        map_location="cpu",
        weights_only=True
    )

    if checkpoint.get("output_size") != OUTPUT_SIZE:
        raise ValueError(
            "Checkpoint architecture does not match the current 14-line model. "
            "Train a new model."
        )

    model = BioBuzzPoseNet()
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()
    return model


def onnx_dependency_error():
    missing = [
        module for module in ("onnx", "onnxruntime")
        if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return None
    return (
        "Required ONNX packages are missing: "
        + ", ".join(missing)
        + f". Install them with:\n\"{sys.executable}\" -m pip install onnx onnxruntime"
    )


def ensure_onnx_model():
    dependency_error = onnx_dependency_error()
    if dependency_error:
        raise RuntimeError(dependency_error)
    if not MODEL_FILE.exists():
        raise FileNotFoundError(
            "There is no trained checkpoint yet. Correct and save at least one "
            "image first; the first proposals may be inaccurate until you add "
            "more corrected images."
        )
    if not ONNX_FILE.exists():
        export_onnx()


def model_status_text():
    dependency_error = onnx_dependency_error()
    if dependency_error:
        return "ONNX packages missing - install onnx and onnxruntime"
    if not MODEL_FILE.exists():
        return "No trained model yet - label the first image manually"

    record_count = "unknown"
    try:
        checkpoint = torch.load(
            MODEL_FILE, map_location="cpu", weights_only=True
        )
        record_count = str(checkpoint.get("training_records", "unknown"))
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        return f"Checkpoint cannot be read: {exc}"

    if not ONNX_FILE.exists():
        return f"Checkpoint found ({record_count} training images); ONNX export needed"
    if record_count.isdigit() and int(record_count) < 10:
        return (
            f"ONNX ready, but trained on only {record_count} labeled image(s). "
            "Expect mistakes; correct more images to improve it."
        )
    return f"ONNX ready | checkpoint trained on {record_count} image(s)"


def export_onnx():
    dependency_error = onnx_dependency_error()
    if dependency_error:
        raise RuntimeError(dependency_error)
    model = load_checkpoint_model()
    dummy = torch.zeros(1, 3, IMAGE_SIZE, IMAGE_SIZE, dtype=torch.float32)

    torch.onnx.export(
        model,
        dummy,
        str(ONNX_FILE),
        export_params=True,
        opset_version=17,
        do_constant_folding=True,
        input_names=["image"],
        output_names=["predictions"],
        dynamic_axes={
            "image": {0: "batch_size"},
            "predictions": {0: "batch_size"},
        },
        dynamo=False,
    )

    import onnx
    onnx_model = onnx.load(str(ONNX_FILE))
    onnx.checker.check_model(onnx_model)

    import onnxruntime as ort
    session = ort.InferenceSession(
        str(ONNX_FILE),
        providers=["CPUExecutionProvider"]
    )

    test_input = np.random.rand(
        1, 3, IMAGE_SIZE, IMAGE_SIZE
    ).astype(np.float32)

    with torch.no_grad():
        torch_output = model(torch.from_numpy(test_input)).numpy()

    onnx_output = session.run(
        ["predictions"],
        {"image": test_input}
    )[0]

    difference = float(np.max(np.abs(torch_output - onnx_output)))
    if onnx_output.shape != (1, OUTPUT_SIZE) or difference > 1e-4:
        raise RuntimeError(
            f"ONNX verification failed. Shape={onnx_output.shape}, "
            f"max difference={difference:.8f}"
        )

    print(f"ONNX export and runtime verification passed: {ONNX_FILE}")
    print(f"Output shape: {onnx_output.shape}")
    print(f"Max PyTorch/ONNX difference: {difference:.8f}")


def predict_with_onnx(image, include_best_candidate=False):
    ensure_onnx_model()

    import onnxruntime as ort

    letterboxed, transform = letterbox_image(image)
    array = letterboxed.astype(np.float32) / 255.0
    array = np.transpose(array, (2, 0, 1))[None, ...]

    session = ort.InferenceSession(
        str(ONNX_FILE),
        providers=["CPUExecutionProvider"]
    )

    output = session.run(
        ["predictions"],
        {"image": array.astype(np.float32)}
    )[0][0]

    slots = output.reshape(MAX_HIVES, VALUES_PER_HIVE)
    predicted_hives = []

    for hive_index, slot in enumerate(slots):
        if slot[0] < PRESENCE_THRESHOLD:
            continue

        metadata = slot[1:4].tolist()
        model_points = slot[4:].reshape(-1, 2)

        original_points = []
        for x, y in model_points:
            original_x, original_y = model_to_original_xy(x, y, transform)
            original_points.append([
                float(np.clip(original_x, 0, image.shape[1] - 1)),
                float(np.clip(original_y, 0, image.shape[0] - 1))
            ])

        predicted_hives.append({
            "metadata": metadata,
            "points": original_points,
            "confidence": float(slot[0]),
        })

    if include_best_candidate and not predicted_hives:
        hive_index = int(np.argmax(slots[:, 0]))
        slot = slots[hive_index]
        original_points = []
        for x, y in slot[4:].reshape(-1, 2):
            original_x, original_y = model_to_original_xy(x, y, transform)
            original_points.append([
                float(np.clip(original_x, 0, image.shape[1] - 1)),
                float(np.clip(original_y, 0, image.shape[0] - 1))
            ])
        predicted_hives.append({
            "metadata": slot[1:4].tolist(),
            "points": original_points,
            "confidence": float(slot[0]),
        })

    return predicted_hives


# ============================================================
# Prediction review / correction / save as new training data
# ============================================================

def review_prediction(image, hive_data, hive_index):
    points = np.asarray(hive_data["points"], dtype=np.float32)
    edited = edit_hive_points(
        image,
        points,
        hive_index + 1,
        source=f"Model proposal {hive_data['confidence']:.2f}"
    )
    if edited is None:
        return None

    hive_data["points"] = edited.tolist()
    return hive_data


def try_model(root):
    if not DETECTOR_FILE.exists():
        try:
            ensure_onnx_model()
        except Exception as exc:
            messagebox.showerror("ONNX model unavailable", str(exc), parent=root)
            return

    image_path = filedialog.askopenfilename(
        title="Select an image to test the ONNX model",
        filetypes=[
            ("Image files", "*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff")
        ],
        parent=root
    )
    if not image_path:
        return

    image = load_any_image_format(image_path)
    if image is None:
        messagebox.showerror("Image error", "Could not load the image.")
        return

    try:
        predicted_hives = detector_proposals(image)
        if predicted_hives is None:
            predicted_hives = predict_with_onnx(
                image, include_best_candidate=True
            )
    except Exception as exc:
        messagebox.showerror("Prediction failed", str(exc))
        return

    if not predicted_hives:
        messagebox.showinfo(
            "Prediction",
            "The model did not predict a hive above the presence threshold."
        )
        return

    reviewed_hives = []
    for hive_index, hive in enumerate(predicted_hives):
        messagebox.showinfo(
            "Model prediction",
            f"Predicted hive {hive_index + 1}\n"
            f"Presence confidence: {hive['confidence']:.3f}\n\n"
            "The editor will open. Drag points or lines to correct them."
        )

        reviewed = review_prediction(image, hive, hive_index)
        if reviewed is None:
            return
        reviewed_hives.append(reviewed)

    label = pack_label(reviewed_hives, image.shape)

    calibration = None
    if messagebox.askyesno(
        "Calibration",
        "Add a known distance between the two forward edge lines?",
        parent=root
    ):
        calibration = ask_calibration(
            root,
            image,
            np.asarray(reviewed_hives[0]["points"], dtype=np.float32)
        )

    save_as_training = messagebox.askyesno(
        "Save corrected prediction",
        "Save this corrected prediction as a new training annotation?",
        parent=root
    )

    if save_as_training:
        records = load_annotations()
        add_or_replace_record(
            records,
            {
                "image_path": str(Path(image_path).resolve()),
                "label": label.tolist(),
                "hive_count": len(reviewed_hives),
                "calibration": calibration,
                "line_names": LINES_INPUT_HE,
            }
        )
        save_annotations(records)
        try:
            learn_from_annotations(records)
        except Exception as exc:
            messagebox.showerror(
                "Learning failed",
                "The corrected annotation was saved, but model training/export failed.\n\n"
                f"{exc}",
                parent=root
            )
            return
        messagebox.showinfo(
            "Learning complete",
            "The corrected image was added to training and the model was updated.",
            parent=root
        )

    show_measurement_overlay(image, reviewed_hives, calibration)


def show_measurement_overlay(image, hive_data, calibration):
    """
    Show all predicted lines plus their lengths in pixels and, when calibrated,
    in the selected physical unit.
    """
    panel_width = 430
    canvas = np.zeros(
        (image.shape[0], image.shape[1] + panel_width, 3),
        dtype=np.uint8
    )
    canvas[:, :image.shape[1]] = image

    panel_x = image.shape[1]
    cv2.rectangle(
        canvas,
        (panel_x, 0),
        (panel_x + panel_width, image.shape[0]),
        (28, 28, 28),
        -1
    )

    y = 30
    cv2.putText(
        canvas,
        "BioBuzz - predicted geometry",
        (panel_x + 15, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (0, 255, 255),
        2,
        cv2.LINE_AA
    )
    y += 35

    for hive_index, hive in enumerate(hive_data):
        points = np.asarray(hive["points"], dtype=np.float32)

        cv2.putText(
            canvas,
            f"Hive {hive_index + 1}",
            (panel_x + 15, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (255, 255, 255),
            2,
            cv2.LINE_AA
        )
        y += 26

        for line_index, line_name in enumerate(LINES_INPUT_HE):
            p1_array = points[line_index * 2]
            p2_array = points[line_index * 2 + 1]
            p1 = tuple(np.round(p1_array).astype(int))
            p2 = tuple(np.round(p2_array).astype(int))

            cv2.line(canvas, p1, p2, (0, 220, 0), 2, cv2.LINE_AA)
            cv2.circle(canvas, p1, 4, (0, 0, 255), -1)
            cv2.circle(canvas, p2, 4, (0, 0, 255), -1)

            pixel_length = float(np.linalg.norm(p2_array - p1_array))
            if calibration:
                real_length = pixels_to_units(
                    pixel_length,
                    calibration,
                    calibration["unit"]
                )
                measurement_text = (
                    f"{line_index + 1:02d}. {line_name[:20]} "
                    f"{real_length:.2f} {calibration['unit']}"
                )
            else:
                measurement_text = (
                    f"{line_index + 1:02d}. {line_name[:20]} "
                    f"{pixel_length:.1f} px"
                )

            if y < image.shape[0] - 12:
                cv2.putText(
                    canvas,
                    measurement_text,
                    (panel_x + 15, y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.38,
                    (225, 225, 225),
                    1,
                    cv2.LINE_AA
                )
                y += 22

        if y >= image.shape[0] - 35:
            break

    if calibration and hive_data:
        points = np.asarray(hive_data[0]["points"], dtype=np.float32)
        pixel_gap = perpendicular_distance_between_lines(
            points[0:2],
            points[4:6]
        )
        measured_gap = pixels_to_units(
            pixel_gap,
            calibration,
            calibration["unit"]
        )

        print("\n--- BioBuzz metric measurements ---")
        print(
            f"Reference separation (left-forward to right-forward): "
            f"{measured_gap:.3f} {calibration['unit']} "
            f"({pixel_gap:.1f} px)"
        )

        for line_index, line_name in enumerate(LINES_INPUT_HE):
            line_length_px = float(
                np.linalg.norm(
                    points[line_index * 2 + 1] - points[line_index * 2]
                )
            )
            line_length_real = pixels_to_units(
                line_length_px,
                calibration,
                calibration["unit"]
            )
            print(
                f"{line_name}: {line_length_real:.3f} "
                f"{calibration['unit']} ({line_length_px:.1f} px)"
            )

    window = "BioBuzz - ONNX prediction and measurements"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(
        window,
        min(canvas.shape[1], 1700),
        min(canvas.shape[0], 1000)
    )
    cv2.imshow(window, canvas)
    cv2.waitKey(0)
    cv2.destroyWindow(window)


# ============================================================
# Application entry point
# ============================================================

def main():
    root = tk.Tk()
    root.title("BioBuzz ML - Label, Train, Predict")
    root.geometry("520x370")
    root.resizable(False, False)

    tk.Label(
        root,
        text="BioBuzz ML",
        font=("Arial", 22, "bold")
    ).pack(pady=(18, 5))

    status_label = tk.Label(
        root,
        text=model_status_text(),
        font=("Arial", 9),
        wraplength=480
    )
    status_label.pack(pady=(0, 12))

    def review_folder():
        label_folder(root)
        status_label.config(text=model_status_text())

    def train_and_refresh():
        train_model()
        status_label.config(text=model_status_text())

    def try_and_refresh():
        try_model(root)
        status_label.config(text=model_status_text())

    tk.Button(
        root,
        text="1. Review folder (model proposes, you correct)",
        width=38,
        height=2,
        command=review_folder
    ).pack(pady=5)

    tk.Button(
        root,
        text="2. Train model and export ONNX",
        width=38,
        height=2,
        command=train_and_refresh
    ).pack(pady=5)

    tk.Button(
        root,
        text="3. Try one image and correct prediction",
        width=38,
        height=2,
        command=try_and_refresh
    ).pack(pady=5)

    def export_existing():
        try:
            ensure_onnx_model()
            messagebox.showinfo("Export complete", f"Saved:\n{ONNX_FILE}")
            status_label.config(text=model_status_text())
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    tk.Button(
        root,
        text="4. Export existing trained checkpoint",
        width=38,
        height=2,
        command=export_existing
    ).pack(pady=5)

    tk.Label(
        root,
        text="Model output: 4 hive slots x 60 values = 240 values",
        font=("Arial", 9)
    ).pack(pady=(12, 0))

    root.mainloop()


if __name__ == "__main__":
    main()
