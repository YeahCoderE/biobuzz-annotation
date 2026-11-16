#!/usr/bin/env python
"""Download helpers for collecting real BIOBUZZ hive images.

Always run with the project venv in isolated mode, from the repo root:

    .venv/Scripts/python -I tools/harvest.py <command> ...

Everything downloaded is untrusted data. It is only ever decoded as an image or a
video, re-encoded as a clean JPEG, and stored under dataset/real/ (git-ignored).

Commands
    image   URL [--page PAGE] [--source NAME] [--note TEXT]   download one image
    video   URL [--max-height 1080]                           download one video (yt-dlp)
    frames  VIDEO_ID|URL [--every 2.0] [--start S] [--end S]  sample frames into a staging dir
                                                               (a URL streams without downloading: use for long videos)
    sheet   DIR [--cols 5] [--thumb 380]                      contact sheets for triage
    keep    DIR --names a.jpg b.jpg ... [--note TEXT]         move chosen staged frames to the dataset
    drop    FILE [FILE ...]                                    remove files from the dataset + manifest
    status                                                     counts per source
"""
import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "dataset" / "real"
WEB_DIR = REAL / "web"
VIDEO_DIR = REAL / "video"
FRAME_DIR = REAL / "frames"
STAGE_DIR = REAL / "stage"
SHEET_DIR = REAL / "sheets"
MANIFEST = REAL / "manifest.jsonl"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
MAX_IMAGE_BYTES = 40 * 1024 * 1024


def slug(text, limit=48):
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:limit] or "x"


def read_manifest():
    if not MANIFEST.exists():
        return []
    rows = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def append_manifest(row):
    REAL.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_manifest(rows):
    REAL.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def pixel_hash(image):
    small = image.convert("RGB").resize((64, 64))
    return hashlib.sha1(small.tobytes()).hexdigest()


def dhash(image, size=8):
    from PIL import Image
    gray = image.convert("L").resize((size + 1, size), Image.BILINEAR)
    px = gray.tobytes()
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (px[y * (size + 1) + x] > px[y * (size + 1) + x + 1])
    return bits


def cmd_image(args):
    from PIL import Image, ImageOps
    url = args.url
    if not re.match(r"^https?://", url):
        sys.exit("only http(s) URLs")
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.8",
                                                   "Referer": args.page or url})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "url": url, "error": f"download failed: {exc}"}))
        return
    if len(data) > MAX_IMAGE_BYTES:
        print(json.dumps({"ok": False, "url": url, "error": "too large"}))
        return
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        image = ImageOps.exif_transpose(image)
        if getattr(image, "is_animated", False):
            image.seek(0)
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[3])
            image = background
        else:
            image = image.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "url": url, "error": f"not an image: {exc}"}))
        return
    width, height = image.size
    if min(width, height) < args.min_side:
        print(json.dumps({"ok": False, "url": url, "error": f"too small ({width}x{height})"}))
        return
    digest = pixel_hash(image)
    for row in read_manifest():
        if row.get("sha1") == digest:
            print(json.dumps({"ok": True, "duplicate": True, "file": row["file"], "url": url}))
            return
    source = slug(args.source or urllib.parse.urlparse(args.page or url).netloc)
    out_dir = WEB_DIR / source
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"web_{source}_{digest[:10]}.jpg"
    path = out_dir / name
    image.save(path, "JPEG", quality=95)
    row = {"file": str(path.relative_to(ROOT)).replace("\\", "/"), "name": name, "kind": "web", "url": url,
           "page": args.page, "source": source, "note": args.note, "width": width, "height": height, "sha1": digest}
    append_manifest(row)
    print(json.dumps({"ok": True, "file": row["file"], "width": width, "height": height}))


def video_id_of(url):
    match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", url)
    return match.group(1) if match else slug(url, 24)


