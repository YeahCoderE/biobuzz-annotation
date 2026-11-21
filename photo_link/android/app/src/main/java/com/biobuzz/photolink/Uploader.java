package com.biobuzz.photolink;

import android.content.Context;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.util.Arrays;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;

/** Sends every photo in the outbox folder to the PC, oldest first, and deletes it once the PC
 *  confirms. Photos stay on disk while the PC is unreachable and go out when it comes back. */
final class Uploader {
    private static final int OK = 0, NETWORK = 1, AUTH = 2, REJECTED = 3;

    private final Context app;
    private final File outbox;
    private final File rejected;
    private final Runnable onChange;
    private final Semaphore signal = new Semaphore(0);
    private volatile Pairing pairing;
    private volatile boolean running;
    private volatile boolean authFailed;
    private volatile boolean online;
    private volatile int sent;
    private Thread thread;

    Uploader(Context ctx, Pairing pairing, Runnable onChange) {
        this.app = ctx.getApplicationContext();
        this.pairing = pairing;
        this.onChange = onChange;
        outbox = new File(app.getFilesDir(), "outbox");
        rejected = new File(app.getFilesDir(), "rejected");
        outbox.mkdirs();
        rejected.mkdirs();
        File[] stale = outbox.listFiles((d, n) -> n.endsWith(".tmp"));
        if (stale != null) for (File f : stale) f.delete();  // photos that never finished saving
    }

    File outbox() { return outbox; }
    boolean isOnline() { return online; }
    boolean authFailed() { return authFailed; }
    int sent() { return sent; }

    int pending() {
        File[] files = outbox.listFiles((d, n) -> n.endsWith(".jpg"));
        return files == null ? 0 : files.length;
    }

    void setPairing(Pairing p) {
        pairing = p;
        authFailed = false;
        wakeUp();
    }

    void wakeUp() { signal.release(); }

    synchronized void start() {
        if (running) return;
        running = true;
        thread = new Thread(this::loop, "uploader");
        thread.setDaemon(true);
        thread.start();
    }

    synchronized void stop() {
        running = false;
        wakeUp();
    }

    private File[] files() {
        File[] files = outbox.listFiles((d, n) -> n.endsWith(".jpg"));
        if (files == null) return new File[0];
        Arrays.sort(files, (a, b) -> a.getName().compareTo(b.getName()));
        return files;
    }

    private void loop() {
        while (running) {
            Pairing p = pairing;
            boolean progress = false;
            if (p != null && !authFailed) {
                File[] files = files();
                if (files.length == 0) {
                    probe(p);
                } else {
                    for (File f : files) {
                        if (!running) break;
                        int r = send(p, f);
                        if (r == OK) {
                            f.delete();
                            sent++;
                            online = true;
                            progress = true;
                            onChange.run();
                        } else if (r == REJECTED) {
                            f.renameTo(new File(rejected, f.getName()));
                        } else if (r == AUTH) {
                            authFailed = true;
                            online = false;
                            break;
                        } else {
                            online = false;
                            break;
                        }
                    }
                }
            }
            onChange.run();
            try {
                signal.tryAcquire(progress ? 200 : 4000, TimeUnit.MILLISECONDS);
            } catch (InterruptedException e) {
                return;
            }
            signal.drainPermits();
        }
    }

    /** Idle check so the screen can say "connected" before the first photo. */
    private void probe(Pairing p) {
        boolean ok = false;
        for (String host : p.ordered()) {
            int code = Net.ping(host, p.port, p.token);
            if (code == 200) {
                remember(p, host);
                ok = true;
                break;
            }
            if (code == 403) {
                authFailed = true;
                break;
            }
        }
        online = ok;
    }

    private void remember(Pairing p, String host) {
        if (!host.equals(p.lastGood)) {
            p.lastGood = host;
            p.save(app);
        }
    }

    private int send(Pairing p, File f) {
        long length = f.length();
        if (length <= 0) return REJECTED;
        String name = f.getName();
        String base = name.substring(0, name.length() - 4);  // <id>_<takenAtMs>
        int us = base.indexOf('_');
        String id = us > 0 ? base.substring(0, us) : base;
        String taken = us > 0 ? base.substring(us + 1) : "0";
        for (String host : p.ordered()) {
            HttpURLConnection c = null;
            try {
                c = Net.open(host, p.port, "/upload?t=" + Net.enc(p.token));
                c.setConnectTimeout(2500);
                c.setReadTimeout(30000);
                c.setRequestMethod("POST");
                c.setDoOutput(true);
                c.setFixedLengthStreamingMode(length);
                c.setRequestProperty("Content-Type", "image/jpeg");
                c.setRequestProperty("X-Photo-Id", id);
                c.setRequestProperty("X-Taken-At", taken);
                try (InputStream in = new FileInputStream(f); OutputStream out = c.getOutputStream()) {
                    byte[] buf = new byte[64 * 1024];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                }
                int code = c.getResponseCode();
                if (code == 200) {
                    remember(p, host);
                    return OK;
                }
                if (code == 403) return AUTH;
                if (code == 400 || code == 411 || code == 413) return REJECTED;
                return NETWORK;
            } catch (IOException e) {
                // this address did not work: try the next one
            } finally {
                if (c != null) c.disconnect();
            }
        }
        return NETWORK;
    }
}
