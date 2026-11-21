#!/usr/bin/env python
"""Run tools/train.py and restart it with --resume if it crashes (out of memory, etc.).

    .venv/Scripts/python -I tools/run_training.py --name v5 --model runs/v3_final.pt --epochs 10 ...
Extra arguments go to train.py. Gives up after 40 consecutive failures.
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
args = sys.argv[1:]
name = args[args.index("--name") + 1]
cmd = [sys.executable, "-I", str(HERE / "train.py")] + args
failures = 0
while failures < 40:
    code = subprocess.call(cmd)
    if code == 0:
        print("training finished", flush=True)
        break
    failures += 1
    print(f"train.py exited with {code}; resume #{failures} in 60 s", flush=True)
    time.sleep(60)
    cmd = [sys.executable, "-I", str(HERE / "train.py"), "--name", name, "--resume"]
