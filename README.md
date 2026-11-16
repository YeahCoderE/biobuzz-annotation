# BioBuzz Hive Studio

A GitHub Pages site for labeling the FTC BIOBUZZ (2026–27) hive cells, for training
`BioBuzzPoseNet` in `main.py`. Everything under `docs/images/` and `docs/data/` is public.

Site: `https://YeahCoderE.github.io/biobuzz-annotation/`
Run it locally: `run_ui.bat` (static server on port 8767).

## What gets labeled

Each hive cell is a transparent, house-shaped pentagonal prism with a red or blue Goal Rib
(frame) at both ends. The 14 lines per cell match `LINES_INPUT_HE` in `main.py`:

| # | Line | A → B |
|---|------|-------|
| 01 / 02 | Left wall, front / back frame | left eave → bottom-left |
| 03 / 04 | Right wall, front / back | right eave → bottom-right |
| 05 / 06 | Bottom edge, front / back | bottom-left → bottom-right |
| 07 / 08 | Right roof slope, front / back | peak → right eave |
| 09 / 10 | Left roof slope, front / back | peak → left eave |
| 11 / 12 | Bottom connection, left / right | front corner → back corner |
| 13 / 14 | Iron rod (Churro), left / right | front frame → back frame |

* **Front** = the frame closer to the camera in the photo. **Left/right** are judged looking
  at the front frame with the roof up.
* **A/B order matters**: the training loss is plain MSE per coordinate, so the site fixes
  swapped endpoints automatically.
* Geometry and all labels live in `docs/data/hive_model.json`. The dimensions come from the
  official Competition Manual (Figures 9-9 and 9-11: opening 20″ wide, 7.61″ walls, 14″ peak,
  12.04″ deep) and the AndyMark field element assembly guide.

## Features

* **Corner mode**: 10 clicks fill all 28 endpoints, because every endpoint sits on one of the
  10 shared corners.
* **Smart complete**: skip hidden corners, then fit a camera to the visible ones (DLT on the
  real 3D shape) to estimate them. Estimated points are dashed, and saved in `estimated_points`.
* **Accuracy check**: flags corners where lines don't meet and points that don't fit the 3D shape.
* **3D model and full-screen tour** of every hive part and every line (Three.js).
* **Realtime**: shows who is online and which image each person is on. An image is locked
  while someone edits it; others watch it read-only with live points and a live cursor. The
  lock is released when the tab closes or after 10 idle minutes. This runs over the public
  MQTT broker `broker.emqx.io` with no sign-up, so the lock is enforced by the site's code, not
  by a server.
* Saved drafts are kept in the browser and synced to every open copy of the site. **Export**
  downloads `annotations_review.json` in the `main.py` format (240 values per image).

## Add images

Upload files to `docs/images/` on GitHub. The Pages workflow rebuilds `images.json` and the
thumbnails. Images taken from the web are credited in `docs/data/image_sources.json`.

Drafts are not training data. Train only after the annotations have been reviewed and approved.

## Hive detector (the trained model)

`models/biobuzz_hive.onnx` finds every hive cell in a picture, its colour, its 10 corners and
the 14 lines above. Run it on images or folders:

```
.venv/Scripts/python -I tools/detect.py photo.jpg some_folder/ --out runs/detect
```

Each image gets an annotated JPEG and a JSON file: colour, confidence, the 10 corners, the 14
lines (A/B in `main.py` order), side/level, an approximate distance, and a `record` in the
`main.py` annotation format. The network's corners are checked against the real 3D shape of the
cell (PnP); hidden corners and corners outside the picture come from that fit. `--refine` also
fits the cell to the red/blue pixels (slower, more exact). `main.py` uses the detector for its
proposals automatically when the file exists, and the site shows a **🤖 זיהוי אוטומטי** button.

How it was made (all scripts in `tools/`, run with `.venv/Scripts/python -I tools/<script>`):

| Step | Script | What it does |
|------|--------|--------------|
| Geometry | `hivekit.py` | One definition of the cell (from `hive_model.json`), camera fitting, label format |
| Real labels | `fit_label.py auto` | From a rough box per cell, searches 32,000 camera poses and fits the 3D cell to the rib pixels; every fit was checked visually (`dataset/labels/`) |
| Synthetic data | `render.py make` | Renders the real cell (0.5″ ribs, bottom tab, holes, Churros, skins, AprilTags, stand, balls, robots) over real FTC venue backgrounds, with exact labels |
| Web and video | `harvest.py`, `pool.py`, `backgrounds.py` | Downloads web images, samples frames from about 318 BIOBUZZ YouTube videos, and collects backgrounds from previous seasons (no hives) |
| Auto-labels | `pseudo.py` | Labels video frames with a trained model and keeps only frames that pass the 3D-fit and pixel checks |
| Training | `build_yolo.py`, `train.py` | YOLO-pose (10 keypoints, red/blue classes) on CPU |
| Accuracy | `evaluate.py` | Held-out real photos, whole bursts kept out of training |

Test photos never used for training: see `TEST_BURSTS` in `tools/build_yolo.py`.

### Two ways to run it

* **Network only** (default, fast, also what the site and a robot would use): about 0.5 s per
  image on this laptop's CPU, about 5 s in the browser.
* **Network + 3D fit** (`--fit`): the network finds each cell, then the real 3D cell is fitted
  to the red/blue rib pixels inside the box. Much more exact, about 20-60 s per cell on this CPU.

### Measured accuracy (9 held-out photos, 9 cells, model v3, ONNX as deployed)

| | Network only | Network + 3D fit (`--fit`) |
|---|---|---|
| Cells found | 9 / 9 | 9 / 9 |
| False detections | 0 | 0 |
| Mean corner error (% of cell size) | 10.1 % | 3.1 % |
| Corners within 5 % | 21 % | 88 % |
| Cells with at least 9 of 10 corners within 5 % | 0 % | 67 % (6 / 9) |

How to read this honestly:
* The test labels were made with the same silhouette fit (from a rough box, then checked by eye),
  so the "+ 3D fit" column partly measures agreement with that method. On a frontal photo marked
  by hand independently, the labels were within 2.2 % (max 3.5 %), so differences below about
  3 % are inside the measurement noise. On two hard views, two fits disagreed by 6-9 % on 2-3
  corners; both look right by eye.
* The two cells that fail with the 3D fit have another red cell inside their box (seen through the
  plastic, or cut off at the photo edge); the colour fit gets pulled towards it. The side view
  (191810) has no defined front frame, so either labelling is accepted for it.
* Model history on the same test: v1 15.2 % corner error / 12 false detections, v2 11.8 % / 1,
  v3 10.1 % / 0. The final run (v4, all 57,300 training images) was still training when this was written.
* 9 cells is a small test set: one cell is 11 percentage points. The 98 % goal is not reached yet;
  the network needs more training (CPU-bound here) and more real, varied photos.
