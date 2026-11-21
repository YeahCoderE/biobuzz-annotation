package com.biobuzz.photolink;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/** The last photos that left the phone (sent or refused), newest first. */
public final class History {
    public static final int SENT = 1, REFUSED = 2;

    public static final class Entry {
        public String id, name, code;
        public long takenAt, doneAt, size;
        public int state;
    }

    private final File file;
    private final List<Entry> items = new ArrayList<>();

    public History(File base) {
        file = new File(base, "history.json");
        try {
            byte[] b = new byte[(int) file.length()];
            try (java.io.FileInputStream in = new java.io.FileInputStream(file)) {
                int off = 0, n;
                while (off < b.length && (n = in.read(b, off, b.length - off)) > 0) off += n;
            }
            JSONArray a = new JSONArray(new String(b, StandardCharsets.UTF_8));
            for (int i = 0; i < a.length(); i++) {
                JSONObject o = a.getJSONObject(i);
                Entry e = new Entry();
                e.id = o.optString("id");
                e.name = o.optString("name");
                e.code = o.optString("code");
                e.takenAt = o.optLong("taken");
                e.doneAt = o.optLong("done");
                e.size = o.optLong("size");
                e.state = o.optInt("state");
                items.add(e);
            }
        } catch (Exception ignored) {
        }
    }

    public synchronized List<Entry> list() { return new ArrayList<>(items); }

    public synchronized int sentToday() {
        long start = System.currentTimeMillis() - 24L * 3600 * 1000;
        java.util.Calendar c = java.util.Calendar.getInstance();
        c.set(java.util.Calendar.HOUR_OF_DAY, 0);
        c.set(java.util.Calendar.MINUTE, 0);
        c.set(java.util.Calendar.SECOND, 0);
        start = c.getTimeInMillis();
        int n = 0;
        for (Entry e : items) if (e.state == SENT && e.doneAt >= start) n++;
        return n;
    }

    public synchronized void add(Entry e) {
        items.add(0, e);
        while (items.size() > 300) items.remove(items.size() - 1);
        save();
    }

    public synchronized void clear() {
        items.clear();
        save();
    }

    private void save() {
        try {
            JSONArray a = new JSONArray();
            for (Entry e : items) {
                JSONObject o = new JSONObject();
                o.put("id", e.id).put("name", e.name).put("code", e.code).put("taken", e.takenAt)
                        .put("done", e.doneAt).put("size", e.size).put("state", e.state);
                a.put(o);
            }
            try (FileOutputStream out = new FileOutputStream(file)) {
                out.write(a.toString().getBytes(StandardCharsets.UTF_8));
            }
        } catch (JSONException | java.io.IOException ignored) {
        }
    }
}
