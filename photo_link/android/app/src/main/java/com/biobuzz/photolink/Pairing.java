package com.biobuzz.photolink;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

/** Where the PC receiver is: its local addresses, port and the secret code from the QR. */
final class Pairing {
    static final int DEFAULT_PORT = 8777;
    private static final Pattern TOKEN = Pattern.compile("[A-Za-z0-9_-]{8,64}");
    private static final Pattern IPV4 = Pattern.compile("(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})");

    final List<String> hosts;
    final int port;
    final String token;
    volatile String lastGood;

    Pairing(List<String> hosts, int port, String token) {
        this.hosts = hosts;
        this.port = port;
        this.token = token;
    }

    /** Only RFC 1918 addresses: a scanned QR must never make the phone send photos to the internet. */
    static boolean isPrivate(String ip) {
        java.util.regex.Matcher m = IPV4.matcher(ip);
        if (!m.matches()) return false;
        int[] o = new int[4];
        for (int i = 0; i < 4; i++) {
            o[i] = Integer.parseInt(m.group(i + 1));
            if (o[i] > 255) return false;
        }
        return o[0] == 10 || (o[0] == 172 && o[1] >= 16 && o[1] <= 31) || (o[0] == 192 && o[1] == 168);
    }

    static boolean validToken(String t) {
        return t != null && TOKEN.matcher(t).matches();
    }

    /** biobuzz://pair?h=192.168.1.10,10.0.0.5&p=8777&t=CODE  (null when invalid). */
    static Pairing parse(String link) {
        if (link == null) return null;
        Uri u;
        try {
            u = Uri.parse(link.trim());
        } catch (RuntimeException e) {
            return null;
        }
        if (!"biobuzz".equals(u.getScheme()) || !"pair".equals(u.getHost())) return null;
        String h = u.getQueryParameter("h");
        String t = u.getQueryParameter("t");
        if (h == null || !validToken(t)) return null;
        List<String> hosts = new ArrayList<>();
        for (String part : h.split(",")) {
            String ip = part.trim();
            if (isPrivate(ip) && !hosts.contains(ip)) hosts.add(ip);
        }
        if (hosts.isEmpty()) return null;
        int port = DEFAULT_PORT;
        try {
            String p = u.getQueryParameter("p");
            if (p != null) port = Integer.parseInt(p);
        } catch (NumberFormatException e) {
            return null;
        }
        if (port < 1 || port > 65535) return null;
        return new Pairing(hosts, port, t);
    }

    static Pairing load(Context c) {
        SharedPreferences sp = c.getSharedPreferences("pairing", Context.MODE_PRIVATE);
        String h = sp.getString("hosts", null);
        String t = sp.getString("token", null);
        if (h == null || !validToken(t)) return null;
        List<String> hosts = new ArrayList<>();
        for (String part : h.split(",")) {
            if (isPrivate(part)) hosts.add(part);
        }
        if (hosts.isEmpty()) return null;
        Pairing p = new Pairing(hosts, sp.getInt("port", DEFAULT_PORT), t);
        p.lastGood = sp.getString("lastGood", null);
        return p;
    }

    void save(Context c) {
        c.getSharedPreferences("pairing", Context.MODE_PRIVATE).edit()
                .putString("hosts", android.text.TextUtils.join(",", hosts))
                .putInt("port", port)
                .putString("token", token)
                .putString("lastGood", lastGood)
                .apply();
    }

    /** Addresses to try: the one that worked last first. */
    List<String> ordered() {
        List<String> out = new ArrayList<>(hosts);
        String g = lastGood;
        if (g != null && out.remove(g)) out.add(0, g);
        return out;
    }
}
