#!/usr/bin/env python
"""Sample frames from many YouTube videos into dataset/real/pool/<id>/ for later filtering.

No judgement happens here: it only gives broad coverage. The hive detector decides later
which frames actually show a hive cell.

    .venv/Scripts/python -I tools/pool.py [--workers 3] [--max-videos N]

Reads dataset/real/video_candidates.json and skips videos that already have done.json.
Short videos are read sequentially; long streams are sampled by seeking, so only small
parts of them are downloaded.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POOL = ROOT / "dataset" / "real" / "pool"
CANDIDATES = ROOT / "dataset" / "real" / "video_candidates.json"

SKIP = re.compile(r"talent marketplace|crowdfund|pitch video|hiring|biotech|life scien|bioglow|ultimate goal|"
                  r"halloween|funk|award|covid|decode|into the deep|powerplay|freight frenzy|reefscape|rebuilt|"
                  r"\bfrc\b|lego|audiobook|webinar|online e", re.I)
PRIORITY = re.compile(r"hive|cell|assembly|ri3d|ri30h|robot in 3|robot in three|walkthrough|animation|reveal|"
                      r"match|meet|scrimmage|shooter|practice|qualifier|kickoff|pollinate|launch|record|solo", re.I)


def interval_for(duration):
    if not duration:
        return 1.0, 90
    if duration <= 60:
        return 1.0, 60
    if duration <= 300:
        return 2.0, 150
    if duration <= 900:
        return 3.0, 300
    if duration <= 3600:
        return 10.0, 360
    return max(45.0, duration / 400.0), 400


def dhash(gray_small):
    import numpy as np
    diff = gray_small[:, 1:] > gray_small[:, :-1]
    return int("".join("1" if v else "0" for v in diff.flatten()), 2)


def sample_one(vid, out_dir, require_topic=True):
    import cv2
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = ("bv*[height<=720][vcodec^=avc1][protocol=https]/bv*[height<=720][protocol=https]/"
           "b[height<=720][protocol=https]")
    got = subprocess.run([sys.executable, "-m", "yt_dlp", "--js-runtimes", "node", "--no-playlist", "-f", fmt,
                          "--print", "%(title)s", "--print", "%(duration)s", "--print", "%(channel)s", "--print", "urls",
                          f"https://www.youtube.com/watch?v={vid}"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    lines = [line for line in got.stdout.splitlines() if line.strip()]
    if got.returncode != 0 or len(lines) < 4:
        return {"ok": False, "error": (got.stderr or got.stdout)[-500:]}
    title, channel, url = lines[0], lines[2], lines[-1]
    try:
        duration = float(lines[1])
    except ValueError:
        duration = 0.0
    meta = {"id": vid, "title": title, "channel": channel, "duration": duration,
            "url": f"https://www.youtube.com/watch?v={vid}"}
    if SKIP.search(title) or (require_topic and not re.search(r"bio ?buzz|hive|ri3d|ri30h|robot in (3|three)|2026|2027|ftc", title, re.I)):
        return {"ok": True, "skipped": True, **meta, "saved": 0}
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        return {"ok": False, "error": "could not open stream", **meta}
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if not duration:
        total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        duration = total / fps if total else 0.0
    every, limit = interval_for(duration)
    saved, last, started = 0, None, time.time()

    def keep(frame, t):
        nonlocal saved, last
        height, width = frame.shape[:2]
        if width > 1280:
            frame = cv2.resize(frame, (1280, int(round(height * 1280 / width))), interpolation=cv2.INTER_AREA)
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (9, 8), interpolation=cv2.INTER_AREA)
        code = dhash(small)
        if last is not None and bin(code ^ last).count("1") < 5:
            return
        last = code
        cv2.imwrite(str(out_dir / f"{vid}_t{int(round(t * 100)):07d}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        saved += 1

    if duration > 900:
        t = every / 2
        while t < duration and saved < limit and time.time() - started < 1500:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if ok:
                keep(frame, t)
            t += every
    else:
        step = max(1, int(round(every * fps)))
        index = 0
        while saved < limit and time.time() - started < 1500:
            if not cap.grab():
                break
            if index % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    keep(frame, index / fps)
            index += 1
    cap.release()
    return {"ok": True, **meta, "saved": saved, "every": every, "seconds": round(time.time() - started)}


def sample_one_any(vid, out_dir):
    return sample_one(vid, out_dir, require_topic=False)


def run_child(vid):
    out_dir = POOL / vid
    try:
        result = subprocess.run([sys.executable, "-I", __file__, "--one", vid], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=1800)
        line = (result.stdout.strip().splitlines() or ["{}"])[-1]
        info = json.loads(line) if line.startswith("{") else {"ok": False, "error": result.stderr[-400:]}
    except subprocess.TimeoutExpired:
        info = {"ok": False, "error": "timeout"}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "done.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return vid, info


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--one")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-videos", type=int, default=0)
    args = parser.parse_args()
    if args.one:
        print(json.dumps(sample_one(args.one, POOL / args.one), ensure_ascii=False))
        return
    candidates = json.loads(CANDIDATES.read_text(encoding="utf-8"))
    todo = [c for c in candidates if not (POOL / c["id"] / "done.json").exists() and not SKIP.search(c["title"])]
    todo.sort(key=lambda c: (0 if PRIORITY.search(c["title"]) else 1, 0 if (c["duration"] or 0) <= 1200 else 1,
                             c["duration"] or 0))
    if args.max_videos:
        todo = todo[:args.max_videos]
    print(f"{len(todo)} videos to sample", flush=True)
    total = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for done, (vid, info) in enumerate(executor.map(run_child, [c["id"] for c in todo]), 1):
            total += info.get("saved", 0) or 0
            status = "skip" if info.get("skipped") else ("ok" if info.get("ok") else "FAIL")
            print(f"[{done}/{len(todo)}] {vid} {status} saved={info.get('saved', 0)} total={total} "
                  f"{(info.get('title') or info.get('error') or '')[:70]!r}", flush=True)


if __name__ == "__main__":
    main()
