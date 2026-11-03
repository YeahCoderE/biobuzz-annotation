"""
BioBuzz ML - End-to-end labeling, training, and ONNX export.

Workflow:
1. Select a folder containing images.
2. Label each image (1-4 hives, 12 lines / 24 points per hive).
3. Save annotations to dataset/annotations.json.
4. Train the CNN.
5. Export and verify BioBuzzPoseNet.onnx.

Install:
    pip install opencv-python numpy pillow torch onnx onnxruntime

Run:
    python biobuzz_pipeline.py

Notes:
- This script creates a real training/export pipeline, but it does not provide
  pretrained weights. You must label a sufficiently large and representative
  dataset before the exported model can be useful.
- Hive slots are ordered by the order in which you label them.
- Each hive target has 51 values: [color, side, top] + 48 normalized coordinates.
- Four slots produce 204 output values.
"""

import json
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
VALUES_PER_HIVE = 51
OUTPUT_SIZE = MAX_HIVES * VALUES_PER_HIVE

BATCH_SIZE = 8
EPOCHS = 60
LEARNING_RATE = 1e-4
VALIDATION_FRACTION = 0.2
RANDOM_SEED = 42

PROJECT_DIR = Path(__file__).resolve().parent
DATASET_DIR = PROJECT_DIR / "dataset"
ANNOTATIONS_FILE = DATASET_DIR / "annotations.json"
MODEL_FILE = PROJECT_DIR / "BioBuzzPoseNet.pth"
ONNX_FILE = PROJECT_DIR / "BioBuzzPoseNet.onnx"

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"
}

LINES_INPUT_HE = [
    "קו קצה שמאלי - קדימה",
    "קו קצה שמאלי - אחורה",
    "קו קצה ימני - קדימה",
    "קו קצה ימני - אחורה",
    "קו תחתון - קדימה",
    "קו תחתון - אחורה",
    "קו קצה למעלה ימני - קדימה",
    "קו קצה למעלה ימני - אחורה",
    "קו קצה למעלה שמאלי - קדימה",
    "קו קצה למעלה שמאלי - אחורה",
    "קו חיבור ברזל שמאל",
    "קו חיבור ברזל ימין",
]

mouse_click_coord = None
action_trigger = None


# ============================================================
# Image utilities
# ============================================================

def load_any_image_format(file_path):
    """Load an image as an OpenCV BGR uint8 array."""
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


def preprocess_image(image):
    """
    Resize to the model input dimensions.
    The same BGR channel order is used for training and inference.
    """
    resized = cv2.resize(
        image,
        (IMAGE_SIZE, IMAGE_SIZE),
        interpolation=cv2.INTER_AREA
    )

    tensor_array = resized.astype(np.float32) / 255.0
    tensor_array = np.transpose(tensor_array, (2, 0, 1))

    return torch.from_numpy(np.ascontiguousarray(tensor_array))


# ============================================================
# Mouse handling
# ============================================================

def click_event_handler(event, x, y, flags, param):
    global mouse_click_coord, action_trigger

    if event == cv2.EVENT_LBUTTONDOWN:
        mouse_click_coord = (x, y)
        action_trigger = "click"


# ============================================================
# Metadata input
# ============================================================

def ask_hive_metadata(root, hive_number):
    """Ask for metadata and reject invalid or cancelled input."""
    color = simpledialog.askstring(
        "Hive metadata",
        f"Hive {hive_number}: color (blue/red):",
        parent=root
    )
    if color is None:
        return None
    color = color.strip().lower()
    if color not in ("blue", "red"):
        messagebox.showerror("Invalid input", "Color must be blue or red.")
        return None

    side = simpledialog.askstring(
        "Hive metadata",
        f"Hive {hive_number}: field side (left/right):",
        parent=root
    )
    if side is None:
        return None
    side = side.strip().lower()
    if side not in ("left", "right"):
        messagebox.showerror("Invalid input", "Side must be left or right.")
        return None

    vertical = simpledialog.askstring(
        "Hive metadata",
        f"Hive {hive_number}: vertical position (top/bottom):",
        parent=root
    )
    if vertical is None:
        return None
    vertical = vertical.strip().lower()
    if vertical not in ("top", "bottom"):
        messagebox.showerror(
            "Invalid input",
            "Vertical position must be top or bottom."
        )
        return None

    return [
        1.0 if color == "blue" else 0.0,
        1.0 if side == "right" else 0.0,
        1.0 if vertical == "top" else 0.0,
    ]


# ============================================================
# Labeling UI
# ============================================================

