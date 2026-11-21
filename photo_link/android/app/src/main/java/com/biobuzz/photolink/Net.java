package com.biobuzz.photolink;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;

import java.io.IOException;
import java.io.UnsupportedEncodingException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLConnection;
import java.net.URLEncoder;

/** Small HTTP helpers. Traffic is pinned to the Wi-Fi network: when the Wi-Fi has no internet
 *  (a venue router, a hotspot) Android would otherwise send local requests over mobile data. */
final class Net {
    private static volatile Network wifi;
    private static ConnectivityManager.NetworkCallback callback;

    private Net() {}

    static synchronized void register(Context ctx) {
        if (callback != null) return;
        ConnectivityManager cm = ctx.getApplicationContext().getSystemService(ConnectivityManager.class);
        if (cm == null) return;
        NetworkRequest request = new NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build();
        callback = new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network network) { wifi = network; }
            @Override public void onLost(Network network) { if (network.equals(wifi)) wifi = null; }
        };
        try {
            cm.registerNetworkCallback(request, callback);
        } catch (RuntimeException e) {
            callback = null;
        }
    }

    static synchronized void unregister(Context ctx) {
        if (callback == null) return;
        ConnectivityManager cm = ctx.getApplicationContext().getSystemService(ConnectivityManager.class);
        try {
            if (cm != null) cm.unregisterNetworkCallback(callback);
        } catch (RuntimeException ignored) {
        }
        callback = null;
        wifi = null;
    }

    static String enc(String s) {
        try {
            return URLEncoder.encode(s, "UTF-8");
        } catch (UnsupportedEncodingException e) {
            return s;
        }
    }

    static HttpURLConnection open(String host, int port, String path) throws IOException {
        URL url = new URL("http://" + host + ":" + port + path);
        Network network = wifi;
        URLConnection c = network != null ? network.openConnection(url) : url.openConnection();
        return (HttpURLConnection) c;
    }

    /** HTTP status of GET /ping (200 = ok, 403 = wrong code), or -1 when the PC cannot be reached. */
    static int ping(String host, int port, String token) {
        HttpURLConnection c = null;
        try {
            c = open(host, port, "/ping?t=" + enc(token));
            c.setConnectTimeout(2500);
            c.setReadTimeout(4000);
            return c.getResponseCode();
        } catch (IOException e) {
            return -1;
        } finally {
            if (c != null) c.disconnect();
        }
    }
}
