package com.biobuzz.photolink;

import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.LinkAddress;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.MediaStore;

import com.biobuzz.photolink.core.LinkClient;
import com.biobuzz.photolink.core.Outbox;
import com.biobuzz.photolink.core.Pairing;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.net.SocketFactory;

/** Everything that lives as long as the process: queue, connection engine, history, thumbnails. */
public final class Hub implements LinkClient.Env, LinkClient.Listener {
    private static Hub instance;

    public static synchronized Hub get(Context c) {
        if (instance == null) instance = new Hub(c.getApplicationContext());
        return instance;
    }

    public final Context app;
    public final Prefs prefs;
    public final Outbox outbox;
    public final History history;
    public final Thumbs thumbs;
    public final LinkClient client;
    public final ExecutorService io = Executors.newFixedThreadPool(2);
    public final Map<String, long[]> progress = new ConcurrentHashMap<>();

    private final Handler main = new Handler(Looper.getMainLooper());
    private final List<Runnable> observers = new CopyOnWriteArrayList<>();
    private final ConnectivityManager cm;
    private final WifiManager wifi;
    private final PowerManager power;
    private volatile Network wifiNet;
    private WifiManager.WifiLock wifiLock;
    private PowerManager.WakeLock wakeLock;
    private boolean uiPosted;
    private volatile boolean appVisible;
    public volatile String lastFatal = "";

    private Hub(Context c) {
        app = c;
        prefs = new Prefs(c);
        outbox = new Outbox(c.getFilesDir());
        history = new History(c.getFilesDir());
        thumbs = new Thumbs(c.getFilesDir());
        client = new LinkClient(outbox, this, this);
        cm = c.getSystemService(ConnectivityManager.class);
        wifi = (WifiManager) c.getSystemService(Context.WIFI_SERVICE);
        power = (PowerManager) c.getSystemService(Context.POWER_SERVICE);
        registerWifiCallback();
    }

    // ---- lifecycle ---------------------------------------------------------------------------------
    /** Starts (or re-points) the connection engine when the phone is paired. */
    public void startSync() {
        Pairing p = prefs.pairing();
        if (p == null) return;
        client.setConnections(prefs.connections());
        client.start(p);
    }

    public void setVisible(boolean v) {
        appVisible = v;
        updateLocks();
    }

    public void observe(Runnable r) { observers.add(r); }
    public void unobserve(Runnable r) { observers.remove(r); }

    /** Coalesced notification of the UI (at most about 7 per second). */
    private void changed() {
        synchronized (this) {
            if (uiPosted) return;
            uiPosted = true;
        }
        main.postDelayed(() -> {
            synchronized (Hub.this) {
                uiPosted = false;
            }
            for (Runnable r : observers) r.run();
            updateLocks();
        }, 150);
    }

    // ---- a photo was taken ---------------------------------------------------------------------------
    public void photoTaken(File tmp, String id) {
        io.execute(() -> {
            thumbs.create(tmp, id);               // preview first, then release it for upload
            outbox.commit(tmp);
            changed();
        });
    }

