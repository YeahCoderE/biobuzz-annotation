# BioBuzz Photo Link 2.0

Take photos with the phone; each one lands in `dataset\new_photos` on the PC within a moment.

| File | What it is |
|------|-----------|
| `dist\v2\BioBuzzPhotoLink.exe` | Windows receiver: tray icon + local web window (status, photos, devices, settings, log) |
| `dist\v2\BioBuzzPhotoLink.apk` | Android app: camera, photos, status and settings pages, background service |

(`dist\BioBuzzPhotoLink.*` without `v2` are the old 1.0 files. Close the old exe, then copy the v2 files over them
if you want the desktop shortcut to start the new version.)

## Use

1. PC: run the exe. Windows Firewall asks for access: allow **private networks** (TCP 8777 and UDP 8778).
   The window opens; the app keeps running in the tray when you close it. *Settings -> Start with Windows* is on the Settings page.
2. Phone: install the APK, allow the camera and notifications. On first start pair **once**: scan the QR from the PC
   (*Devices* page), or pick the PC from the list the phone finds by itself and type the code, or type address + code.
3. Take photos (round button; hold it for continuous shooting; volume buttons also work). They are queued on the phone and
   sent over 2 parallel WebSocket connections. After a pairing, the phone finds the PC again by itself if its address changes.

Photos arrive as `YYYYMMDD_HHMMSS.jpg` (time taken) in the folder chosen in *Settings* (default: the project's `dataset\new_photos`).

## What changed from 1.0 and why

* **Speed**: photos are 12 MP at JPEG 92 % by default (3-5 MB instead of 10-25 MB), two WebSocket connections send in
  parallel in 256 KB frames, no polling. Measured against the real receiver: 100 x 4 MB photos in about 15 s over loopback
  (about 220 Mbps; a real Wi-Fi link is the limit). *Status -> Speed test* on the phone measures your actual Wi-Fi.
* **No more "disconnected" when walking away**: a WebSocket ping every 15 s instead of one 2.5 s probe; the connection is
  re-established silently and the queue is kept; the PC is looked up again by UDP broadcast if its address changed.
* **Background**: a foreground service (with a notification) keeps the connection and uploads alive while the screen is off.
  Android blocks the camera in the background, so photos can only be taken while the app is open.
  For best results allow *unrestricted battery use* (button on the Status page).
* **Pairing is one time**: the QR is not needed again, and it is not the only way (list of PCs + code, or manual).

## Protocol

See `PLAN.md`. WebSocket `ws://PC:8777/ws` (JSON + binary frames), UDP discovery on 8778. A photo is deleted from the phone
only after the PC acknowledged it; photo ids make retries idempotent. The web window is reachable only from the PC itself.
Security: plain HTTP on the local network protected by the secret code; the PC accepts only JPEGs (max 120 MB) and chooses file
names itself; the phone refuses non-private addresses. Do not use it on an untrusted public Wi-Fi.

## Tests

* `tests\test_protocol.py` - end-to-end protocol and stress test of the PC receiver (run it with `--headless` first).
* `tests\jvm\LinkClientIT.java` - runs the phone's real connection code (`android\...\core`) against the receiver on the PC:
  speed, reconnect after the server is killed mid-transfer, no loss and no duplicates.
* The phone UI (camera, pages, service) was compiled but **not yet run on a real phone**.

## Build

`powershell -ExecutionPolicy Bypass -File photo_link\build.ps1` (exe with PyInstaller, APK with Gradle + the Android SDK;
needs about 1.2 GB of free RAM for Gradle, so close Android Studio first).
Sources: `pc\` (`core.py`, `server.py`, `main.py`, `ui\index.html`), `android\app\src\main\java\com\biobuzz\photolink\`.
