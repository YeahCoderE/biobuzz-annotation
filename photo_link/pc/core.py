"""BioBuzz Photo Link 2.0 - state and storage of the PC receiver (no networking in this file).

Everything here runs on the asyncio thread of server.py except the small blocking helpers that
server.py sends to an executor (thumbnails, folder dialog).
"""
import asyncio
import datetime as dt
import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import socket
import sys
import time
from collections import deque
from pathlib import Path

APP = "BioBuzz Photo Link"
VERSION = "2.0"
DEFAULT_PORT = 8777
DISCOVERY_PORT = 8778
CHUNK = 256 * 1024
MAX_PHOTO = 120 * 1024 * 1024
TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ID_RE = re.compile(r"[^A-Za-z0-9-]")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

HOME = Path(os.environ.get("BIOBUZZ_HOME") or (Path(os.environ.get("APPDATA") or Path.home()) / "BioBuzzPhotoLink"))
THUMBS = HOME / "thumbs"
TRASH = HOME / "trash"


def app_dir() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def resource(rel: str) -> Path:
    """File shipped with the app (PyInstaller unpacks to sys._MEIPASS)."""
    base = Path(getattr(sys, "_MEIPASS", app_dir()))
    return base / rel


def find_default_dir() -> Path:
    """dataset/new_photos of the project: look upwards from the exe / script, then from the cwd."""
    for start in (app_dir(), Path.cwd()):
        for p in (start, *start.parents):
            cand = p / "dataset" / "new_photos"
            if cand.is_dir():
                return cand
    fallback = Path(r"D:\Robotics\Model\dataset\new_photos")
    return fallback if fallback.parent.exists() else Path.home() / "Pictures" / "BioBuzz"


def make_token() -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(12))


def fmt_token(t: str) -> str:
    return "-".join(t[i:i + 4] for i in range(0, len(t), 4))


