#!/usr/bin/env python
"""End-to-end test of the PC receiver protocol (run the receiver with --headless first).

    pc\\.venv\\Scripts\\python tests\\test_protocol.py --port 8800 --token ABCDEFGH1234 --folder <save folder> [--photos 100]
"""
import argparse
import asyncio
import hashlib
import json
import os
import socket
import sys
import time
import uuid

import aiohttp

CHUNK = 256 * 1024


def fake_jpeg(size: int, seed: int) -> bytes:
    body = hashlib.sha256(str(seed).encode()).digest() * (size // 32 + 1)
    return b"\xff\xd8\xff\xe0" + body[: size - 6] + b"\xff\xd9"


class Phone:
    def __init__(self, port, token, device, role="main", name="Test Phone"):
        self.url = f"http://127.0.0.1:{port}/ws"
        self.token, self.device, self.role, self.name = token, device, role, name

    async def open(self, token=None):
        self.sess = aiohttp.ClientSession()
        self.ws = await self.sess.ws_connect(self.url, heartbeat=15, compress=0, max_msg_size=0)
        await self.ws.send_json({"t": "hello", "v": 2, "token": token or self.token, "device": self.device,
                                 "name": self.name, "role": self.role, "app": "test"})
        return await self.ws.receive_json()

    async def close(self):
        await self.ws.close()
        await self.sess.close()

    async def upload(self, pid, data, taken=None, abort_after=None):
        await self.ws.send_json({"t": "photo", "id": pid, "taken": taken or int(time.time() * 1000), "size": len(data)})
        sent = 0
        while sent < len(data):
            n = min(CHUNK, len(data) - sent)
            await self.ws.send_bytes(data[sent:sent + n])
            sent += n
            if abort_after is not None and sent >= abort_after:
                return None
        return await self.ws.receive_json()


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not ok:
        check.failed += 1


check.failed = 0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--photos", type=int, default=100)
    ap.add_argument("--mb", type=float, default=4.0)
    a = ap.parse_args()
    dev = uuid.uuid4().hex[:16]

    # 1. wrong token
    p = Phone(a.port, a.token, dev)
    r = await p.open(token="WRONGWRONG12")
    check("wrong token rejected", r.get("t") == "error" and r.get("code") == "bad_token", str(r))
    await p.close()

    # 2. hello, ping
    p = Phone(a.port, a.token, dev)
    r = await p.open(token=a.token.lower())  # case/hyphen insensitive
    check("hello_ok (token normalised)", r.get("t") == "hello_ok" and r.get("v") == 2, str(r))
    await p.ws.send_json({"t": "ping", "ts": 123})
    r = await p.ws.receive_json()
    check("ping -> pong", r == {"t": "pong", "ts": 123}, str(r))

    # 3. one photo, duplicate, bad jpeg
    data = fake_jpeg(300_000, 1)
    pid = "t-" + uuid.uuid4().hex[:10]
    r = await p.upload(pid, data, taken=1795283000000)
    path = os.path.join(a.folder, r.get("name", "?"))
    check("photo acked and stored", r.get("t") == "ack" and not r["dup"] and os.path.exists(path) and open(path, "rb").read() == data, str(r))
    r2 = await p.upload(pid, data)
    check("duplicate id acked as dup, no second file", r2.get("t") == "ack" and r2["dup"] and r2["name"] == r["name"], str(r2))
    r = await p.upload("t-bad" + uuid.uuid4().hex[:6], b"this is not a jpeg" * 100)
    check("non-JPEG refused", r.get("t") == "nack" and r.get("code") == "bad_jpeg", str(r))

    # 4. status shows up in the UI state
    await p.ws.send_json({"t": "status", "queue": 7, "queueBytes": 7000000, "battery": 81, "charging": True, "rssi": -52, "mbps": 120, "latency": 8})
    await asyncio.sleep(0.4)
    async with aiohttp.ClientSession() as s:
        st = await (await s.get(f"http://127.0.0.1:{a.port}/api/state")).json()
    d = [x for x in st["devices"] if x["id"] == dev]
    check("device online with status in UI state", bool(d) and d[0]["online"] and d[0]["battery"] == 81 and d[0]["queue"] == 7, str(d[:1]))

    # 5. speed test
    size = 8 * 1024 * 1024
    await p.ws.send_json({"t": "speed", "size": size})
    t0 = time.time()
    for _ in range(size // CHUNK):
        await p.ws.send_bytes(os.urandom(CHUNK))
    r = await p.ws.receive_json()
    check("speed test result", r.get("t") == "speed_result" and r["bytes"] == size, f"{size * 8 / 1e6 / (time.time() - t0):.0f} Mbps over loopback")

    # 6. connection dropped in the middle of a photo: no partial file, same id works afterwards
    pid2 = "t-cut" + uuid.uuid4().hex[:6]
    await p.upload(pid2, fake_jpeg(2_000_000, 2), abort_after=600_000)
    await p.close()
    await asyncio.sleep(0.5)
    left = [f for f in os.listdir(a.folder) if f.endswith(".part")]
    check("no .part file left after a cut connection", not left, str(left))
    p = Phone(a.port, a.token, dev)
    await p.open()
    data2 = fake_jpeg(2_000_000, 2)
    r = await p.upload(pid2, data2)
    check("same photo id uploads fine after reconnect", r.get("t") == "ack" and not r["dup"], str(r))
    await p.close()

    # 7. stress: N photos over 2 connections in parallel
    n = a.photos
    size = int(a.mb * 1024 * 1024)
    photos = [(f"s-{i}-{uuid.uuid4().hex[:6]}", fake_jpeg(size, 1000 + i)) for i in range(n)]
    queue = asyncio.Queue()
    for item in photos:
        queue.put_nowait(item)
    results = {}

    async def worker(role):
        w = Phone(a.port, a.token, dev, role=role)
        await w.open()
        while True:
            try:
                pid, data = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            r = await w.upload(pid, data, taken=1795283100000 + int(pid.split("-")[1]) * 1000)
            results[pid] = r
        await w.close()

    t0 = time.time()
    await asyncio.gather(worker("main"), worker("worker"))
    dt = time.time() - t0
    total = n * size
    ok = all(r.get("t") == "ack" for r in results.values()) and len(results) == n
    check(f"stress: {n} photos x {a.mb} MB acked", ok, f"{dt:.1f} s, {total / 1e6 / dt:.0f} MB/s ({total * 8 / 1e6 / dt:.0f} Mbps), {dt / n * 1000:.0f} ms per photo")
    bad = 0
    for pid, data in photos:
        rel = results[pid]["name"]
        if hashlib.md5(open(os.path.join(a.folder, rel), "rb").read()).digest() != hashlib.md5(data).digest():
            bad += 1
    check("stress: every stored file is byte-identical", bad == 0, f"{bad} differ")

    # 8. UDP discovery
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    s.sendto(b"BBLINK?1", ("127.0.0.1", 8778))
    try:
        reply = json.loads(s.recvfrom(1024)[0])
        check("UDP discovery answered", reply.get("app") == "biobuzz" and reply.get("port") == a.port, str(reply))
    except socket.timeout:
        check("UDP discovery answered", False, "timeout")

    print("\nFAILED: %d" % check.failed if check.failed else "\nALL PASSED")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