def cmd_video(args):
    vid = video_id_of(args.url)
    out_dir = VIDEO_DIR / vid
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = [p for p in out_dir.glob("video.*") if p.suffix in (".mp4", ".webm", ".mkv")]
    if existing:
        print(json.dumps({"ok": True, "video_id": vid, "file": str(existing[0]), "cached": True}))
        return
    fmt = (f"bv*[ext=mp4][vcodec^=avc1][height<={args.max_height}]/"
           f"b[ext=mp4][height<={args.max_height}]/bv*[height<={args.max_height}]/b")
    command = [sys.executable, "-m", "yt_dlp", "--js-runtimes", "node", "--no-playlist", "-f", fmt,
               "--max-filesize", "1500M", "--write-info-json", "--no-progress", "--retries", "3",
               "-o", str(out_dir / "video.%(ext)s"), args.url]
    if args.section:
        command[4:4] = ["--download-sections", args.section]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    files = [p for p in out_dir.glob("video.*") if p.suffix in (".mp4", ".webm", ".mkv")]
    info = {}
    info_path = out_dir / "video.info.json"
    if info_path.exists():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            info = {k: raw.get(k) for k in ("title", "channel", "duration", "upload_date", "width", "height")}
        except Exception:  # noqa: BLE001
            pass
    if not files:
        tail = (result.stderr or result.stdout or "")[-900:]
        print(json.dumps({"ok": False, "video_id": vid, "error": tail}))
        return
    (out_dir / "source.json").write_text(json.dumps({"url": args.url, **info}, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": True, "video_id": vid, "file": str(files[0]), **info}, ensure_ascii=False))


def cmd_frames(args):
    import cv2
    from PIL import Image
    vid = video_id_of(args.video_id) if args.video_id.startswith("http") else args.video_id
    files = [p for p in (VIDEO_DIR / vid).glob("video.*") if p.suffix in (".mp4", ".webm", ".mkv")]
    if files:
        capture = cv2.VideoCapture(str(files[0]))
    elif args.video_id.startswith("http"):
        # Stream straight from the source (no full download): good for long videos.
        fmt = (f"bv*[ext=mp4][vcodec^=avc1][height<={args.max_height}][protocol=https]/"
               f"b[ext=mp4][height<={args.max_height}][protocol=https]")
        got = subprocess.run([sys.executable, "-m", "yt_dlp", "--js-runtimes", "node", "--no-playlist", "-f", fmt,
                              "--print", "%(title)s", "--print", "%(channel)s", "--print", "urls", args.video_id],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        lines = [line for line in got.stdout.splitlines() if line.strip()]
        if got.returncode != 0 or len(lines) < 3:
            print(json.dumps({"ok": False, "video_id": vid, "error": (got.stderr or got.stdout)[-700:]}))
            return
        (VIDEO_DIR / vid).mkdir(parents=True, exist_ok=True)
        (VIDEO_DIR / vid / "source.json").write_text(
            json.dumps({"url": args.video_id, "title": lines[0], "channel": lines[1]}, ensure_ascii=False), encoding="utf-8")
        capture = cv2.VideoCapture(lines[-1])
    else:
        sys.exit(f"no downloaded video for {vid}")
    if not capture.isOpened():
        print(json.dumps({"ok": False, "video_id": vid, "error": "could not open the video stream"}))
        return
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    total = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = total / fps if total else 0
    start = max(0.0, args.start)
    end = min(duration, args.end) if args.end > 0 and duration else (args.end if args.end > 0 else duration)
    stage = STAGE_DIR / (args.tag or vid)
    stage.mkdir(parents=True, exist_ok=True)
    saved, last_hash, t = [], None, start
    while t <= end + 1e-6 and len(saved) < args.limit:
        capture.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = capture.read()
        if not ok:
            break
        height, width = frame.shape[:2]
        if width > args.max_width:
            scale = args.max_width / width
            frame = cv2.resize(frame, (args.max_width, int(round(height * scale))), interpolation=cv2.INTER_AREA)
        pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        code = dhash(pil)
        if last_hash is None or bin(code ^ last_hash).count("1") >= args.min_diff:
            name = f"{vid}_t{int(round(t * 100)):07d}.jpg"
            pil.save(stage / name, "JPEG", quality=93)
            saved.append(name)
            last_hash = code
        t += args.every
    capture.release()
    print(json.dumps({"ok": True, "video_id": vid, "stage": str(stage), "saved": len(saved), "duration": round(duration, 1),
                      "fps": round(fps, 2), "first": saved[:2], "last": saved[-2:]}))


def cmd_sheet(args):
    from PIL import Image, ImageDraw
    directory = Path(args.dir)
    names = sorted(p.name for p in directory.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if not names:
        sys.exit("no images")
    out_dir = SHEET_DIR / slug(directory.name, 60)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    per = args.cols * args.rows
    cell_w, cell_h = args.thumb, int(args.thumb * 0.62) + 20
    sheets = []
    for page, offset in enumerate(range(0, len(names), per)):
        chunk = names[offset:offset + per]
        rows = (len(chunk) + args.cols - 1) // args.cols
        sheet = Image.new("RGB", (args.cols * cell_w, rows * cell_h), (18, 18, 18))
        draw = ImageDraw.Draw(sheet)
        for k, name in enumerate(chunk):
            try:
                thumb = Image.open(directory / name).convert("RGB")
            except Exception:  # noqa: BLE001
                continue
            thumb.thumbnail((cell_w - 6, cell_h - 22))
            x, y = (k % args.cols) * cell_w + 3, (k // args.cols) * cell_h + 2
            sheet.paste(thumb, (x, y))
            draw.text((x + 2, y + cell_h - 19), f"[{offset + k}] {name[-34:]}", fill=(255, 255, 0))
        path = out_dir / f"sheet_{page:03d}.jpg"
        sheet.save(path, "JPEG", quality=85)
        sheets.append(str(path))
    index = out_dir / "index.json"
    index.write_text(json.dumps(names), encoding="utf-8")
    print(json.dumps({"ok": True, "count": len(names), "sheets": sheets, "index": str(index)}))


def cmd_keep(args):
    from PIL import Image
    directory = Path(args.dir)
    names = list(args.names or [])
    if args.indexes:
        listing = sorted(p.name for p in directory.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
        names += [listing[i] for i in args.indexes if 0 <= i < len(listing)]
    source = {}
    vid = directory.name
    source_file = VIDEO_DIR / vid / "source.json"
    if source_file.exists():
        source = json.loads(source_file.read_text(encoding="utf-8"))
    out_dir = FRAME_DIR / slug(vid, 40)
    out_dir.mkdir(parents=True, exist_ok=True)
    known = {row.get("sha1") for row in read_manifest()}
    kept = []
    for name in dict.fromkeys(names):
        path = directory / name
        if not path.exists():
            continue
        image = Image.open(path).convert("RGB")
        digest = pixel_hash(image)
        if digest in known:
            continue
        known.add(digest)
        target = out_dir / f"vid_{name}"
        shutil.copyfile(path, target)
        match = re.search(r"_t(\d{7})\.jpg$", name)
        seconds = int(match.group(1)) / 100 if match else None
        append_manifest({"file": str(target.relative_to(ROOT)).replace("\\", "/"), "name": target.name, "kind": "video",
                         "url": source.get("url"), "page": source.get("url"), "source": slug(source.get("channel") or vid),
                         "title": source.get("title"), "t": seconds, "note": args.note, "width": image.size[0],
                         "height": image.size[1], "sha1": digest})
        kept.append(target.name)
    if args.clear:
        shutil.rmtree(directory, ignore_errors=True)
    print(json.dumps({"ok": True, "kept": len(kept), "dir": str(out_dir)}))


def cmd_drop(args):
    rows = read_manifest()
    targets = {Path(f).name for f in args.files}
    remaining, dropped = [], 0
    for row in rows:
        if row.get("name") in targets:
            path = ROOT / row["file"]
            if path.exists():
                path.unlink()
            dropped += 1
        else:
            remaining.append(row)
    write_manifest(remaining)
    print(json.dumps({"ok": True, "dropped": dropped, "left": len(remaining)}))


def cmd_status(_args):
    rows = read_manifest()
    counts = {}
    for row in rows:
        key = f"{row.get('kind')}:{row.get('source')}"
        counts[key] = counts.get(key, 0) + 1
    print(json.dumps({"total": len(rows), "by_source": dict(sorted(counts.items(), key=lambda kv: -kv[1]))},
                     ensure_ascii=False, indent=1))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("image")
    p.add_argument("url")
    p.add_argument("--page")
    p.add_argument("--source")
    p.add_argument("--note")
    p.add_argument("--min-side", type=int, default=300)
    p.set_defaults(func=cmd_image)
    p = sub.add_parser("video")
    p.add_argument("url")
    p.add_argument("--max-height", type=int, default=1080)
    p.add_argument("--section", help='yt-dlp --download-sections value, e.g. "*00:01:00-00:03:00"')
    p.set_defaults(func=cmd_video)
    p = sub.add_parser("frames")
    p.add_argument("video_id")
    p.add_argument("--every", type=float, default=2.0)
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--end", type=float, default=0.0)
    p.add_argument("--limit", type=int, default=400)
    p.add_argument("--max-width", type=int, default=1920)
    p.add_argument("--min-diff", type=int, default=6, help="minimum dHash bit difference from the previous kept frame")
    p.add_argument("--tag", help="staging folder name (default: the video id)")
    p.add_argument("--max-height", type=int, default=1080, help="only used when streaming from a URL")
    p.set_defaults(func=cmd_frames)
    p = sub.add_parser("sheet")
    p.add_argument("dir")
    p.add_argument("--cols", type=int, default=5)
    p.add_argument("--rows", type=int, default=5)
    p.add_argument("--thumb", type=int, default=384)
    p.set_defaults(func=cmd_sheet)
    p = sub.add_parser("keep")
    p.add_argument("dir")
    p.add_argument("--names", nargs="*")
    p.add_argument("--indexes", nargs="*", type=int)
    p.add_argument("--note")
    p.add_argument("--clear", action="store_true", help="delete the staging dir afterwards")
    p.set_defaults(func=cmd_keep)
    p = sub.add_parser("drop")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_drop)
    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
