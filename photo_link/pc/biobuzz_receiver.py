#!/usr/bin/env python
"""BioBuzz Photo Link - PC receiver.

Runs a small HTTP server on the local network. The phone app (BioBuzzPhotoLink.apk) pairs by
scanning the QR code shown here and then sends every photo it takes straight into the
`dataset\\new_photos` folder of the BioBuzz project.

    BioBuzzPhotoLink.exe              window with QR code and the list of received photos
    BioBuzzPhotoLink.exe --headless   no window, prints the pairing link (for tests / servers)

Protocol (plain HTTP on the LAN, protected by a secret token that is part of the QR code):
    GET  /ping?t=TOKEN                       -> {"ok": true, ...}
    POST /upload?t=TOKEN   body = JPEG       -> {"ok": true, "name": "20261121_101500.jpg"}
         headers: X-Photo-Id (unique id, makes retries idempotent), X-Taken-At (epoch ms)
"""
import argparse
import datetime as dt
import hmac
import json
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP = "BioBuzz Photo Link"
VERSION = "1.0"
DEFAULT_PORT = 8777
MAX_BYTES = 40 * 1024 * 1024
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "BioBuzzPhotoLink"
CONFIG_FILE = CONFIG_DIR / "config.json"
SEEN_FILE = CONFIG_DIR / "seen.json"
ID_RE = re.compile(r"[^A-Za-z0-9-]")


def here() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def find_default_dir() -> Path:
    """dataset/new_photos of the project: look upwards from the exe / script, then from the cwd."""
    for start in (here(), Path.cwd()):
        for p in (start, *start.parents):
            cand = p / "dataset" / "new_photos"
            if cand.is_dir():
                return cand
    fallback = Path(r"D:\Robotics\Model\dataset\new_photos")
    return fallback if fallback.parent.exists() else Path.home() / "Pictures" / "BioBuzz"


def load_config() -> dict:
    cfg = {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    changed = False
    if not isinstance(cfg.get("token"), str) or len(cfg["token"]) < 8:
        cfg["token"] = secrets.token_urlsafe(9)
        changed = True
    if not cfg.get("save_dir"):
        cfg["save_dir"] = str(find_default_dir())
        changed = True
    if not isinstance(cfg.get("port"), int):
        cfg["port"] = DEFAULT_PORT
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=1), encoding="utf-8")


