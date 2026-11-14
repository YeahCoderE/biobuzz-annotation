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