    // ---- power / Wi-Fi locks ---------------------------------------------------------------------------
    private synchronized void updateLocks() {
        boolean busy = client.state() == LinkClient.State.ONLINE && (outbox.count() > 0 || appVisible);
        try {
            if (busy) {
                if (wifiLock == null && wifi != null) {
                    int mode = Build.VERSION.SDK_INT >= 29 ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
                    wifiLock = wifi.createWifiLock(mode, "biobuzz:link");
                    wifiLock.setReferenceCounted(false);
                    wifiLock.acquire();
                }
                if (outbox.count() > 0 && wakeLock == null && power != null) {
                    wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "biobuzz:upload");
                    wakeLock.setReferenceCounted(false);
                    wakeLock.acquire(20 * 60 * 1000L);
                }
            }
            if (!busy || outbox.count() == 0) {
                if (wakeLock != null) {
                    if (wakeLock.isHeld()) wakeLock.release();
                    wakeLock = null;
                }
            }
            if (!busy && wifiLock != null) {
                if (wifiLock.isHeld()) wifiLock.release();
                wifiLock = null;
            }
        } catch (RuntimeException ignored) {
        }
    }

    // ---- Wi-Fi network (traffic must use it even when it has no internet) ----------------------------
    private void registerWifiCallback() {
        if (cm == null) return;
        NetworkRequest req = new NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build();
        try {
            cm.registerNetworkCallback(req, new ConnectivityManager.NetworkCallback() {
                @Override public void onAvailable(Network n) { wifiNet = n; client.reconnectNowIfOffline(); }
                @Override public void onLost(Network n) { if (n.equals(wifiNet)) wifiNet = null; }
            });
        } catch (RuntimeException ignored) {
        }
    }

    // ---- LinkClient.Env --------------------------------------------------------------------------------
    @Override public SocketFactory socketFactory() {
        Network n = wifiNet;
        return n == null ? null : n.getSocketFactory();
    }

    @Override public void bindDatagram(DatagramSocket s) throws IOException {
        Network n = wifiNet;
        if (n != null) n.bindSocket(s);
    }

    @Override public String localIp() {
        Network n = wifiNet;
        if (n == null || cm == null) return null;
        LinkProperties lp = cm.getLinkProperties(n);
        if (lp == null) return null;
        for (LinkAddress a : lp.getLinkAddresses()) {
            if (a.getAddress() instanceof Inet4Address) return a.getAddress().getHostAddress();
        }
        return null;
    }

    @Override public int battery() {
        Intent i = app.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (i == null) return -1;
        int level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1), scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
        return level < 0 ? -1 : Math.round(level * 100f / scale);
    }

    @Override public boolean charging() {
        Intent i = app.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        return i != null && i.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0;
    }

    @Override @SuppressWarnings("deprecation") public int rssi() {
        try {
            return wifi == null ? 0 : wifi.getConnectionInfo().getRssi();
        } catch (RuntimeException e) {
            return 0;
        }
    }

    @Override public String deviceId() { return prefs.deviceId(); }
    @Override public String deviceName() { return prefs.deviceName(); }

    // ---- LinkClient.Listener -----------------------------------------------------------------------------
    @Override public void onChange() { changed(); }

    @Override public void onProgress(String id, long sent, long total) {
        if (sent >= total) progress.remove(id);
        else progress.put(id, new long[]{sent, total});
        changed();
    }

    @Override public void onSent(File f, String id, String serverName, boolean dup, long bytes) {
        History.Entry e = new History.Entry();
        e.id = id;
        e.name = serverName;
        e.takenAt = com.biobuzz.photolink.core.Outbox.takenOf(f);
        e.doneAt = System.currentTimeMillis();
        e.size = bytes;
        e.state = History.SENT;
        history.add(e);
        if (prefs.keepCopy()) saveCopy(f);
        progress.remove(id);
        changed();
    }

    @Override public void onRejected(File f, String id, String code) {
        History.Entry e = new History.Entry();
        e.id = id;
        e.code = code;
        e.takenAt = com.biobuzz.photolink.core.Outbox.takenOf(f);
        e.doneAt = System.currentTimeMillis();
        e.state = History.REFUSED;
        history.add(e);
        progress.remove(id);
        changed();
    }

    @Override public void onFatal(String code) {
        lastFatal = code;
        changed();
    }

    @Override public void onPairingUpdated(Pairing p) { prefs.savePairing(p); }

    @Override public void onLog(String line) { }

    /** Optional copy in Pictures/BioBuzz (Android 10+, needs no permission). */
    private void saveCopy(File f) {
        if (Build.VERSION.SDK_INT < 29) return;
        try {
            ContentValues v = new ContentValues();
            v.put(MediaStore.Images.Media.DISPLAY_NAME, "BioBuzz_" + f.getName().replace(".jpg", "") + ".jpg");
            v.put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg");
            v.put(MediaStore.Images.Media.RELATIVE_PATH, "Pictures/BioBuzz");
            android.net.Uri uri = app.getContentResolver().insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, v);
            if (uri == null) return;
            try (OutputStream out = app.getContentResolver().openOutputStream(uri); FileInputStream in = new FileInputStream(f)) {
                byte[] buf = new byte[64 * 1024];
                int n;
                while (out != null && (n = in.read(buf)) > 0) out.write(buf, 0, n);
            }
        } catch (IOException | RuntimeException ignored) {
        }
    }
}
