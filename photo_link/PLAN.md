# BioBuzz Photo Link 2.0 - plan

Goals (from the user): much more UI (status / photos / settings pages on both sides), photos arrive at
Wi-Fi speed (100 queued photos), no disconnects when walking away from the PC on the same Wi-Fi,
WebSocket server, works in the background, QR pairing exactly once.

## Why 1.0 was slow and dropped out (diagnosis from the code)
* Photos were taken at maximum quality and full sensor size (10-25 MB each) -> several seconds on Wi-Fi.
  Fix: default 12 MP at JPEG quality 92 (about 3-5 MB; the detector uses 448 px), selectable in Settings.
* One request at a time, a new HTTP connection per photo, polling loop sleeping 200 ms to 4 s.
  Fix: persistent WebSocket connections (2 in parallel), 256 KB binary chunks, no polling.
* Online/offline was decided by one 2.5 s HTTP probe every 4 s; a single lost probe at the edge of the
  Wi-Fi range flipped it to "offline". Fix: WebSocket ping every 15 s, silent reconnect with back-off,
  queue never lost.
* The PC's IP address was stored once; after a DHCP change it was unreachable. Fix: UDP discovery
  (phone broadcasts, PC answers) - no re-scan needed.
* The phone's Wi-Fi radio sleeps in the background. Fix: foreground service, Wi-Fi low-latency lock while
  uploading, partial wake lock, battery-optimisation exemption button.

## Protocol (v2)
WebSocket `ws://PC:8777/ws`, JSON text frames + binary frames. UDP discovery on port 8778.

    phone -> {"t":"hello","v":2,"token":T,"device":ID,"name":"Pixel","role":"main|worker","app":"2.0"}
    PC    -> {"t":"hello_ok","v":2,"pc":PCID,"name":"DESKTOP","chunk":262144}   | {"t":"error","code":"bad_token|blocked"}
    phone -> {"t":"photo","id":ID,"taken":ms,"size":N,"w":W,"h":H}  then binary chunks totalling N bytes
    PC    -> {"t":"ack","id":ID,"name":"20261121_101500.jpg","dup":false} | {"t":"nack","id":ID,"code":"bad_jpeg|too_large|disk"}
    phone -> {"t":"status","queue":n,"queueBytes":b,"battery":p,"charging":bool,"rssi":dBm,"mbps":x}   (main, every 5 s)
    phone -> {"t":"ping","ts":ms}  PC -> {"t":"pong","ts":ms}                                           (main, every 3 s)
    phone -> {"t":"speed","size":N} + binary chunks; PC -> {"t":"speed_result","bytes":N,"ms":T}        (speed test)
    PC    -> {"t":"bye","code":"blocked|code_changed"}
    UDP: phone "BBLINK?1" (broadcast)  ->  PC {"app":"biobuzz","pc":PCID,"name":..,"port":..,"ips":[..]}

A photo stays in the phone's outbox until the ack arrives. Photo ids make retries idempotent.

## PC app (Python: aiohttp server + tray + browser-based window)
* `core.py` state/storage, `server.py` WebSocket + UI API + UDP discovery, `main.py` tray, autostart, window.
* The window is a local web UI (Edge `--app` window, only reachable from this PC): Status, Photos (grid, viewer,
  delete, live), Devices (+ QR to add a phone), Settings, Log. Hebrew / English.
* Tray icon, start with Windows, single instance, photos saved to `dataset\new_photos` by default.

## Phone app (Java)
* `SyncService` (foreground service, notification) owns the connections and the queue; the camera needs the
  app on screen (Android blocks the camera in the background).
* Pages: Camera, Photos (queue + sent history with thumbnails), Status (connection, speed, test, permissions),
  Settings (quality, connections, background, camera options). First-run pairing screen (QR, discovered PCs, code).
* Hebrew + English strings.

## Verification (I cannot run the phone here)
* PC: stress test with 100 x 4 MB photos over 2 connections, reconnect test, duplicate test, bad token test.
* Phone protocol code is plain Java (`core` package) and is tested on the JVM against the real PC server.
* UI of the PC app checked in a browser; phone UI only compiled + reviewed (needs the user's phone for the rest).