def local_ips() -> list:
    """IPv4 addresses of this PC: the default-route one first (the Wi-Fi the phone is on)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # UDP connect: only picks the route, sends nothing
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith(("127.", "169.254.")):
                ips.append(ip)
    except OSError:
        pass
    return ips


class Receiver:
    """HTTP server plus the code that stores a photo atomically and without duplicates."""

    def __init__(self, cfg: dict, on_event=None):
        self.cfg = cfg
        self.on_event = on_event or (lambda *a: None)
        self.lock = threading.Lock()
        self.server = None
        self.port = cfg["port"]
        self.count = 0
        try:
            self.seen = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.seen = {}

    # ---- server -------------------------------------------------------------------------
    def start(self) -> int:
        handler = make_handler(self)
        last = None
        for port in range(self.cfg["port"], self.cfg["port"] + 20):
            try:
                self.server = ThreadingHTTPServer(("0.0.0.0", port), handler)
                self.port = port
                break
            except OSError as exc:
                last = exc
        if self.server is None:
            raise last
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def pair_link(self) -> str:
        return f"biobuzz://pair?h={','.join(local_ips())}&p={self.port}&t={self.cfg['token']}"

    def token_ok(self, token: str) -> bool:
        return hmac.compare_digest(token.encode("utf-8", "replace"), self.cfg["token"].encode())

    # ---- storing ------------------------------------------------------------------------
    def store(self, body: bytes, photo_id: str, taken_ms: int) -> tuple:
        """Returns (file name, was_duplicate)."""
        photo_id = ID_RE.sub("", photo_id)[:64]
        with self.lock:
            if photo_id and photo_id in self.seen:
                return self.seen[photo_id], True
            folder = Path(self.cfg["save_dir"])
            folder.mkdir(parents=True, exist_ok=True)
            when = dt.datetime.fromtimestamp(taken_ms / 1000 if taken_ms > 0 else time.time())
            stem = when.strftime("%Y%m%d_%H%M%S")
            name, n = f"{stem}.jpg", 1
            while (folder / name).exists():
                name = f"{stem}_{n}.jpg"
                n += 1
            part = folder / f".{name}.part"
            part.write_bytes(body)
            os.replace(part, folder / name)  # the file appears complete or not at all
            if photo_id:
                self.seen[photo_id] = name
                if len(self.seen) > 5000:
                    self.seen = dict(list(self.seen.items())[-2500:])
                try:
                    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                    SEEN_FILE.write_text(json.dumps(self.seen), encoding="utf-8")
                except OSError:
                    pass
            self.count += 1
        self.on_event("photo", name, len(body))
        return name, False


def make_handler(rx: Receiver):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BioBuzzLink/" + VERSION
        timeout = 60

        def log_message(self, *args):  # no console spam
            pass

        def reply(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorised(self) -> bool:
            token = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
            return rx.token_ok(token)

        def do_GET(self):
            if urlparse(self.path).path != "/ping":
                return self.reply(404, {"ok": False, "error": "not found"})
            if not self.authorised():
                return self.reply(403, {"ok": False, "error": "wrong code"})
            rx.on_event("ping", self.client_address[0], 0)
            self.reply(200, {"ok": True, "app": APP, "version": VERSION, "received": rx.count})

        def do_POST(self):
            if urlparse(self.path).path != "/upload":
                return self.reply(404, {"ok": False, "error": "not found"})
            if not self.authorised():
                return self.reply(403, {"ok": False, "error": "wrong code"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0:
                return self.reply(411, {"ok": False, "error": "length required"})
            if length > MAX_BYTES:
                return self.reply(413, {"ok": False, "error": "photo too large"})
            body = self.rfile.read(length)
            if len(body) != length or body[:3] != b"\xff\xd8\xff":
                return self.reply(400, {"ok": False, "error": "not a complete JPEG"})
            try:
                taken = int(self.headers.get("X-Taken-At", "0"))
            except ValueError:
                taken = 0
            try:
                name, dup = rx.store(body, self.headers.get("X-Photo-Id", ""), taken)
            except OSError as exc:
                return self.reply(500, {"ok": False, "error": f"cannot save: {exc}"})
            self.reply(200, {"ok": True, "name": name, "duplicate": dup})

    return Handler


# ---------------------------------------------------------------------------------------------
def run_gui(rx: Receiver, events: queue.Queue) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    import qrcode
    from PIL import ImageTk

    root = tk.Tk()
    root.title(APP)
    root.minsize(720, 480)
    BG, FG, ACC = "#14171c", "#e8eaed", "#f5a524"
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
    style.configure("Big.TLabel", font=("Segoe UI", 18, "bold"), foreground=ACC)
    style.configure("Dim.TLabel", foreground="#9aa0a6")
    style.configure("TButton", font=("Segoe UI", 10), padding=6)

    left, right = ttk.Frame(root, padding=16), ttk.Frame(root, padding=16)
    left.pack(side="left", fill="y")
    right.pack(side="left", fill="both", expand=True)

    ttk.Label(left, text="BioBuzz Photo Link", style="Big.TLabel").pack(anchor="w")
    ttk.Label(left, text="Scan with the phone app", style="Dim.TLabel").pack(anchor="w", pady=(0, 8))
    qr_label = tk.Label(left, bg=BG)
    qr_label.pack()
    addr_var, folder_var, count_var, state_var = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
    ttk.Label(left, textvariable=addr_var, style="Dim.TLabel", wraplength=300, justify="left").pack(anchor="w", pady=(8, 0))
    ttk.Label(left, textvariable=state_var, wraplength=300, justify="left").pack(anchor="w", pady=(8, 0))

    ttk.Label(right, text="Received photos", style="Big.TLabel").pack(anchor="w")
    ttk.Label(right, textvariable=count_var).pack(anchor="w")
    ttk.Label(right, textvariable=folder_var, style="Dim.TLabel", wraplength=380).pack(anchor="w", pady=(0, 8))
    listbox = tk.Listbox(right, bg="#1d2127", fg=FG, bd=0, highlightthickness=0, font=("Consolas", 10),
                         selectbackground="#2b3139", activestyle="none")
    listbox.pack(fill="both", expand=True)
    buttons = ttk.Frame(right)
    buttons.pack(fill="x", pady=(10, 0))

    session = {"n": 0}

    def refresh_qr() -> None:
        link = rx.pair_link()
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=3)
        qr.add_data(link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB").resize((300, 300))
        photo = ImageTk.PhotoImage(img)
        qr_label.configure(image=photo)
        qr_label.image = photo
        addr_var.set(f"{', '.join(local_ips()) or 'no network'}  port {rx.port}\ncode: {rx.cfg['token']}")

    def refresh_texts() -> None:
        folder_var.set(f"Saving to: {rx.cfg['save_dir']}")
        count_var.set(f"{session['n']} photos this session")

    def open_folder() -> None:
        Path(rx.cfg["save_dir"]).mkdir(parents=True, exist_ok=True)
        os.startfile(rx.cfg["save_dir"])

    def choose_folder() -> None:
        d = filedialog.askdirectory(initialdir=rx.cfg["save_dir"], title="Folder for received photos")
        if d:
            rx.cfg["save_dir"] = d
            save_config(rx.cfg)
            refresh_texts()

    def new_code() -> None:
        if messagebox.askyesno(APP, "A new code disconnects every paired phone until it scans the new QR. Continue?"):
            rx.cfg["token"] = secrets.token_urlsafe(9)
            save_config(rx.cfg)
            refresh_qr()

    for text, cmd in (("Open folder", open_folder), ("Change folder", choose_folder), ("New code", new_code)):
        ttk.Button(buttons, text=text, command=cmd).pack(side="left", padx=(0, 8))

    def pump() -> None:
        try:
            while True:
                kind, a, b = events.get_nowait()
                if kind == "photo":
                    session["n"] += 1
                    listbox.insert(0, f"{time.strftime('%H:%M:%S')}  {a}  {b / 1024:.0f} KB")
                    if listbox.size() > 300:
                        listbox.delete(299, "end")
                    state_var.set(f"Phone connected. Last photo {time.strftime('%H:%M:%S')}")
                    refresh_texts()
                elif kind == "ping":
                    state_var.set(f"Phone {a} connected")
        except queue.Empty:
            pass
        root.after(200, pump)

    state_var.set("Waiting for the phone...\nIf Windows asks, allow access on private networks.")
    refresh_qr()
    refresh_texts()
    pump()
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


def main() -> int:
    ap = argparse.ArgumentParser(description=APP)
    ap.add_argument("--headless", action="store_true", help="no window; print the pairing link")
    ap.add_argument("--save-dir", help="override the folder for received photos (also saved)")
    args = ap.parse_args()
    cfg = load_config()
    if args.save_dir:
        cfg["save_dir"] = args.save_dir
        save_config(cfg)
    events: queue.Queue = queue.Queue()
    rx = Receiver(cfg, lambda *a: events.put(a))
    try:
        rx.start()
    except OSError as exc:
        print(f"Cannot start the server: {exc}", file=sys.stderr)
        return 1
    if args.headless:
        print(f"{APP} {VERSION} listening on port {rx.port}, saving to {cfg['save_dir']}")
        print("pairing link:", rx.pair_link(), flush=True)
        try:
            while True:
                kind, a, b = events.get()
                if kind == "photo":
                    print(f"{time.strftime('%H:%M:%S')} received {a} ({b} bytes)", flush=True)
        except KeyboardInterrupt:
            pass
    else:
        run_gui(rx, events)
    rx.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
