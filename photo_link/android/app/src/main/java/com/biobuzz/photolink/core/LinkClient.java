package com.biobuzz.photolink.core;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.Proxy;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

import javax.net.SocketFactory;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

/**
 * Connection to the PC receiver (protocol v2, see photo_link/PLAN.md).
 *
 * One "main" WebSocket carries the heartbeat and status; extra "worker" sockets upload photos in
 * parallel. Every socket uploads photos from the {@link Outbox}; a photo leaves the outbox only
 * after the PC acknowledged it. When a socket breaks the photo it was sending goes back to the
 * queue; the manager thread reconnects with back-off and, when the saved addresses stop working,
 * looks for the PC with a UDP broadcast.
 *
 * Plain Java + OkHttp (no Android classes) so it can be tested on a PC against the real receiver.
 */
public final class LinkClient {
    public interface Listener {
        void onChange();                                           // anything the UI shows changed
        void onProgress(String id, long sent, long total);         // a photo is on its way
        void onSent(File f, String id, String serverName, boolean dup, long bytes);  // called before the file is deleted
        void onRejected(File f, String id, String code);
        void onFatal(String code);                                 // bad_token, blocked: stop and ask the user
        void onPairingUpdated(Pairing p);                          // the address of the PC changed
        void onLog(String line);
    }

    /** Things only the phone knows. */
    public interface Env {
        SocketFactory socketFactory();                             // bound to the Wi-Fi network, or null
        void bindDatagram(DatagramSocket s) throws IOException;
        String localIp();                                          // phone's Wi-Fi IPv4 or null
        int battery();                                             // percent or -1
        boolean charging();
        int rssi();                                                // dBm or 0
        String deviceId();
        String deviceName();
    }

    public enum State { IDLE, CONNECTING, ONLINE, FATAL }

    public static final class Found {
        public String pcId, name, fromIp;
        public int port;
        public List<String> ips = new ArrayList<>();
    }

    private static final int CHUNK = 256 * 1024;
    private static final long MAX_QUEUE = 1536 * 1024;
    private static final String APP_VERSION = "2.0";

    private final Outbox outbox;
    private final Listener listener;
    private final Env env;
    private final OkHttpClient base;
    private final AtomicLong tx = new AtomicLong();            // bytes handed to the network so far
    private final ArrayDeque<String> logs = new ArrayDeque<>();
    private final Object managerLock = new Object();

    private volatile boolean running;
    private volatile Pairing pairing;
    private volatile State state = State.IDLE;
    private volatile String fatalCode = "";
    private volatile String pcName = "";
    private volatile String host = "";
    private volatile long latencyMs = -1;
    private volatile double mbpsNow;
    private volatile int sentCount;
    private volatile long sentBytes;
    private volatile int sockets;
    private volatile int wantedSockets = 2;
    private volatile Session primary;
    private volatile boolean kick;
    private Thread manager;
    private Thread ticker;

    public LinkClient(Outbox outbox, Env env, Listener listener) {
        this.outbox = outbox;
        this.env = env;
        this.listener = listener;
        this.base = new OkHttpClient.Builder()
                .pingInterval(15, TimeUnit.SECONDS)
                .connectTimeout(3500, TimeUnit.MILLISECONDS)
                .readTimeout(0, TimeUnit.MILLISECONDS)
                .writeTimeout(30, TimeUnit.SECONDS)
                .retryOnConnectionFailure(false)
                .proxy(Proxy.NO_PROXY)
                .build();
    }

    // ---- public API -----------------------------------------------------------------------
    public State state() { return state; }
    public String fatalCode() { return fatalCode; }
    public String pcName() { return pcName; }
    public String host() { return host; }
    public long latencyMs() { return latencyMs; }
    public double mbpsNow() { return mbpsNow; }
    public int sentCount() { return sentCount; }
    public long sentBytes() { return sentBytes; }
    public int sockets() { return sockets; }
    public Pairing pairing() { return pairing; }
    public Outbox outbox() { return outbox; }

    public synchronized List<String> logLines() {
        return new ArrayList<>(logs);
    }

    public void setConnections(int n) {
        wantedSockets = Math.max(1, Math.min(4, n));
    }

    public synchronized void start(Pairing p) {
        pairing = p;
        if (running) return;
        running = true;
        manager = new Thread(this::managerLoop, "link-manager");
        manager.setDaemon(true);
        manager.start();
        ticker = new Thread(this::tickerLoop, "link-ticker");
        ticker.setDaemon(true);
        ticker.start();
    }

