package com.biobuzz.photolink;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;

import com.biobuzz.photolink.core.Pairing;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/** Settings and the saved pairing. */
public final class Prefs {
    private final SharedPreferences sp;

    public Prefs(Context c) {
        sp = c.getSharedPreferences("settings", Context.MODE_PRIVATE);
    }

    public int megapixels() { return sp.getInt("mp", 12); }                // 0 = sensor maximum
    public int quality() { return sp.getInt("quality", 92); }
    public boolean fastCapture() { return sp.getBoolean("fast", true); }
    public int connections() { return sp.getInt("connections", 2); }
    public boolean keepCopy() { return sp.getBoolean("keepCopy", false); }
    public boolean grid() { return sp.getBoolean("grid", false); }
    public boolean volumeShutter() { return sp.getBoolean("volume", true); }
    public boolean haptic() { return sp.getBoolean("haptic", true); }
    public boolean background() { return sp.getBoolean("background", true); }
    public int flash() { return sp.getInt("flash", 0); }                   // 0 off, 1 auto, 2 on
    public boolean notifAsked() { return sp.getBoolean("notifAsked", false); }

    public void putInt(String k, int v) { sp.edit().putInt(k, v).apply(); }
    public void putBool(String k, boolean v) { sp.edit().putBoolean(k, v).apply(); }

    public String deviceId() {
        String id = sp.getString("deviceId", null);
        if (id == null) {
            id = UUID.randomUUID().toString().replace("-", "").substring(0, 16);
            sp.edit().putString("deviceId", id).apply();
        }
        return id;
    }

    public String deviceName() {
        String m = Build.MODEL == null ? "Android" : Build.MODEL;
        String man = Build.MANUFACTURER == null ? "" : Build.MANUFACTURER;
        return m.toLowerCase().startsWith(man.toLowerCase()) || man.isEmpty() ? m : man + " " + m;
    }

    // ---- pairing -----------------------------------------------------------------------------
    public Pairing pairing() {
        String h = sp.getString("p_hosts", null);
        String t = sp.getString("p_token", null);
        if (h == null || !Pairing.validToken(t)) return null;
        List<String> hosts = new ArrayList<>();
        for (String x : h.split(",")) if (Pairing.isPrivate(x)) hosts.add(x);
        if (hosts.isEmpty()) return null;
        Pairing p = new Pairing(hosts, sp.getInt("p_port", Pairing.DEFAULT_PORT), t);
        p.pcId = sp.getString("p_pcId", "");
        p.pcName = sp.getString("p_pcName", "");
        p.lastGood = sp.getString("p_last", null);
        return p;
    }

    public void savePairing(Pairing p) {
        sp.edit().putString("p_hosts", android.text.TextUtils.join(",", p.hosts)).putInt("p_port", p.port)
                .putString("p_token", p.token).putString("p_pcId", p.pcId).putString("p_pcName", p.pcName)
                .putString("p_last", p.lastGood).apply();
    }

    public void clearPairing() {
        sp.edit().remove("p_hosts").remove("p_token").remove("p_pcId").remove("p_pcName").remove("p_last").apply();
    }
}
