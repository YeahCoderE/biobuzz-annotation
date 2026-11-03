"""
BioBuzz ML - labeling, editing, metric calibration, training and ONNX inference.

Install:
    pip install opencv-python numpy pillow torch onnx onnxruntime

Run:
    python biobuzz_pipeline.py

Important:
- The model predicts 4 hive slots. Each slot has 60 values:
  presence, color, side, top, and 28 normalized points (56 coordinates).
- 14 lines are labeled per hive; each line has two endpoints.
- Calibration converts pixel distances to a real-world unit using the known
  perpendicular distance between the two forward edge lines. This is a local
  scale estimate, not a full perspective correction for a 3D object.
"""

import json
import math
import random
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

import cv2
import numpy as np
from PIL import Image

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
    try:
        image = cv2.imread(str(file_path), cv2.IMREAD_COLOR)
        if image is not None:
            return image

        with Image.open(file_path) as pil_image:
            rgb = np.array(pil_image.convert("RGB"))
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    except Exception as exc:
        print(f"Image loading error ({file_path}): {exc}")
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


def pack_label(hive_data):
    """
    hive_data: list of dicts, each with metadata and points.
    points: [line0_start, line0_end, ..., line13_start, line13_end]
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


def draw_editor(image, points, current_hive, current_line, selected_point,
                selected_line, mode, status):
    canvas = image.copy()

    for line_index in range(LINES_PER_HIVE):
        p1 = tuple(np.round(points[line_index * 2]).astype(int))
        p2 = tuple(np.round(points[line_index * 2 + 1]).astype(int))

        if selected_line == line_index:
            line_color = (0, 165, 255)
            thickness = 4
        elif line_index == current_line:
            line_color = (255, 255, 0)
            thickness = 3
        else:
            line_color = (0, 220, 0)
            thickness = 2

        cv2.line(canvas, p1, p2, line_color, thickness, cv2.LINE_AA)

        for endpoint_index, point in enumerate((p1, p2)):
            global_point_index = line_index * 2 + endpoint_index
            color = (
                (0, 0, 255)
                if selected_point == global_point_index
                else (255, 80, 0)
            )
            cv2.circle(canvas, point, 6, color, -1, cv2.LINE_AA)

    overlay = canvas.copy()
    cv2.rectangle(overlay, (0, 0), (image.shape[1], 112), (25, 25, 25), -1)
    cv2.addWeighted(overlay, 0.78, canvas, 0.22, 0, canvas)

    lines = [
        f"Hive {current_hive + 1} | {current_line + 1}/{LINES_PER_HIVE}: {LINES_INPUT_HE[current_line]}",
        f"Mode: {mode} | {status}",
        "Drag endpoint = move point | Drag line = move whole line",
        "R = redraw selected line | Z = undo | Y = redo | ENTER = accept | ESC = cancel",
    ]

    for index, text in enumerate(lines):
        cv2.putText(
            canvas,
            text,
            (12, 25 + index * 27),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (255, 255, 255),
            1,
            cv2.LINE_AA
        )

    return canvas


def edit_hive_points(image, points, hive_number):
    """
    Drag endpoints or whole lines. R redraws a selected line using two clicks.
    Z/Y undo/redo. ENTER accepts.
    """
    global mouse_position, mouse_event, mouse_flags

    points = np.asarray(points, dtype=np.float32).copy()
    history = [points.copy()]
    history_index = 0

    selected_point = None
    selected_line = None
    drag_mode = None
    drag_start = None
    line_original = None
    redraw_line = None
    redraw_clicks = []
    status = "Select a point or line"
    current_line = 0

    window = f"BioBuzz editor - Hive {hive_number}"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, min(image.shape[1], 1400), min(image.shape[0], 900))
    cv2.setMouseCallback(window, mouse_callback)

    try:
        while True:
            current_line = (
                selected_line if selected_line is not None
                else (selected_point // 2 if selected_point is not None else 0)
            )

            canvas = draw_editor(
                image, points, hive_number - 1, current_line,
                selected_point, selected_line,
                "REDRAW" if redraw_line is not None else "EDIT",
                status
            )

            if redraw_line is not None:
                cv2.putText(
                    canvas,
                    f"Redrawing line {redraw_line + 1}: click endpoint 1 then endpoint 2",
                    (12, 145),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (0, 255, 255),
                    2,
                    cv2.LINE_AA
                )
                for point in redraw_clicks:
                    cv2.circle(canvas, tuple(np.round(point).astype(int)), 8, (255, 0, 255), -1)

            cv2.imshow(window, canvas)
            key = cv2.waitKey(15) & 0xFF

            if key == 27:
                return None

            if key in (10, 13):
                return points

            if key in (ord("z"), ord("Z")):
                if history_index > 0:
                    history_index -= 1
                    points = history[history_index].copy()
                    status = "Undo"
                continue

            if key in (ord("y"), ord("Y")):
                if history_index < len(history) - 1:
                    history_index += 1
                    points = history[history_index].copy()
                    status = "Redo"
                continue

            if key in (ord("r"), ord("R")):
                if selected_line is not None:
                    redraw_line = selected_line
                    redraw_clicks = []
                    status = "Click two new endpoints"
                else:
                    status = "Select a line first, then press R"
                continue

            if mouse_event == cv2.EVENT_LBUTTONDOWN and mouse_position is not None:
                x, y = mouse_position
                if redraw_line is not None:
                    redraw_clicks.append(np.array([x, y], dtype=np.float32))
                    if len(redraw_clicks) == 2:
                        points[redraw_line * 2] = redraw_clicks[0]
                        points[redraw_line * 2 + 1] = redraw_clicks[1]
                        history = history[:history_index + 1]
                        history.append(points.copy())
                        history_index += 1
                        status = f"Line {redraw_line + 1} rebuilt"
                        redraw_line = None
                        redraw_clicks = []
                    mouse_event = None
                    continue

                distances = np.linalg.norm(points - np.array([x, y]), axis=1)
                nearest_point = int(np.argmin(distances))
                if distances[nearest_point] <= 18:
                    selected_point = nearest_point
                    selected_line = nearest_point // 2
                    drag_mode = "point"
                    drag_start = (x, y)
                    status = "Dragging endpoint"
                else:
                    segment_distances = [
                        point_segment_distance(
                            (x, y),
                            points[line_index * 2],
                            points[line_index * 2 + 1]
                        )
                        for line_index in range(LINES_PER_HIVE)
                    ]
                    nearest_line = int(np.argmin(segment_distances))
                    if segment_distances[nearest_line] <= 14:
                        selected_line = nearest_line
                        selected_point = None
                        drag_mode = "line"
                        drag_start = np.array([x, y], dtype=np.float32)
                        line_original = points[
                            nearest_line * 2:nearest_line * 2 + 2
                        ].copy()
                        status = "Dragging whole line"
                    else:
                        selected_point = None
                        selected_line = None
                        drag_mode = None
                        status = "No point or line selected"

                mouse_event = None

            elif mouse_event == cv2.EVENT_MOUSEMOVE and drag_mode and mouse_position:
                x, y = mouse_position

                if drag_mode == "point" and selected_point is not None:
                    points[selected_point] = [x, y]

                elif drag_mode == "line" and selected_line is not None:
                    delta = np.array([x, y], dtype=np.float32) - drag_start
                    points[selected_line * 2:selected_line * 2 + 2] = line_original + delta

                mouse_event = None

            elif mouse_event == cv2.EVENT_LBUTTONUP and drag_mode:
                history = history[:history_index + 1]
                history.append(points.copy())
                history_index += 1
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

def label_single_image(image_path, root):
    image = load_any_image_format(image_path)
    if image is None:
        messagebox.showerror("Image error", f"Cannot load:\n{image_path}")
        return None

    hive_count = simpledialog.askinteger(
        "כמות כוורות",
        "כמה כוורות מופיעות בתמונה? (1-4)",
        parent=root,
        minvalue=1,
        maxvalue=MAX_HIVES,
        initialvalue=1
    )
    if hive_count is None:
        return None

    hives = []
    for hive_index in range(hive_count):
        metadata = ask_hive_metadata(root, hive_index + 1)
        if metadata is None:
            return None

        points = []
        for line_index, line_name in enumerate(LINES_INPUT_HE):
            for point_type in ("התחלה", "סיום"):
                global mouse_position, mouse_event

                prompt = (
                    f"כוורת {hive_index + 1}/{hive_count}\n"
                    f"קו {line_index + 1}/{LINES_PER_HIVE}: {line_name}\n"
                    f"לחץ על נקודת {point_type}.\n"
                    "Z = undo | ESC = cancel"
                )
                messagebox.showinfo("Label point", prompt, parent=root)

                clicked = None
                window = "BioBuzz - Labeling"
                cv2.namedWindow(window, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(window, min(image.shape[1], 1400), min(image.shape[0], 900))
                cv2.setMouseCallback(window, mouse_callback)

                try:
                    while clicked is None:
                        display = image.copy()
                        for idx in range(0, len(points), 2):
                            if idx + 1 < len(points):
                                cv2.line(
                                    display,
                                    tuple(np.round(points[idx]).astype(int)),
                                    tuple(np.round(points[idx + 1]).astype(int)),
                                    (0, 220, 0),
                                    2
                                )
                        cv2.putText(
                            display,
                            f"Hive {hive_index + 1} | Line {line_index + 1}/{LINES_PER_HIVE} | {point_type}",
                            (12, 30),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.65,
                            (0, 0, 255),
                            2
                        )
                        cv2.imshow(window, display)
                        key = cv2.waitKey(20) & 0xFF

                        if key == 27:
                            return None
                        # Point placement is sequential. Use the editor after
                        # labeling for undo/redo and corrections.
                        if mouse_event == cv2.EVENT_LBUTTONDOWN and mouse_position:
                            x, y = mouse_position
                            if 0 <= x < image.shape[1] and 0 <= y < image.shape[0]:
                                clicked = (float(x), float(y))
                            mouse_event = None
                finally:
                    cv2.destroyWindow(window)

                points.append(clicked)

        points = np.asarray(points, dtype=np.float32).reshape(-1, 2)

        edited_points = edit_hive_points(
            image,
            points,
            hive_index + 1
        )
        if edited_points is None:
            return None

        hives.append({
            "metadata": metadata,
            "points": edited_points.tolist(),
        })

    label = pack_label(hives)

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
    added = 0

    for image_path in image_paths:
        answer = messagebox.askyesnocancel(
            "Label images",
            f"Label this image?\n\n{image_path.name}\n\n"
            "Yes = label, No = skip, Cancel = stop.",
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
        added += 1

    messagebox.showinfo(
        "Labeling complete",
        f"New or updated labels: {added}\n"
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

        # Convert original-image normalized points into letterboxed model coordinates.
        for hive_index in range(MAX_HIVES):
            offset = hive_index * VALUES_PER_HIVE
            if label[offset] < 0.5:
                continue

            coordinate_start = offset + 4
            coords = label[coordinate_start:offset + VALUES_PER_HIVE].reshape(-1, 2)

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
    model = BioBuzzPoseNet().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)

    best_loss = float("inf")
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


def export_onnx():
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


def predict_with_onnx(image):
    if not ONNX_FILE.exists():
        raise FileNotFoundError("ONNX file not found. Train/export first.")

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

    return predicted_hives


# ============================================================
# Prediction review / correction / save as new training data
# ============================================================

def review_prediction(image, hive_data, hive_index):
    points = np.asarray(hive_data["points"], dtype=np.float32)
    edited = edit_hive_points(image, points, hive_index + 1)
    if edited is None:
        return None

    hive_data["points"] = edited.tolist()
    return hive_data


def try_model(root):
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
        predicted_hives = predict_with_onnx(image)
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

    label = pack_label(reviewed_hives)

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
    root.geometry("470x330")
    root.resizable(False, False)

    tk.Label(
        root,
        text="BioBuzz ML",
        font=("Arial", 22, "bold")
    ).pack(pady=(18, 5))

    tk.Label(
        root,
        text="14 structural lines | Manual editing | Metric calibration | ONNX",
        font=("Arial", 9)
    ).pack(pady=(0, 16))

    tk.Button(
        root,
        text="1. Label images / add training data",
        width=38,
        height=2,
        command=lambda: label_folder(root)
    ).pack(pady=5)

    tk.Button(
        root,
        text="2. Train model and export ONNX",
        width=38,
        height=2,
        command=train_model
    ).pack(pady=5)

    tk.Button(
        root,
        text="3. Try ONNX model and correct prediction",
        width=38,
        height=2,
        command=lambda: try_model(root)
    ).pack(pady=5)

    def export_existing():
        try:
            export_onnx()
            messagebox.showinfo("Export complete", f"Saved:\n{ONNX_FILE}")
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
