package com.biobuzz.photolink.core;

import java.net.URLDecoder;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Where the PC receiver is: its local addresses, port, identity and the secret pairing code. */
public final class Pairing {
    public static final int DEFAULT_PORT = 8777;
    private static final Pattern IPV4 = Pattern.compile("(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})\\.(\\d{1,3})");

    public final List<String> hosts = new CopyOnWriteArrayList<>();
    public volatile int port;
    public final String token;
    public volatile String pcId = "";
    public volatile String pcName = "";
    public volatile String lastGood;

    public Pairing(List<String> hosts, int port, String token) {
        this.hosts.addAll(hosts);
        this.port = port;
        this.token = normToken(token);
    }

    /** Only RFC 1918 addresses: a scanned code must never make the phone send photos to the internet. */
    public static boolean isPrivate(String ip) {
        if (ip == null) return false;
        Matcher m = IPV4.matcher(ip.trim());
        if (!m.matches()) return false;
        int[] o = new int[4];
        for (int i = 0; i < 4; i++) {
            o[i] = Integer.parseInt(m.group(i + 1));
            if (o[i] > 255) return false;
        }
        return o[0] == 10 || (o[0] == 172 && o[1] >= 16 && o[1] <= 31) || (o[0] == 192 && o[1] == 168);
    }

    public static String normToken(String t) {
        return t == null ? "" : t.replaceAll("[^A-Za-z0-9]", "").toUpperCase();
    }

    public static boolean validToken(String t) {
        String n = normToken(t);
        return n.length() >= 8 && n.length() <= 64;
    }

    /** biobuzz://pair?h=192.168.1.10,10.0.0.5&p=8777&t=CODE&n=NAME&i=PCID  (null when invalid). */
    public static Pairing parse(String link) {
        if (link == null) return null;
        String s = link.trim();
        String prefix = "biobuzz://pair?";
        if (!s.startsWith(prefix)) return null;
        Map<String, String> m = new HashMap<>();
        try {
            for (String kv : s.substring(prefix.length()).split("&")) {
                int i = kv.indexOf('=');
                if (i > 0) m.put(kv.substring(0, i), URLDecoder.decode(kv.substring(i + 1), "UTF-8"));
            }
        } catch (Exception e) {
            return null;
        }
        String h = m.get("h");
        if (h == null || !validToken(m.get("t"))) return null;
        List<String> hosts = new ArrayList<>();
        for (String part : h.split(",")) {
            String ip = part.trim();
            if (isPrivate(ip) && !hosts.contains(ip)) hosts.add(ip);
        }
        if (hosts.isEmpty()) return null;
        int port = DEFAULT_PORT;
        try {
            if (m.get("p") != null) port = Integer.parseInt(m.get("p"));
        } catch (NumberFormatException e) {
            return null;
        }
        if (port < 1 || port > 65535) return null;
        Pairing p = new Pairing(hosts, port, m.get("t"));
        if (m.get("n") != null) p.pcName = m.get("n");
        if (m.get("i") != null) p.pcId = m.get("i");
        return p;
    }

    /** Addresses to try: the last one that worked, then those on the phone's own /24, then the rest. */
    public List<String> ordered(String phoneIp) {
        List<String> out = new ArrayList<>(hosts);
        String prefix = null;
        if (phoneIp != null && phoneIp.lastIndexOf('.') > 0) prefix = phoneIp.substring(0, phoneIp.lastIndexOf('.') + 1);
        final String pre = prefix;
        if (pre != null) {
            List<String> same = new ArrayList<>(), other = new ArrayList<>();
            for (String h : out) (h.startsWith(pre) ? same : other).add(h);
            out = new ArrayList<>(same);
            out.addAll(other);
        }
        String g = lastGood;
        if (g != null && out.remove(g)) out.add(0, g);
        return out;
    }

    public void addHostFirst(String ip) {
        if (isPrivate(ip) && !hosts.contains(ip)) hosts.add(0, ip);
    }
}