def norm_token(t: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", t or "").upper()


def local_ips() -> list:
    """IPv4 addresses of this PC: the default-route one first (the Wi-Fi the phone is on)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # UDP connect only picks the route, nothing is sent
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


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------------------------
class Config:
    DEFAULTS = {
        "token": None, "pc_id": None, "pc_name": None, "save_dir": None, "subfolders": "flat",
        "lang": "he", "autostart": False, "notify": True, "port": DEFAULT_PORT,
    }

    def __init__(self):
        self.path = HOME / "config.json"
        self.data = dict(self.DEFAULTS)
        self.data.update(read_json(self.path, {}))
        changed = False
        if not self.data.get("token") or len(self.data["token"]) < 8:
            self.data["token"] = make_token()
            changed = True
        if not self.data.get("pc_id"):
            self.data["pc_id"] = secrets.token_hex(8)
            changed = True
        if not self.data.get("pc_name"):
            self.data["pc_name"] = socket.gethostname()
            changed = True
        if not self.data.get("save_dir"):
            self.data["save_dir"] = str(find_default_dir())
            changed = True
        if changed:
            self.save()

    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, d=None):
        return self.data.get(k, d)

    def update(self, **kw) -> None:
        self.data.update(kw)
        self.save()

    def save(self) -> None:
        write_json(self.path, self.data)


class Stats:
    """Counters that survive restarts: photos and bytes per day."""

    def __init__(self):
        self.path = HOME / "stats.json"
        d = read_json(self.path, {})
        self.total = int(d.get("total", 0))
        self.bytes = int(d.get("bytes", 0))
        self.days = dict(d.get("days", {}))
        self.session = 0
        self.session_bytes = 0
        self.dirty = False

    def add(self, size: int) -> None:
        day = dt.date.today().isoformat()
        self.days[day] = self.days.get(day, 0) + 1
        self.total += 1
        self.bytes += size
        self.session += 1
        self.session_bytes += size
        self.dirty = True

    def today(self) -> int:
        return self.days.get(dt.date.today().isoformat(), 0)

    def flush(self) -> None:
        if self.dirty:
            keep = dict(sorted(self.days.items())[-120:])
            write_json(self.path, {"total": self.total, "bytes": self.bytes, "days": keep})
            self.dirty = False


class Devices:
    """Phones that ever connected (persistent) plus their live state (in memory)."""

    def __init__(self):
        self.path = HOME / "devices.json"
        self.known = read_json(self.path, {})
        self.live = {}

    def save(self) -> None:
        write_json(self.path, self.known)

    def touch(self, dev_id: str, name: str, ip: str) -> dict:
        d = self.known.setdefault(dev_id, {"first": time.time(), "count": 0, "bytes": 0, "blocked": False})
        d["name"] = name or d.get("name") or "Phone"
        d["last"] = time.time()
        d["ip"] = ip
        return d

    def view(self) -> list:
        out = []
        for dev_id, d in self.known.items():
            lv = self.live.get(dev_id)
            out.append({
                "id": dev_id, "name": d.get("name", "Phone"), "first": d.get("first"), "last": d.get("last"),
                "count": d.get("count", 0), "bytes": d.get("bytes", 0), "blocked": d.get("blocked", False),
                "ip": d.get("ip", ""), "online": bool(lv and lv["conns"] > 0),
                "conns": lv["conns"] if lv else 0,
                "battery": lv.get("battery") if lv else None, "charging": lv.get("charging") if lv else None,
                "queue": lv.get("queue", 0) if lv else 0, "queueBytes": lv.get("queueBytes", 0) if lv else 0,
                "rssi": lv.get("rssi") if lv else None, "latency": lv.get("latency") if lv else None,
                "mbps": lv.get("mbps") if lv else None,
            })
        out.sort(key=lambda x: (not x["online"], -(x["last"] or 0)))
        return out


class Core:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        HOME.mkdir(parents=True, exist_ok=True)
        self.loop = loop
        self.cfg = Config()
        self.stats = Stats()
        self.devices = Devices()
        self.sessions = set()
        self.subscribers = set()
        self.log_lines = deque(maxlen=1500)
        self.seen = read_json(HOME / "seen.json", {})
        self.rx_bytes = 0          # total received this run (photos + speed tests)
        self.speed_hist = deque([0.0] * 60, maxlen=60)
        self.port = self.cfg["port"]
        self.started = time.time()
        self.last_photo = None
        self.paused = False
        self.recent = deque(maxlen=60)
        self.hooks = {}            # set by main.py: show(), autostart(bool)
        self._flush_handle = None
        self._setup_logging()
        self.clean_partials()

    # ---- logging / events ---------------------------------------------------------------
    def _setup_logging(self) -> None:
        self.logger = logging.getLogger("biobuzz")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        fh = logging.handlers.RotatingFileHandler(HOME / "link.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        self.logger.addHandler(fh)

    def log(self, msg: str, level: str = "info") -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.log_lines.append({"ts": time.time(), "level": level, "msg": msg})
        getattr(self.logger, "warning" if level == "warn" else "info")(msg)
        self.emit({"t": "log", "line": line, "level": level})

    def emit(self, ev: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    # ---- folders --------------------------------------------------------------------------
    def save_dir(self) -> Path:
        return Path(self.cfg["save_dir"])

    def clean_partials(self) -> None:
        try:
            for p in self.save_dir().glob(".*.part"):
                p.unlink(missing_ok=True)
        except OSError:
            pass

    def part_path(self, photo_id: str) -> Path:
        d = self.save_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d / f".{photo_id}.part"

    def is_seen(self, photo_id: str) -> str:
        return self.seen.get(photo_id, "")

    # ---- storing a photo ------------------------------------------------------------------
    def finalize(self, part: Path, photo_id: str, taken_ms: int, dev_id: str, dev_name: str, size: int) -> str:
        """Move the finished temp file to its final name. Returns the path relative to the save dir."""
        when = dt.datetime.fromtimestamp(taken_ms / 1000 if taken_ms > 0 else time.time())
        mode = self.cfg.get("subfolders", "flat")
        base = self.save_dir()
        if mode == "day":
            base = base / when.strftime("%Y-%m-%d")
        elif mode == "device":
            base = base / (re.sub(r'[\\/:*?"<>|]', "_", dev_name).strip() or "phone")
        base.mkdir(parents=True, exist_ok=True)
        stem = when.strftime("%Y%m%d_%H%M%S")
        name, n = f"{stem}.jpg", 1
        while (base / name).exists():
            name = f"{stem}_{n}.jpg"
            n += 1
        os.replace(part, base / name)
        rel = (base / name).relative_to(self.save_dir()).as_posix()
        if photo_id:
            self.seen[photo_id] = rel
            if len(self.seen) > 6000:
                self.seen = dict(list(self.seen.items())[-3000:])
        self.stats.add(size)
        d = self.devices.known.get(dev_id)
        if d is not None:
            d["count"] = d.get("count", 0) + 1
            d["bytes"] = d.get("bytes", 0) + size
        self.last_photo = {"name": rel, "ts": time.time(), "size": size, "device": dev_name}
        self.recent.appendleft(self.last_photo)
        self._schedule_flush()
        self.emit({"t": "photo", "name": rel, "size": size, "device": dev_name, "ts": time.time(),
                   "today": self.stats.today(), "total": self.stats.total})
        self.log(f"received {rel} ({size / 1024:.0f} KB) from {dev_name}")
        return rel

    def _schedule_flush(self) -> None:
        if self._flush_handle is None:
            self._flush_handle = self.loop.call_later(2.0, self.flush)

    def flush(self) -> None:
        self._flush_handle = None
        try:
            self.stats.flush()
            self.devices.save()
            write_json(HOME / "seen.json", self.seen)
        except OSError as exc:
            self.log(f"cannot write state: {exc}", "warn")

    # ---- devices / sessions --------------------------------------------------------------
    def devices_view(self) -> list:
        return self.devices.view()

    def emit_devices(self) -> None:
        self.emit({"t": "devices", "devices": self.devices_view()})

    async def close_all(self, code: str, only_device: str = "") -> None:
        for s in list(self.sessions):
            if not only_device or s.dev_id == only_device:
                await s.bye(code)

    def new_code(self) -> None:
        self.cfg.update(token=make_token())
        self.log("pairing code renewed, all phones disconnected")

    def pair_link(self) -> str:
        from urllib.parse import quote
        return (f"biobuzz://pair?h={','.join(local_ips())}&p={self.port}&t={self.cfg['token']}"
                f"&n={quote(self.cfg['pc_name'])}&i={self.cfg['pc_id']}")

    def sample_speed(self) -> None:
        """Called once per second."""
        last = getattr(self, "_last_rx", 0)
        bps = (self.rx_bytes - last)
        self._last_rx = self.rx_bytes
        self.speed_hist.append(float(bps))
        if bps or any(self.speed_hist):
            self.emit({"t": "speed", "bps": bps})

    def disk_free(self) -> int:
        try:
            p = self.save_dir()
            while not p.exists() and p != p.parent:
                p = p.parent
            return shutil.disk_usage(p).free
        except OSError:
            return 0

    # ---- photos listing / thumbnails ------------------------------------------------------
    def safe_path(self, rel: str):
        """Resolve a relative photo path and make sure it stays inside the save folder."""
        base = self.save_dir().resolve()
        try:
            p = (base / rel).resolve()
            p.relative_to(base)
        except (ValueError, OSError):
            return None
        return p if p.suffix.lower() in IMAGE_EXT else None

    def list_photos(self) -> list:
        base = self.save_dir()
        items = []
        if not base.exists():
            return items

        def scan(folder: Path, depth: int):
            try:
                with os.scandir(folder) as it:
                    for e in it:
                        if e.name.startswith("."):
                            continue
                        if e.is_dir(follow_symlinks=False) and depth < 2:
                            scan(Path(e.path), depth + 1)
                        elif e.is_file() and Path(e.name).suffix.lower() in IMAGE_EXT:
                            st = e.stat()
                            items.append({"name": Path(e.path).relative_to(base).as_posix(),
                                          "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                pass

        scan(base, 0)
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items

    def thumb_file(self, p: Path) -> Path:
        st = p.stat()
        key = hashlib.sha1(f"{p}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()[:20]
        return THUMBS / f"{key}.jpg"

    def make_thumb(self, src: Path, dst: Path) -> None:
        from PIL import Image, ImageOps
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im.draft("RGB", (640, 640))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((420, 420))
            tmp = dst.with_suffix(".tmp")
            im.save(tmp, "JPEG", quality=80)
        os.replace(tmp, dst)

    def trash(self, rels: list) -> int:
        n = 0
        TRASH.mkdir(parents=True, exist_ok=True)
        for rel in rels:
            p = self.safe_path(rel)
            if not p or not p.exists():
                continue
            dst = TRASH / f"{int(time.time())}_{p.name}"
            try:
                shutil.move(str(p), str(dst))
                n += 1
            except OSError as exc:
                self.log(f"cannot delete {rel}: {exc}", "warn")
        if n:
            self.log(f"moved {n} photo(s) to the trash folder")
            self.emit({"t": "deleted", "names": rels})
        return n

    # ---- state for the UI -----------------------------------------------------------------
    def state(self) -> dict:
        online = [d for d in self.devices_view() if d["online"]]
        return {
            "app": APP, "version": VERSION, "pc_name": self.cfg["pc_name"], "port": self.port,
            "uptime": time.time() - self.started, "paused": self.paused,
            "ips": local_ips(), "devices": self.devices_view(), "online": len(online),
            "today": self.stats.today(), "total": self.stats.total, "bytes": self.stats.bytes,
            "session": self.stats.session, "session_bytes": self.stats.session_bytes,
            "speed": list(self.speed_hist), "disk_free": self.disk_free(),
            "last_photo": self.last_photo, "recent": list(self.recent)[:12],
            "config": {k: self.cfg.get(k) for k in ("save_dir", "subfolders", "lang", "autostart", "notify", "port", "pc_name")},
            "code": fmt_token(self.cfg["token"]), "pair_link": self.pair_link(),
        }
