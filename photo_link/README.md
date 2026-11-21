# BioBuzz Photo Link

Take photos with the phone; each one lands in `dataset\new_photos` on the PC a moment later.

| File | What it is |
|------|-----------|
| `dist\BioBuzzPhotoLink.exe` | Windows receiver (window with a QR code and the list of received photos) |
| `dist\BioBuzzPhotoLink.apk` | Android camera app (Android 7.0+; debug-signed, so allow "install unknown apps") |

## Use

1. PC: run `BioBuzzPhotoLink.exe`. On the first run Windows Firewall asks for access: allow **private networks**.
2. Phone: install the APK, open it, allow the camera.
3. The phone and the PC must be on the same Wi-Fi. In the app tap **Scan QR** and scan the code in the PC window (only once; the pairing is remembered).
4. Take photos with the round button or a volume key. Tap the picture to focus, pinch to zoom.
   The top bar shows `Connected · sent N · queued M`.

Photos go to `dataset\new_photos` (found automatically next to the exe; **Change folder** overrides it) as
`YYYYMMDD_HHMMSS.jpg`, named by the time they were taken. If the PC is not reachable the photos wait
on the phone (shown as "queued") and are sent when it is back. Retries never create duplicates.

Then tell Claude "new photos" to label them and train v6 (see `tools/` and the main README).

## How it works

```
phone  --HTTP POST /upload?t=CODE (JPEG, X-Photo-Id, X-Taken-At)-->  PC :8777  -->  dataset\new_photos
```

* The QR holds `biobuzz://pair?h=<PC addresses>&p=<port>&t=<secret code>`. The code is created on the
  first run and stored in `%APPDATA%\BioBuzzPhotoLink\config.json`; **New code** in the PC window revokes every phone.
* Plain HTTP on the local network, protected by that code. The PC accepts only JPEG files, up to 40 MB,
  and picks the file name itself. The phone refuses pairing links that are not private addresses
  (192.168.x.x, 10.x.x.x, 172.16-31.x.x).
* Do not use it on public Wi-Fi you do not trust: anyone on the same network who sees the QR can send photos.
* The phone sends over the Wi-Fi network even if it has no internet (Android would otherwise use mobile data).

## Build

`powershell -ExecutionPolicy Bypass -File photo_link\build.ps1` (exe with PyInstaller, apk with Gradle + the Android SDK).
Sources: `pc\biobuzz_receiver.py`, `android\app\src\main\java\com\biobuzz\photolink\`.

Not yet tested on a real phone (the APK was built and its upload code path tested against the exe from Java).
