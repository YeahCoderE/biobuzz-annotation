package com.biobuzz.photolink.core;

import java.io.File;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/** Photos waiting to go to the PC: one file per photo named {@code <id>_<takenAtMs>.jpg}.
 *  A photo is only deleted after the PC acknowledged it. */
public final class Outbox {
    private final File dir;
    private final File rejected;
    private final Object lock = new Object();
    private final Set<String> claimed = new HashSet<>();

    public Outbox(File base) {
        dir = new File(base, "outbox");
        rejected = new File(base, "rejected");
        dir.mkdirs();
        rejected.mkdirs();
        File[] stale = dir.listFiles((d, n) -> n.endsWith(".tmp"));
        if (stale != null) for (File f : stale) f.delete();  // photos that never finished saving
    }

    public File dir() { return dir; }

    public File tempFile(String id, long takenMs) {
        return new File(dir, id + "_" + takenMs + ".tmp");
    }

    /** Called when the camera finished writing the temp file: it becomes visible to the uploaders. */
    public File commit(File tmp) {
        String p = tmp.getPath();
        File f = new File(p.substring(0, p.length() - 4) + ".jpg");
        if (!tmp.renameTo(f)) return null;
        wake();
        return f;
    }

    public List<File> list() {
        File[] files = dir.listFiles((d, n) -> n.endsWith(".jpg"));
        if (files == null) return new ArrayList<>();
        Arrays.sort(files, (a, b) -> a.getName().compareTo(b.getName()));
        return new ArrayList<>(Arrays.asList(files));
    }

    public int count() {
        File[] files = dir.listFiles((d, n) -> n.endsWith(".jpg"));
        return files == null ? 0 : files.length;
    }

    public long bytes() {
        long sum = 0;
        File[] files = dir.listFiles((d, n) -> n.endsWith(".jpg"));
        if (files != null) for (File f : files) sum += f.length();
        return sum;
    }

    public boolean isClaimed(File f) {
        synchronized (lock) {
            return claimed.contains(f.getName());
        }
    }

    /** Takes the oldest photo nobody is sending yet; waits up to waitMs for one to appear. */
    public File claim(long waitMs) throws InterruptedException {
        synchronized (lock) {
            for (int round = 0; round < 2; round++) {
                for (File f : list()) {
                    if (claimed.add(f.getName())) return f;
                }
                if (round == 0) lock.wait(waitMs);
            }
            return null;
        }
    }

    public void release(File f) {
        synchronized (lock) {
            claimed.remove(f.getName());
            lock.notifyAll();
        }
    }

    public void done(File f) {
        f.delete();
        release(f);
    }

    /** The PC refused this photo for good (not a JPEG, too large): keep it out of the queue. */
    public void reject(File f) {
        File to = new File(rejected, f.getName());
        if (!f.renameTo(to)) f.delete();
        release(f);
    }

    public void wake() {
        synchronized (lock) {
            lock.notifyAll();
        }
    }

    public static String idOf(File f) {
        String n = f.getName();
        n = n.substring(0, n.lastIndexOf('.'));
        int us = n.indexOf('_');
        return us > 0 ? n.substring(0, us) : n;
    }

    public static long takenOf(File f) {
        String n = f.getName();
        n = n.substring(0, n.lastIndexOf('.'));
        int us = n.indexOf('_');
        try {
            return us > 0 ? Long.parseLong(n.substring(us + 1)) : 0L;
        } catch (NumberFormatException e) {
            return 0L;
        }
    }
}