def label_single_image(image_path, root):
    """
    Label one image.
    Returns a 204-value float32 vector, or None if cancelled.
    """
    global mouse_click_coord, action_trigger

    image = load_any_image_format(image_path)
    if image is None:
        messagebox.showerror("Image error", f"Could not load:\n{image_path}")
        return None

    image_height, image_width = image.shape[:2]

    num_hives = simpledialog.askinteger(
        "Number of hives",
        "How many hives are visible? (1-4)",
        parent=root,
        minvalue=1,
        maxvalue=MAX_HIVES,
        initialvalue=1
    )
    if num_hives is None:
        return None

    hive_metadata = []
    for hive_index in range(num_hives):
        metadata = ask_hive_metadata(root, hive_index + 1)
        if metadata is None:
            return None
        hive_metadata.append(metadata)

    tasks = []
    for hive_index in range(num_hives):
        for line_index, line_name in enumerate(LINES_INPUT_HE):
            tasks.append({
                "hive": hive_index,
                "line_index": line_index,
                "line_name": line_name,
                "point_type": "התחלה",
            })
            tasks.append({
                "hive": hive_index,
                "line_index": line_index,
                "line_name": line_name,
                "point_type": "סיום",
            })

    points = []
    task_index = 0

    window_name = "BioBuzz ML - Labeling"
    dashboard_width = 450
    canvas_width = image_width + dashboard_width
    canvas_height = max(image_height, 700)

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, min(canvas_width, 1800), min(canvas_height, 1000))
    cv2.setMouseCallback(window_name, click_event_handler)

    try:
        while task_index < len(tasks):
            task = tasks[task_index]

            canvas = np.zeros(
                (canvas_height, canvas_width, 3),
                dtype=np.uint8
            )
            canvas[:image_height, :image_width] = image

            # Draw completed points and their line segments.
            for point_index, point in enumerate(points):
                cv2.circle(canvas, point, 5, (0, 0, 255), -1)
                if point_index % 2 == 1:
                    cv2.line(
                        canvas,
                        points[point_index - 1],
                        point,
                        (0, 255, 0),
                        2
                    )

            # Dashboard.
            dash_x = image_width
            cv2.rectangle(
                canvas,
                (dash_x, 0),
                (canvas_width, canvas_height),
                (30, 30, 30),
                -1
            )
            cv2.line(
                canvas,
                (dash_x, 0),
                (dash_x, canvas_height),
                (70, 70, 70),
                2
            )

            metadata = hive_metadata[task["hive"]]
            color_text = "Blue" if metadata[0] else "Red"
            side_text = "Right" if metadata[1] else "Left"
            vertical_text = "Top" if metadata[2] else "Bottom"

            progress = int(100 * task_index / len(tasks))

            dashboard_lines = [
                ("BIOBUZZ - LABELING", 50, 0.8, (0, 255, 255)),
                (f"Hive: {task['hive'] + 1}/{num_hives}", 120, 0.65, (255, 255, 255)),
                (f"Metadata: {color_text}, {side_text}, {vertical_text}", 165, 0.5, (210, 210, 210)),
                (f"Line: {task['line_index'] + 1}/12", 220, 0.65, (210, 210, 210)),
                (f"Click: {task['point_type']}", 290, 0.65, (100, 255, 255)),
                (task["line_name"], 345, 0.45, (0, 200, 255)),
                ("Undo: Z or Backspace", 440, 0.5, (170, 170, 170)),
                ("Cancel: ESC", 485, 0.5, (170, 170, 170)),
                (f"Progress: {progress}%", 600, 0.6, (0, 255, 0)),
            ]

            for text, y, scale, color in dashboard_lines:
                # Hebrew strings are reversed for a basic OpenCV display workaround.
                display_text = text[::-1] if any("\u0590" <= ch <= "\u05FF" for ch in text) else text
                cv2.putText(
                    canvas,
                    display_text,
                    (dash_x + 20, y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    scale,
                    color,
                    1 if scale < 0.7 else 2,
                    cv2.LINE_AA
                )

            cv2.imshow(window_name, canvas)

            mouse_click_coord = None
            action_trigger = None
            key = cv2.waitKey(20) & 0xFF

            if key in (8, 127, ord("z"), ord("Z")):
                if points:
                    points.pop()
                    task_index -= 1
                continue

            if key == 27:
                return None

            if action_trigger == "click" and mouse_click_coord is not None:
                x, y = mouse_click_coord

                if 0 <= x < image_width and 0 <= y < image_height:
                    points.append((x, y))
                    task_index += 1

        # Convert labels to normalized coordinates.
        feature_slots = []

        points_per_hive = 24
        for hive_index in range(MAX_HIVES):
            if hive_index < num_hives:
                start = hive_index * points_per_hive
                hive_points = points[start:start + points_per_hive]

                normalized_coordinates = []
                for x, y in hive_points:
                    normalized_coordinates.extend([
                        x / image_width,
                        y / image_height
                    ])

                slot = hive_metadata[hive_index] + normalized_coordinates
                if len(slot) != VALUES_PER_HIVE:
                    raise RuntimeError(
                        f"Expected {VALUES_PER_HIVE} values for hive, got {len(slot)}"
                    )
                feature_slots.extend(slot)
            else:
                feature_slots.extend([0.0] * VALUES_PER_HIVE)

        label = np.asarray(feature_slots, dtype=np.float32)

        if label.shape != (OUTPUT_SIZE,):
            raise RuntimeError(f"Unexpected label shape: {label.shape}")

        return label

    finally:
        cv2.destroyWindow(window_name)


# ============================================================
# Annotation storage
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
        messagebox.showwarning(
            "No images",
            "No supported images were found in the selected folder."
        )
        return

    records = load_annotations()
    existing_paths = {
        str(Path(record["image_path"]).resolve())
        for record in records
    }

    added = 0
    skipped = 0

    for image_path in image_paths:
        resolved_path = str(image_path.resolve())

        if resolved_path in existing_paths:
            skipped += 1
            continue

        answer = messagebox.askyesnocancel(
            "Label images",
            f"Label this image?\n\n{image_path.name}\n\n"
            "Yes = label, No = skip, Cancel = stop.",
            parent=root
        )

        if answer is None:
            break
        if not answer:
            skipped += 1
            continue

        label = label_single_image(image_path, root)
        if label is None:
            continue

        records.append({
            "image_path": resolved_path,
            "label": label.tolist()
        })
        existing_paths.add(resolved_path)
        added += 1

        # Save after every image so progress is not lost.
        save_annotations(records)

    messagebox.showinfo(
        "Labeling complete",
        f"New images labeled: {added}\nSkipped: {skipped}\n"
        f"Total annotations: {len(records)}\n\n"
        f"Saved to:\n{ANNOTATIONS_FILE}"
    )


# ============================================================
# PyTorch Dataset
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
            raise RuntimeError(
                f"Could not load training image: {record['image_path']}"
            )

        image_tensor = preprocess_image(image)

        label = np.asarray(record["label"], dtype=np.float32)
        if label.shape != (OUTPUT_SIZE,):
            raise ValueError(
                f"Invalid label size for {record['image_path']}: {label.shape}"
            )

        label_tensor = torch.from_numpy(label)
        return image_tensor, label_tensor


# ============================================================
# CNN Model
# ============================================================

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
        features = self.encoder(x)
        return self.fc_layers(features)


# ============================================================
# Training and validation
# ============================================================

def train_one_epoch(model, loader, optimizer, criterion, device):
    model.train()
    total_loss = 0.0

    for images, labels in loader:
        images = images.to(device)
        labels = labels.to(device)

        optimizer.zero_grad(set_to_none=True)

        predictions = model(images)
        loss = criterion(predictions, labels)

        loss.backward()
        optimizer.step()

        total_loss += loss.item() * images.size(0)

    return total_loss / len(loader.dataset)


@torch.no_grad()
def validate(model, loader, criterion, device):
    model.eval()
    total_loss = 0.0

    for images, labels in loader:
        images = images.to(device)
        labels = labels.to(device)

        predictions = model(images)
        loss = criterion(predictions, labels)

        total_loss += loss.item() * images.size(0)

    return total_loss / len(loader.dataset)


def train_model():
    if not ANNOTATIONS_FILE.exists():
        messagebox.showerror(
            "Dataset missing",
            "Label images first. annotations.json was not found."
        )
        return

    records = load_annotations()

    if len(records) < 2:
        messagebox.showerror(
            "Not enough data",
            "At least 2 labeled images are required to start training. "
            "More images are strongly recommended."
        )
        return

    dataset = BioBuzzDataset(records)

    random.seed(RANDOM_SEED)
    torch.manual_seed(RANDOM_SEED)

    validation_size = max(
        1,
        int(len(dataset) * VALIDATION_FRACTION)
    )
    training_size = len(dataset) - validation_size

    if training_size < 1:
        messagebox.showerror("Dataset error", "Not enough images for a split.")
        return

    train_dataset, validation_dataset = random_split(
        dataset,
        [training_size, validation_size],
        generator=torch.Generator().manual_seed(RANDOM_SEED)
    )

    train_loader = DataLoader(
        train_dataset,
        batch_size=BATCH_SIZE,
        shuffle=True,
        num_workers=0
    )

    validation_loader = DataLoader(
        validation_dataset,
        batch_size=BATCH_SIZE,
        shuffle=False,
        num_workers=0
    )

    device = torch.device(
        "cuda" if torch.cuda.is_available() else "cpu"
    )
    print(f"Training device: {device}")
    print(f"Images: {len(dataset)}")
    print(f"Training images: {training_size}")
    print(f"Validation images: {validation_size}")

    model = BioBuzzPoseNet().to(device)
    criterion = nn.MSELoss()
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=LEARNING_RATE
    )

    best_validation_loss = float("inf")

    for epoch in range(EPOCHS):
        training_loss = train_one_epoch(
            model,
            train_loader,
            optimizer,
            criterion,
            device
        )

        validation_loss = validate(
            model,
            validation_loader,
            criterion,
            device
        )

        print(
            f"Epoch [{epoch + 1}/{EPOCHS}] "
            f"Train Loss: {training_loss:.6f} | "
            f"Validation Loss: {validation_loss:.6f}"
        )

        if validation_loss < best_validation_loss:
            best_validation_loss = validation_loss

            torch.save(
                {
                    "model_state_dict": model.state_dict(),
                    "image_size": IMAGE_SIZE,
                    "output_size": OUTPUT_SIZE,
                    "best_validation_loss": best_validation_loss,
                },
                MODEL_FILE
            )

    print(f"Best validation loss: {best_validation_loss:.6f}")
    print(f"Saved PyTorch model: {MODEL_FILE}")

    export_onnx()
    return best_validation_loss