    public synchronized void stop() {
        running = false;
        Session s = primary;
        if (s != null) s.kill();
        outbox.wake();
        wakeManager();
    }

    /** New pairing (or the same one again): clears a fatal state and reconnects right away. */
    public void setPairing(Pairing p) {
        pairing = p;
        fatalCode = "";
        if (state == State.FATAL) setState(State.IDLE);
        reconnectNow();
    }

    public void reconnectNow() {
        kick = true;
        Session s = primary;
        if (s != null) s.kill();
        wakeManager();
    }

    public void reconnectNowIfOffline() {
        if (running && state != State.ONLINE && state != State.FATAL) reconnectNow();
    }

    public void wakeUploads() { outbox.wake(); }

    // ---- state ------------------------------------------------------------------------------
    private void setState(State s) {
        if (state != s) {
            state = s;
            listener.onChange();
        }
    }

    private void log(String msg) {
        String line = new java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.US).format(new java.util.Date()) + " " + msg;
        synchronized (this) {
            logs.addLast(line);
            while (logs.size() > 200) logs.removeFirst();
        }
        listener.onLog(line);
    }

    private void wakeManager() {
        synchronized (managerLock) {
            managerLock.notifyAll();
        }
    }

    private void sleepManager(long ms) {
        synchronized (managerLock) {
            if (kick) {
                kick = false;
                return;
            }
            try {
                managerLock.wait(ms);
            } catch (InterruptedException ignored) {
            }
        }
    }

    // ---- manager: connect, reconnect, discover ---------------------------------------------------
    private void managerLoop() {
        int fails = 0;
        while (running) {
            Pairing p = pairing;
            if (p == null || state == State.FATAL) {
                sleepManager(1000);
                continue;
            }
            setState(State.CONNECTING);
            Session s = connectPrimary(p);
            if (s == null) {
                if (state == State.FATAL) continue;
                fails++;
                setState(State.CONNECTING);
                if (fails == 2 || fails % 5 == 0) discoverAndUpdate(p);
                sleepManager(Math.min(3000, 400L * (1L << Math.min(fails, 3))));
                continue;
            }
            fails = 0;
            primary = s;
            host = s.host;
            pcName = s.pcName != null && !s.pcName.isEmpty() ? s.pcName : p.pcName;
            setState(State.ONLINE);
            log("connected to " + pcName + " (" + host + ")");
            List<Thread> workers = startWorkers(s);
            Thread up = new Thread(() -> s.uploadLoop(), "upload-main");
            up.setDaemon(true);
            up.start();
            s.awaitDead();
            primary = null;
            sockets = 0;
            for (Thread w : workers) w.interrupt();
            up.interrupt();
            if (s.fatal != null && !s.fatal.isEmpty()) {
                handleFatal(s.fatal);
            } else if (running) {
                log("connection lost, reconnecting");
                setState(State.CONNECTING);
                sleepManager(500);
            }
        }
    }

    private static boolean isFatalCode(String code) {
        return "bad_token".equals(code) || "blocked".equals(code) || "code_changed".equals(code);
    }

    private void handleFatal(String code) {
        if (!isFatalCode(code)) {
            if (!"reconnect".equals(code)) log("PC said: " + code + ", trying again soon");
            sleepManager("too_many_tries".equals(code) ? 20_000 : 1000);
            return;
        }
        fatalCode = code;
        setState(State.FATAL);
        log("stopped: " + code);
        listener.onFatal(code);
    }

    private List<String> orderedHosts(Pairing p) {
        return p.ordered(env.localIp());
    }

    private Session connectPrimary(Pairing p) {
        for (String h : orderedHosts(p)) {
            if (!running) break;
            Session s = new Session("main");
            if (s.connect(h, p.port)) {
                p.lastGood = h;
                listener.onPairingUpdated(p);
                return s;
            }
            if (s.fatal != null && !s.fatal.isEmpty() && !"reconnect".equals(s.fatal)) {
                handleFatal(s.fatal);
                return null;
            }
            s.kill();
        }
        return null;
    }

    private List<Thread> startWorkers(Session main) {
        List<Thread> out = new ArrayList<>();
        for (int i = 1; i < wantedSockets; i++) {
            Thread t = new Thread(() -> workerLoop(main), "upload-worker-" + i);
            t.setDaemon(true);
            t.start();
            out.add(t);
        }
        return out;
    }

    private void workerLoop(Session main) {
        Pairing p = pairing;
        while (running && main.alive && p != null) {
            Session s = new Session("worker");
            if (s.connect(main.host, p.port)) {
                s.uploadLoop();
            }
            s.kill();
            if (s.fatal != null && !s.fatal.isEmpty()) return;
            try {
                Thread.sleep(1500);
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    // ---- discovery -------------------------------------------------------------------------------
    public List<Found> discover(int timeoutMs) {
        List<Found> out = new ArrayList<>();
        DatagramSocket s = null;
        try {
            s = new DatagramSocket();
            env.bindDatagram(s);
            s.setBroadcast(true);
            s.setSoTimeout(250);
            byte[] msg = "BBLINK?1".getBytes("UTF-8");
            List<String> targets = new ArrayList<>();
            targets.add("255.255.255.255");
            String ip = env.localIp();
            if (ip != null && ip.lastIndexOf('.') > 0) targets.add(ip.substring(0, ip.lastIndexOf('.') + 1) + "255");
            long end = System.currentTimeMillis() + timeoutMs;
            long nextSend = 0;
            byte[] buf = new byte[2048];
            while (System.currentTimeMillis() < end) {
                if (System.currentTimeMillis() >= nextSend) {
                    for (String t : targets) {
                        try {
                            s.send(new DatagramPacket(msg, msg.length, InetAddress.getByName(t), 8778));
                        } catch (IOException ignored) {
                        }
                    }
                    nextSend = System.currentTimeMillis() + 700;
                }
                DatagramPacket pk = new DatagramPacket(buf, buf.length);
                try {
                    s.receive(pk);
                } catch (java.net.SocketTimeoutException e) {
                    continue;
                }
                try {
                    JSONObject j = new JSONObject(new String(pk.getData(), 0, pk.getLength(), "UTF-8"));
                    if (!"biobuzz".equals(j.optString("app"))) continue;
                    Found f = new Found();
                    f.pcId = j.optString("pc");
                    f.name = j.optString("name");
                    f.port = j.optInt("port", Pairing.DEFAULT_PORT);
                    f.fromIp = pk.getAddress().getHostAddress();
                    org.json.JSONArray arr = j.optJSONArray("ips");
                    if (arr != null) for (int i = 0; i < arr.length(); i++) f.ips.add(arr.optString(i));
                    boolean dup = false;
                    for (Found o : out) if (o.pcId.equals(f.pcId)) dup = true;
                    if (!dup) out.add(f);
                } catch (JSONException ignored) {
                }
            }
        } catch (IOException e) {
            log("discovery failed: " + e.getMessage());
        } finally {
            if (s != null) s.close();
        }
        return out;
    }

    private void discoverAndUpdate(Pairing p) {
        for (Found f : discover(1800)) {
            if (p.pcId.isEmpty() || p.pcId.equals(f.pcId)) {
                p.addHostFirst(f.fromIp);
                for (String ip : f.ips) if (!ip.equals(f.fromIp)) p.addHostFirst(ip);
                p.addHostFirst(f.fromIp);
                p.port = f.port;
                if (p.pcId.isEmpty()) p.pcId = f.pcId;
                p.pcName = f.name;
                log("found the PC at " + f.fromIp);
                listener.onPairingUpdated(p);
                return;
            }
        }
    }

    // ---- speed ticker + status ---------------------------------------------------------------------
    private void tickerLoop() {
        long last = tx.get();
        long lastTime = System.nanoTime();
        int n = 0;
        while (running) {
            try {
                Thread.sleep(1000);
            } catch (InterruptedException e) {
                return;
            }
            long now = System.nanoTime();
            long cur = tx.get();
            double v = (cur - last) * 8.0 / 1e6 / Math.max(0.2, (now - lastTime) / 1e9);
            mbpsNow = mbpsNow * 0.5 + v * 0.5;
            if (mbpsNow < 0.05) mbpsNow = 0;
            last = cur;
            lastTime = now;
            n++;
            Session s = primary;
            if (s != null && s.alive) {
                if (n % 3 == 0) s.send(obj("t", "ping", "ts", System.currentTimeMillis()));
                if (n % 5 == 0) s.sendStatus();
            }
            listener.onChange();
        }
    }

    // ---- speed test ------------------------------------------------------------------------------------
    /** Sends random data to the PC over a fresh socket. Returns Mbps, or -1 on failure. */
    public double speedTest(long bytes) {
        Pairing p = pairing;
        if (p == null) return -1;
        for (String h : orderedHosts(p)) {
            Session s = new Session("worker");
            try {
                if (!s.connect(h, p.port)) continue;
                BlockingQueue<JSONObject> q = new ArrayBlockingQueue<>(2);
                s.speedWait = q;
                s.send(obj("t", "speed", "size", bytes));
                byte[] buf = new byte[CHUNK];
                new java.util.Random().nextBytes(buf);
                long t0 = System.nanoTime();
                long sent = 0;
                while (sent < bytes && s.alive) {
                    int n = (int) Math.min(CHUNK, bytes - sent);
                    while (s.ws.queueSize() > MAX_QUEUE && s.alive) Thread.sleep(2);
                    if (!s.ws.send(ByteString.of(buf, 0, n))) return -1;
                    sent += n;
                }
                JSONObject r = q.poll(20, TimeUnit.SECONDS);
                double sec = (System.nanoTime() - t0) / 1e9;
                if (r == null || !"speed_result".equals(r.optString("t")) || sec <= 0) return -1;
                return bytes * 8.0 / 1e6 / sec;
            } catch (InterruptedException e) {
                return -1;
            } finally {
                s.kill();
            }
        }
        return -1;
    }

    // ---- helpers -----------------------------------------------------------------------------------------
    static JSONObject obj(Object... kv) {
        JSONObject o = new JSONObject();
        try {
            for (int i = 0; i + 1 < kv.length; i += 2) o.put((String) kv[i], kv[i + 1]);
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
        return o;
    }

    // ==================================================================================================
    private final class Session extends WebSocketListener {
        final String role;
        String host = "";
        volatile WebSocket ws;
        volatile boolean alive;
        volatile String fatal = "";
        volatile String pcName = "";
        volatile BlockingQueue<JSONObject> speedWait;
        final CountDownLatch opened = new CountDownLatch(1);
        final CountDownLatch dead = new CountDownLatch(1);
        final Map<String, BlockingQueue<JSONObject>> waits = new ConcurrentHashMap<>();
        boolean helloOk;

        Session(String role) {
            this.role = role;
        }

        boolean connect(String h, int port) {
            this.host = h;
            OkHttpClient.Builder b = base.newBuilder();
            SocketFactory sf = env.socketFactory();
            if (sf != null) b.socketFactory(sf);
            Request req = new Request.Builder().url("http://" + h + ":" + port + "/ws").build();
            ws = b.build().newWebSocket(req, this);
            try {
                if (!opened.await(5, TimeUnit.SECONDS) || !helloOk) {
                    ws.cancel();
                    return false;
                }
            } catch (InterruptedException e) {
                ws.cancel();
                return false;
            }
            if (dead.getCount() == 0) return false;    // it died right after the hello
            alive = true;
            sockets++;
            listener.onChange();
            return true;
        }

        boolean send(JSONObject o) {
            WebSocket w = ws;
            return w != null && alive && w.send(o.toString());
        }

        void sendStatus() {
            send(obj("t", "status", "queue", outbox.count(), "queueBytes", outbox.bytes(), "battery", env.battery(),
                    "charging", env.charging(), "rssi", env.rssi(), "mbps", Math.round(mbpsNow), "latency", latencyMs));
        }

        void kill() {
            WebSocket w = ws;
            if (w != null) w.cancel();
            markDead();
        }

        void markDead() {
            if (alive) {
                alive = false;
                sockets = Math.max(0, sockets - 1);
            }
            opened.countDown();
            dead.countDown();
            for (BlockingQueue<JSONObject> q : waits.values()) q.offer(obj("t", "dead"));
            BlockingQueue<JSONObject> sw = speedWait;
            if (sw != null) sw.offer(obj("t", "dead"));
            listener.onChange();
        }

        void awaitDead() {
            try {
                dead.await();
            } catch (InterruptedException ignored) {
            }
        }

        // ---- OkHttp callbacks ----------------------------------------------------------------
        @Override public void onOpen(WebSocket w, Response r) {
            Pairing p = pairing;
            w.send(obj("t", "hello", "v", 2, "token", p == null ? "" : p.token, "device", env.deviceId(),
                    "name", env.deviceName(), "role", role, "app", APP_VERSION).toString());
        }

        @Override public void onMessage(WebSocket w, String text) {
            JSONObject m;
            try {
                m = new JSONObject(text);
            } catch (JSONException e) {
                return;
            }
            String t = m.optString("t");
            switch (t) {
                case "hello_ok": {
                    Pairing p = pairing;
                    String id = m.optString("pc");
                    if (p != null && !p.pcId.isEmpty() && !id.isEmpty() && !p.pcId.equals(id)) {
                        log("another PC answered at " + host + ", ignoring it");
                        fatal = "";
                        opened.countDown();
                        w.cancel();
                        return;
                    }
                    if (p != null && p.pcId.isEmpty()) p.pcId = id;
                    pcName = m.optString("name");
                    if (p != null && !pcName.isEmpty()) p.pcName = pcName;
                    helloOk = true;
                    opened.countDown();
                    break;
                }
                case "error":
                    fatal = m.optString("code", "error");
                    opened.countDown();
                    w.cancel();
                    markDead();
                    break;
                case "bye":
                    fatal = m.optString("code", "reconnect");
                    w.cancel();
                    markDead();
                    break;
                case "pong": {
                    long ts = m.optLong("ts");
                    long rtt = System.currentTimeMillis() - ts;
                    if (rtt >= 0 && rtt < 60000) latencyMs = latencyMs < 0 ? rtt : (latencyMs * 2 + rtt) / 3;
                    break;
                }
                case "ack":
                case "nack": {
                    BlockingQueue<JSONObject> q = waits.get(m.optString("id"));
                    if (q != null) q.offer(m);
                    break;
                }
                case "speed_result": {
                    BlockingQueue<JSONObject> q = speedWait;
                    if (q != null) q.offer(m);
                    break;
                }
                default:
                    break;
            }
        }

        @Override public void onClosing(WebSocket w, int code, String reason) {
            w.close(1000, null);
            markDead();
        }

        @Override public void onClosed(WebSocket w, int code, String reason) {
            markDead();
        }

        @Override public void onFailure(WebSocket w, Throwable t, Response r) {
            markDead();
        }

        // ---- uploading --------------------------------------------------------------------------
        void uploadLoop() {
            try {
                while (running && alive) {
                    File f = outbox.claim(1500);
                    if (f == null) continue;
                    int r = sendFile(f);
                    if (r == 1) break;                       // socket broke: reconnect
                    if (r == 2) Thread.sleep(3000);          // PC is busy or paused
                }
            } catch (InterruptedException ignored) {
            }
        }

        /** 0 = sent, 1 = connection problem (file stays queued), 2 = try again later, 3 = refused for good */
        private int sendFile(File f) throws InterruptedException {
            String id = Outbox.idOf(f);
            long size = f.length();
            if (size <= 0) {
                outbox.reject(f);
                listener.onRejected(f, id, "empty");
                return 3;
            }
            BlockingQueue<JSONObject> q = new ArrayBlockingQueue<>(4);
            waits.put(id, q);
            long t0 = System.nanoTime();
            try {
                if (!send(obj("t", "photo", "id", id, "taken", Outbox.takenOf(f), "size", size))) {
                    outbox.release(f);
                    return 1;
                }
                long queued = 0, accounted = 0, lastReport = 0;
                byte[] buf = new byte[CHUNK];
                try (FileInputStream in = new FileInputStream(f)) {
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        if (!alive) {
                            outbox.release(f);
                            return 1;
                        }
                        if (!q.isEmpty()) break;             // the PC already answered (refused it)
                        while (ws.queueSize() > MAX_QUEUE && alive && q.isEmpty()) Thread.sleep(2);
                        if (!ws.send(ByteString.of(buf, 0, n))) {
                            outbox.release(f);
                            return 1;
                        }
                        queued += n;
                        long transmitted = Math.max(0, queued - ws.queueSize());
                        tx.addAndGet(transmitted - accounted);
                        accounted = transmitted;
                        long now = System.nanoTime();
                        if (now - lastReport > 150_000_000L) {
                            lastReport = now;
                            listener.onProgress(id, transmitted, size);
                        }
                    }
                    tx.addAndGet(queued - accounted);
                } catch (IOException e) {
                    outbox.release(f);
                    log("cannot read " + f.getName() + ": " + e.getMessage());
                    return 2;
                }
                JSONObject r = q.poll(20_000 + size / 20_000, TimeUnit.MILLISECONDS);
                if (r == null || "dead".equals(r.optString("t"))) {
                    outbox.release(f);
                    if (alive) {
                        log("no answer from the PC for " + id + ", reconnecting");
                        kill();
                    }
                    return 1;
                }
                if ("ack".equals(r.optString("t"))) {
                    sentCount++;
                    sentBytes += size;
                    listener.onProgress(id, size, size);
                    listener.onSent(f, id, r.optString("name"), r.optBoolean("dup"), size);
                    outbox.done(f);
                    listener.onChange();
                    return 0;
                }
                String code = r.optString("code");
                log("PC refused " + id + ": " + code);
                if ("disk".equals(code) || "paused".equals(code)) {
                    outbox.release(f);
                    return 2;
                }
                outbox.reject(f);
                listener.onRejected(f, id, code);
                return 3;
            } finally {
                waits.remove(id);
            }
        }
    }
}
