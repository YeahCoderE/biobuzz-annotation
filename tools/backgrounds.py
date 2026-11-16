#!/usr/bin/env python
"""Background frames WITHOUT hive cells: previous-season FTC videos (same venues, fields,
robots and crowds, but before BIOBUZZ existed) plus indoor scenes.

    .venv/Scripts/python -I tools/backgrounds.py [--per-query 6] [--frames 45]
"""
import argparse, json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dataset" / "backgrounds"
QUERIES = [
    "FTC DECODE league meet full", "FTC DECODE qualifier matches", "FTC Into The Deep league meet",
    "FTC Into the Deep qualifier", "FTC CENTERSTAGE league meet", "FTC POWERPLAY qualifier matches",
    "FTC DECODE robot reveal", "FTC Into the Deep robot reveal", "FTC DECODE field walkthrough",
    "FTC Into the Deep game animation field tour", "FIRST Tech Challenge world championship 2026 DECODE",
    "FTC scrimmage 2025", "robotics team workshop tour", "high school robotics lab tour",
    "FTC practice field robot test 2025", "FRC REBUILT match 2026",
]


def search(query, n):
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--js-runtimes", "node", "--flat-playlist", "--print",
                        "%(id)s|%(duration)s|%(title)s", f"ytsearch{n}:{query}"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            try:
                dur = float(parts[1])
            except ValueError:
                dur = 0
            out.append((parts[0], dur, parts[2]))
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-query", type=int, default=6)
    ap.add_argument("--frames", type=int, default=45)
    args = ap.parse_args()
    seen = set()
    for q in QUERIES:
        for vid, dur, title in search(q, args.per_query):
            if vid in seen or "biobuzz" in title.lower() or (OUT / vid / "done.json").exists():
                continue
            seen.add(vid)
            if dur and (dur < 20 or dur > 6 * 3600):
                continue
            out_dir = OUT / vid
            # reuse the pool sampler with a fixed frame budget
            pool.interval_for = lambda d, n=args.frames: ((max(1.0, d / n), n) if d else (2.0, n))
            pool.SKIP = __import__("re").compile(r"biobuzz|bio buzz|hive", __import__("re").I)
            try:
                info = pool.sample_one_any(vid, out_dir)
            except Exception as exc:  # noqa: BLE001
                info = {"ok": False, "error": str(exc)}
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "done.json").write_text(json.dumps({**info, "query": q}, ensure_ascii=False), encoding="utf-8")
            print(vid, info.get("saved"), title[:70], flush=True)


if __name__ == "__main__":
    main()
