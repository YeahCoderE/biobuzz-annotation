#!/usr/bin/env python
"""BioBuzz Photo Link 2.0 - Windows receiver.

    BioBuzzPhotoLink.exe               tray icon + server; opens the window on the first start
    BioBuzzPhotoLink.exe --background  same, but no window (used by "start with Windows")
    BioBuzzPhotoLink.exe --headless    server only, no tray, no window (tests / servers)
"""
import argparse
import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I does not add the script folder
import core as C  # noqa: E402
import server  # noqa: E402

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "BioBuzzPhotoLink"
INSTANCE_FILE = C.HOME / "instance.json"


# ---- helpers ---------------------------------------------------------------------------------
def autostart_command() -> str:
    exe = sys.executable if getattr(sys, "frozen", False) else f'"{sys.executable}" "{Path(__file__).resolve()}"'
    return f'"{exe}" --background' if getattr(sys, "frozen", False) else f"{exe} --background"


def set_autostart(enabled: bool) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass
        return enabled
    except OSError:
        return not enabled


def find_edge():
    for p in (shutil.which("msedge"),
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if p and Path(p).exists():
            return p
    return None


def open_window(port: int) -> None:
    url = f"http://127.0.0.1:{port}/"
    edge = find_edge()
    try:
        if edge:
            subprocess.Popen([edge, f"--app={url}", "--window-size=1180,780"])
            return
    except OSError:
        pass
    webbrowser.open(url)


def other_instance_port():
    """If another copy is already running, return its port (and ask it to show its window)."""
    try:
        info = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
        port = int(info["port"])
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/show", method="POST", data=b"{}",
                                     headers={"X-BB": "1", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
        return port
    except Exception:
        return None


def single_instance_lock():
    """Named mutex: returns False when another copy already holds it."""
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\BioBuzzPhotoLinkMutex")
        return ctypes.windll.kernel32.GetLastError() != 183, handle
    except Exception:
        return True, None


def load_icon():
    from PIL import Image
    path = C.resource("icon.png")
    if path.exists():
        return Image.open(path)
    return Image.new("RGB", (64, 64), (245, 165, 36))


# ---- tray ------------------------------------------------------------------------------------
def run_tray(core: C.Core, loop: asyncio.AbstractEventLoop, shutdown, show_first: bool) -> None:
    import pystray

    state = {"last_notify": 0.0, "pending": 0}

    def show(*_):
        open_window(core.port)

    def open_folder(*_):
        core.save_dir().mkdir(parents=True, exist_ok=True)
        os.startfile(str(core.save_dir()))

    def toggle_autostart(icon, item):
        core.cfg.update(autostart=set_autostart(not core.cfg.get("autostart")))

    def toggle_pause(icon, item):
        core.paused = not core.paused
        core.log("receiving paused" if core.paused else "receiving resumed")
        core.emit({"t": "state", "paused": core.paused})

    def quit_app(icon, item):
        fut = asyncio.run_coroutine_threadsafe(shutdown(), loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass
        icon.stop()

    def status_text(item):
        n = sum(1 for d in core.devices_view() if d["online"])
        return f"{n} phone(s) connected - {core.stats.today()} photos today"

    menu = pystray.Menu(
        pystray.MenuItem("Open BioBuzz Photo Link", show, default=True),
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open photos folder", open_folder),
        pystray.MenuItem("Pause receiving", toggle_pause, checked=lambda i: core.paused),
        pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda i: bool(core.cfg.get("autostart"))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("BioBuzzPhotoLink", load_icon(), "BioBuzz Photo Link", menu)
    core.hooks["show"] = show

    # a balloon when photos arrive (grouped, at most one every 8 s) - only if the window is not in use
    def notifier():
        async def pump():
            q = asyncio.Queue(maxsize=200)
            core.subscribers.add(q)  # created on the loop thread, like every other subscriber
            while True:
                ev = await q.get()
                if ev.get("t") == "photo" and core.cfg.get("notify"):
                    state["pending"] += 1
                    if time.time() - state["last_notify"] > 8:
                        n, state["pending"], state["last_notify"] = state["pending"], 0, time.time()
                        try:
                            icon.notify(f"{n} new photo(s) from {ev.get('device', 'phone')}", "BioBuzz Photo Link")
                        except Exception:
                            pass

        asyncio.run_coroutine_threadsafe(pump(), loop)

    def setup(icon):
        icon.visible = True
        notifier()
        if show_first:
            show()

    icon.run(setup)


# ---- main ------------------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=C.APP)
    ap.add_argument("--background", action="store_true", help="start in the tray without a window")
    ap.add_argument("--headless", action="store_true", help="no tray, no window (tests)")
    ap.add_argument("--save-dir", help="override the folder for received photos (also saved)")
    ap.add_argument("--port", type=int, help="override the port (also saved)")
    args = ap.parse_args()

    if not args.headless:
        first, _handle = single_instance_lock()
        if not first:
            port = other_instance_port()
            return 0 if port else 1

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    core = C.Core(loop)
    core.hooks["autostart"] = set_autostart
    if args.save_dir:
        core.cfg.update(save_dir=args.save_dir)
    if args.port:
        core.cfg.update(port=args.port)

    try:
        shutdown = loop.run_until_complete(server.start(core))
    except OSError as exc:
        print(f"Cannot start the server: {exc}", file=sys.stderr)
        return 1
    INSTANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    INSTANCE_FILE.write_text(json.dumps({"port": core.port, "pid": os.getpid()}), encoding="utf-8")
    if core.cfg.get("autostart"):
        set_autostart(True)  # keep the registry entry pointing at this exe if it moved

    if args.headless:
        print(f"{C.APP} {C.VERSION} on port {core.port}, saving to {core.cfg['save_dir']}")
        print("pairing link:", core.pair_link(), flush=True)
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            pass
        loop.run_until_complete(shutdown())
        return 0

    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        run_tray(core, loop, shutdown, show_first=not args.background)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        try:
            INSTANCE_FILE.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