# ============================================================
# ONNX export and verification
# ============================================================

def export_onnx():
    if not MODEL_FILE.exists():
        raise FileNotFoundError(
            f"Trained checkpoint not found: {MODEL_FILE}"
        )

    device = torch.device("cpu")

    checkpoint = torch.load(
        MODEL_FILE,
        map_location=device,
        weights_only=True
    )

    model = BioBuzzPoseNet().to(device)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.eval()

    dummy_input = torch.zeros(
        1,
        3,
        IMAGE_SIZE,
        IMAGE_SIZE,
        dtype=torch.float32
    )

    torch.onnx.export(
        model,
        dummy_input,
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

    print(f"ONNX model exported: {ONNX_FILE}")

    # Verify ONNX graph if ONNX is installed.
    try:
        import onnx

        onnx_model = onnx.load(str(ONNX_FILE))
        onnx.checker.check_model(onnx_model)
        print("ONNX graph check: PASSED")

    except ImportError:
        print("onnx package not installed; skipped graph validation.")

    # Compare ONNX Runtime output with PyTorch output.
    try:
        import onnxruntime as ort

        session = ort.InferenceSession(
            str(ONNX_FILE),
            providers=["CPUExecutionProvider"]
        )

        test_input = np.random.rand(
            1, 3, IMAGE_SIZE, IMAGE_SIZE
        ).astype(np.float32)

        with torch.no_grad():
            torch_output = model(
                torch.from_numpy(test_input)
            ).numpy()

        onnx_output = session.run(
            ["predictions"],
            {"image": test_input}
        )[0]

        max_difference = float(
            np.max(np.abs(torch_output - onnx_output))
        )

        print(f"ONNX Runtime output shape: {onnx_output.shape}")
        print(f"Maximum PyTorch/ONNX difference: {max_difference:.8f}")

        if onnx_output.shape != (1, OUTPUT_SIZE):
            raise RuntimeError(
                f"Unexpected ONNX output shape: {onnx_output.shape}"
            )

        if max_difference > 1e-4:
            raise RuntimeError(
                "ONNX output differs too much from PyTorch output."
            )

        print("ONNX Runtime verification: PASSED")

    except ImportError:
        print(
            "onnxruntime package not installed; "
            "skipped runtime verification."
        )

    print(f"\nFinal ONNX file: {ONNX_FILE}")


# ============================================================
# Main application
# ============================================================

def main():
    root = tk.Tk()
    root.title("BioBuzz ML Pipeline")
    root.geometry("440x260")
    root.resizable(False, False)

    title = tk.Label(
        root,
        text="BioBuzz ML",
        font=("Arial", 20, "bold")
    )
    title.pack(pady=(18, 6))

    subtitle = tk.Label(
        root,
        text="Label images, train the model, and export ONNX.",
        font=("Arial", 10)
    )
    subtitle.pack(pady=(0, 15))

    label_button = tk.Button(
        root,
        text="1. Label images",
        width=30,
        height=2,
        command=lambda: label_folder(root)
    )
    label_button.pack(pady=5)

    train_button = tk.Button(
        root,
        text="2. Train and export ONNX",
        width=30,
        height=2,
        command=train_model
    )
    train_button.pack(pady=5)

    def export_existing():
        try:
            export_onnx()
            messagebox.showinfo(
                "Export complete",
                f"ONNX export completed:\n{ONNX_FILE}"
            )
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    export_button = tk.Button(
        root,
        text="3. Export existing trained model",
        width=30,
        height=2,
        command=export_existing
    )
    export_button.pack(pady=5)

    root.mainloop()


if __name__ == "__main__":
    main()
