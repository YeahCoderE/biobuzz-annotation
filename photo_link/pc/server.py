"""BioBuzz Photo Link 2.0 - network side of the PC receiver.

* /ws          WebSocket for phones (token protected, LAN)
* /events, /api/*, /thumb, /photo, /   the local web UI (only reachable from this PC)
* UDP 8778     discovery: a phone broadcasts "BBLINK?1", the PC answers with its addresses
"""
import asyncio
import hmac
import io
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from aiohttp import WSMsgType, web

import core as C

FAILS = {}          # ip -> (count, first time): crude brake on token guessing
LOCAL = {"127.0.0.1", "::1", "localhost"}


# --------------------------------------------------------------------------------------------
class PhoneSession:
    def __init__(self, core: C.Core, ws: web.WebSocketResponse, ip: str):
        self.core, self.ws, self.ip = core, ws, ip
        self.dev_id = ""
        self.name = "Phone"
        self.role = "worker"
        self.cur = None
        self.closed = False

    async def send(self, obj: dict) -> None:
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(obj))

    async def bye(self, code: str) -> None:
        try:
            await self.send({"t": "bye", "code": code})
            await self.ws.close(code=4003)
        except Exception:
            pass

    # ---- main loop ------------------------------------------------------------------------
    async def run(self) -> None:
        core = self.core
        try:
            if not await self.handshake():
                return
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT:
                    await self.on_text(json.loads(msg.data))
                elif msg.type == WSMsgType.BINARY:
                    await self.on_binary(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception as exc:  # a bad frame must not take the server down
            core.log(f"session error ({self.name}): {exc!r}", "warn")
        finally:
            self.cleanup()

    async def handshake(self) -> bool:
        core = self.core
        try:
            msg = await asyncio.wait_for(self.ws.receive(), 8)
        except asyncio.TimeoutError:
            return False
        if msg.type != WSMsgType.TEXT:
            return False
        try:
            h = json.loads(msg.data)
        except ValueError:
            return False
        if h.get("t") != "hello":
            return False
        count, first = FAILS.get(self.ip, (0, time.time()))
        if count >= 8 and time.time() - first < 60:
            await self.send({"t": "error", "code": "too_many_tries"})
            await asyncio.sleep(1)
            await self.ws.close(code=4003)
            return False
        token = C.norm_token(str(h.get("token", "")))
        if not hmac.compare_digest(token.encode(), core.cfg["token"].encode()):
            FAILS[self.ip] = (count + 1 if time.time() - first < 60 else 1, first if time.time() - first < 60 else time.time())
            core.log(f"rejected connection from {self.ip}: wrong code", "warn")
            await self.send({"t": "error", "code": "bad_token"})
            await asyncio.sleep(0.5)
            await self.ws.close(code=4003)
            return False
        FAILS.pop(self.ip, None)
        self.dev_id = C.ID_RE.sub("", str(h.get("device", "")))[:40]
        if not self.dev_id:
            return False
        self.name = str(h.get("name") or "Phone")[:40]
        self.role = "main" if h.get("role") == "main" else "worker"
        dev = core.devices.touch(self.dev_id, self.name, self.ip)
        if dev.get("blocked"):
            await self.send({"t": "error", "code": "blocked"})
            await self.ws.close(code=4003)
            return False
        core.sessions.add(self)
        live = core.devices.live.setdefault(self.dev_id, {"conns": 0})
        if live["conns"] == 0:
            core.log(f"{self.name} connected from {self.ip}")
        live["conns"] += 1
        core.emit_devices()
        core._schedule_flush()
        await self.send({"t": "hello_ok", "v": 2, "pc": core.cfg["pc_id"], "name": core.cfg["pc_name"], "chunk": C.CHUNK})
        return True

    def cleanup(self) -> None:
        core = self.core
        self.drop_current()
        if self in core.sessions:
            core.sessions.discard(self)
            live = core.devices.live.get(self.dev_id)
            if live:
                live["conns"] = max(0, live["conns"] - 1)
                if live["conns"] == 0:
                    core.log(f"{self.name} disconnected")
            core.emit_devices()

    def drop_current(self) -> None:
        c, self.cur = self.cur, None
        if c and c.get("f"):
            try:
                c["f"].close()
                c["part"].unlink(missing_ok=True)
            except OSError:
                pass

    # ---- messages -------------------------------------------------------------------------
    async def on_text(self, m: dict) -> None:
        t = m.get("t")
        if t == "ping":
            await self.send({"t": "pong", "ts": m.get("ts")})
        elif t == "status":
            live = self.core.devices.live.setdefault(self.dev_id, {"conns": 1})
            for k in ("queue", "queueBytes", "battery", "charging", "rssi", "mbps", "latency"):
                if k in m:
                    live[k] = m[k]
            self.core.devices.touch(self.dev_id, self.name, self.ip)
            self.core.emit_devices()
        elif t == "photo":
            await self.start_photo(m)
        elif t == "speed":
            self.drop_current()
            self.cur = {"mode": "speed", "size": int(m.get("size", 0)), "got": 0, "t0": time.time()}
            if self.cur["size"] <= 0:
                self.cur = None

    async def start_photo(self, m: dict) -> None:
        core = self.core
        self.drop_current()
        pid = C.ID_RE.sub("", str(m.get("id", "")))[:64]
        try:
            size = int(m.get("size", 0))
            taken = int(m.get("taken", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        if not pid or size <= 0:
            await self.send({"t": "nack", "id": pid, "code": "bad_header"})
            return
        if core.paused:
            await self.send({"t": "nack", "id": pid, "code": "paused"})
            return
        if size > C.MAX_PHOTO:
            await self.send({"t": "nack", "id": pid, "code": "too_large"})
            return
        dup = core.is_seen(pid)
        if dup:
            self.cur = {"mode": "dup", "id": pid, "size": size, "got": 0, "rel": dup}
            return
        part = core.part_path(pid)
        try:
            f = open(part, "wb")
        except OSError as exc:
            core.log(f"cannot write {part}: {exc}", "warn")
            await self.send({"t": "nack", "id": pid, "code": "disk"})
            return
        self.cur = {"mode": "photo", "id": pid, "size": size, "got": 0, "taken": taken, "f": f, "part": part,
                    "first": True, "t0": time.time()}

    async def on_binary(self, data: bytes) -> None:
        core = self.core
        core.rx_bytes += len(data)
        c = self.cur
        if not c:
            return
        c["got"] += len(data)
        mode = c["mode"]
        if mode == "photo":
            if c["got"] > c["size"]:
                self.drop_current()
                await self.send({"t": "nack", "id": c["id"], "code": "overflow"})
                return
            if c["first"]:
                c["first"] = False
                if data[:3] != b"\xff\xd8\xff":
                    self.drop_current()
                    await self.send({"t": "nack", "id": c["id"], "code": "bad_jpeg"})
                    return
            try:
                c["f"].write(data)
            except OSError as exc:
                self.drop_current()
                core.log(f"disk error: {exc}", "warn")
                await self.send({"t": "nack", "id": c["id"], "code": "disk"})
                return
            if c["got"] == c["size"]:
                c["f"].close()
                self.cur = None
                try:
                    rel = core.finalize(c["part"], c["id"], c["taken"], self.dev_id, self.name, c["size"])
                except OSError as exc:
                    core.log(f"cannot store photo: {exc}", "warn")
                    await self.send({"t": "nack", "id": c["id"], "code": "disk"})
                    return
                await self.send({"t": "ack", "id": c["id"], "name": rel, "dup": False})
        elif mode == "dup":
            if c["got"] >= c["size"]:
                self.cur = None
                await self.send({"t": "ack", "id": c["id"], "name": c["rel"], "dup": True})
        elif mode == "speed":
            if c["got"] >= c["size"]:
                self.cur = None
                ms = max(1, int((time.time() - c["t0"]) * 1000))
                await self.send({"t": "speed_result", "bytes": c["got"], "ms": ms})


async def ws_phone(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024, compress=False)
    await ws.prepare(request)
    await PhoneSession(request.app["core"], ws, request.remote or "?").run()
    return ws


# --------------------------------------------------------------------------------------------
# local web UI
@web.middleware
async def guard(request: web.Request, handler):
    if request.path == "/ws":
        return await handler(request)
    host = request.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
    if request.remote not in LOCAL or host not in LOCAL:
        return web.Response(status=403, text="local only")
    origin = request.headers.get("Origin")
    if origin and origin.split("//", 1)[-1].rsplit(":", 1)[0].strip("[]") not in LOCAL:
        return web.Response(status=403, text="bad origin")
    if request.method not in ("GET", "HEAD") and request.headers.get("X-BB") != "1":
        return web.Response(status=403, text="missing header")
    return await handler(request)


def js(obj, status: int = 200) -> web.Response:
    return web.json_response(obj, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def index(request):
    return web.FileResponse(C.resource("ui/index.html"), headers={"Cache-Control": "no-store"})


async def api_state(request):
    return js(request.app["core"].state())


async def api_photos(request):
    core = request.app["core"]
    q = request.query
    offset, limit = int(q.get("offset", 0)), min(int(q.get("limit", 120)), 400)
    items = await asyncio.get_running_loop().run_in_executor(None, core.list_photos)
    return js({"total": len(items), "items": items[offset:offset + limit]})


async def thumb(request):
    core = request.app["core"]
    p = core.safe_path(request.match_info["rel"])
    if not p or not p.exists():
        return web.Response(status=404)
    dst = core.thumb_file(p)
    if not dst.exists():
        try:
            await asyncio.get_running_loop().run_in_executor(None, core.make_thumb, p, dst)
        except Exception:
            return web.FileResponse(p)
    return web.FileResponse(dst, headers={"Cache-Control": "max-age=86400"})


async def photo(request):
    p = request.app["core"].safe_path(request.match_info["rel"])
    if not p or not p.exists():
        return web.Response(status=404)
    return web.FileResponse(p, headers={"Cache-Control": "max-age=3600"})


async def api_delete(request):
    core = request.app["core"]
    names = (await request.json()).get("names", [])
    n = await asyncio.get_running_loop().run_in_executor(None, core.trash, [str(x) for x in names])
    return js({"deleted": n})


async def api_open_folder(request):
    core = request.app["core"]
    body = await request.json() if request.can_read_body else {}
    target = core.save_dir()
    if body.get("trash"):
        target = C.TRASH
    target.mkdir(parents=True, exist_ok=True)
    os.startfile(str(target))
    return js({"ok": True})


async def api_open_photo(request):
    p = request.app["core"].safe_path((await request.json()).get("name", ""))
    if not p or not p.exists():
        return js({"ok": False}, 404)
    subprocess.Popen(["explorer", "/select,", str(p)])
    return js({"ok": True})


async def api_settings(request):
    core = request.app["core"]
    body = await request.json()
    upd = {}
    if "save_dir" in body:
        d = Path(str(body["save_dir"]).strip())
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return js({"ok": False, "error": f"cannot use that folder: {exc}"}, 400)
        upd["save_dir"] = str(d)
    if body.get("subfolders") in ("flat", "day", "device"):
        upd["subfolders"] = body["subfolders"]
    if body.get("lang") in ("he", "en"):
        upd["lang"] = body["lang"]
    if "notify" in body:
        upd["notify"] = bool(body["notify"])
    if "pc_name" in body and str(body["pc_name"]).strip():
        upd["pc_name"] = str(body["pc_name"]).strip()[:40]
    if "port" in body:
        port = int(body["port"])
        if not 1024 <= port <= 65535:
            return js({"ok": False, "error": "port must be 1024-65535"}, 400)
        upd["port"] = port
    if "autostart" in body:
        hook = core.hooks.get("autostart")
        upd["autostart"] = bool(hook(bool(body["autostart"]))) if hook else bool(body["autostart"])
    core.cfg.update(**upd)
    core.log("settings saved: " + ", ".join(upd))
    return js({"ok": True, "state": core.state()})


async def api_new_code(request):
    core = request.app["core"]
    core.new_code()
    await core.close_all("code_changed")
    core.emit_devices()
    return js({"ok": True, "state": core.state()})


async def api_device(request):
    core = request.app["core"]
    b = await request.json()
    dev_id, action = str(b.get("id", "")), b.get("action")
    d = core.devices.known.get(dev_id)
    if d is None:
        return js({"ok": False}, 404)
    if action == "rename":
        d["name"] = str(b.get("name", "")).strip()[:40] or d["name"]
    elif action == "block":
        d["blocked"] = True
        await core.close_all("blocked", dev_id)
    elif action == "unblock":
        d["blocked"] = False
    elif action == "forget":
        await core.close_all("blocked", dev_id)
        core.devices.known.pop(dev_id, None)
        core.devices.live.pop(dev_id, None)
    elif action == "disconnect":
        await core.close_all("reconnect", dev_id)
    core.devices.save()
    core.emit_devices()
    return js({"ok": True})


async def api_pause(request):
    core = request.app["core"]
    core.paused = bool((await request.json()).get("paused"))
    core.log("receiving paused" if core.paused else "receiving resumed")
    core.emit({"t": "state", "paused": core.paused})
    return js({"ok": True, "paused": core.paused})


async def api_pick_folder(request):
    core = request.app["core"]

    def pick():
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        d = filedialog.askdirectory(initialdir=str(core.save_dir()), title="BioBuzz Photo Link")
        root.destroy()
        return d

    d = await asyncio.get_running_loop().run_in_executor(None, pick)
    return js({"path": str(Path(d)) if d else ""})


async def api_log(request):
    return js({"lines": list(request.app["core"].log_lines)[-600:]})


async def api_qr(request):
    import qrcode
    img = qrcode.make(request.app["core"].pair_link(), box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return web.Response(body=buf.getvalue(), content_type="image/png", headers={"Cache-Control": "no-store"})


async def api_show(request):
    hook = request.app["core"].hooks.get("show")
    if hook:
        hook()
    return js({"ok": True})


async def ws_events(request):
    core = request.app["core"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    q = asyncio.Queue(maxsize=600)
    core.subscribers.add(q)
    try:
        await ws.send_str(json.dumps({"t": "state", "state": core.state()}, ensure_ascii=False))
        recv = asyncio.ensure_future(ws.receive())
        while not ws.closed:
            get = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait({get, recv}, return_when=asyncio.FIRST_COMPLETED)
            if recv in done:
                get.cancel()
                break
            await ws.send_str(json.dumps(get.result(), ensure_ascii=False))
        recv.cancel()
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        core.subscribers.discard(q)
    return ws


# --------------------------------------------------------------------------------------------
def discovery_thread(core: C.Core, stop: threading.Event) -> None:
    """Answer phones that look for the PC (their saved IP may be out of date)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", C.DISCOVERY_PORT))
    except OSError as exc:
        core.log(f"discovery disabled (UDP {C.DISCOVERY_PORT}): {exc}", "warn")
        return
    s.settimeout(1.0)
    while not stop.is_set():
        try:
            data, addr = s.recvfrom(256)
        except socket.timeout:
            continue
        except OSError:
            break
        if data.startswith(b"BBLINK?"):
            reply = {"app": "biobuzz", "pc": core.cfg["pc_id"], "name": core.cfg["pc_name"], "port": core.port,
                     "ips": C.local_ips()}
            try:
                s.sendto(json.dumps(reply).encode(), addr)
            except OSError:
                pass
    s.close()


async def ticker(core: C.Core) -> None:
    while True:
        await asyncio.sleep(1)
        core.sample_speed()


def make_app(core: C.Core) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=1024 * 1024)
    app["core"] = core
    r = app.router
    r.add_get("/ws", ws_phone)
    r.add_get("/events", ws_events)
    r.add_get("/", index)
    r.add_get("/api/state", api_state)
    r.add_get("/api/photos", api_photos)
    r.add_get("/api/log", api_log)
    r.add_get("/api/qr.png", api_qr)
    r.add_get("/thumb/{rel:.*}", thumb)
    r.add_get("/photo/{rel:.*}", photo)
    for path, h in (("/api/delete", api_delete), ("/api/open-folder", api_open_folder), ("/api/open-photo", api_open_photo),
                    ("/api/settings", api_settings), ("/api/new-code", api_new_code), ("/api/device", api_device),
                    ("/api/pause", api_pause), ("/api/pick-folder", api_pick_folder), ("/api/show", api_show)):
        r.add_post(path, h)
    r.add_static("/ui/", C.resource("ui"), show_index=False)
    return app


async def start(core: C.Core):
    """Start the HTTP/WebSocket server (trying a few ports) and the background jobs."""
    core.hooks = getattr(core, "hooks", {})
    app = make_app(core)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    last = None
    for port in range(core.cfg["port"], core.cfg["port"] + 20):
        try:
            site = web.TCPSite(runner, "0.0.0.0", port, reuse_address=True)
            await site.start()
            core.port = port
            break
        except OSError as exc:
            last = exc
    else:
        raise last
    stop = threading.Event()
    threading.Thread(target=discovery_thread, args=(core, stop), daemon=True).start()
    task = asyncio.ensure_future(ticker(core))
    core.log(f"{C.APP} {C.VERSION} listening on port {core.port}, saving to {core.cfg['save_dir']}")

    async def shutdown():
        stop.set()
        task.cancel()
        await core.close_all("reconnect")
        core.flush()
        await runner.cleanup()

    return shutdown
