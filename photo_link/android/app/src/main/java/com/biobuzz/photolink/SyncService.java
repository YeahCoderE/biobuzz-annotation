package com.biobuzz.photolink;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

import com.biobuzz.photolink.core.LinkClient;

/** Foreground service: keeps the process (and so the connection to the PC) alive in the background. */
public class SyncService extends Service {
    private static final String CHANNEL = "link";
    private static final int ID = 7;
    public static final String ACTION_STOP = "com.biobuzz.photolink.STOP";

    private Hub hub;
    private final Runnable refresh = this::updateNotification;
    private long lastUpdate;

    public static void start(Context c) {
        try {
            ContextCompat.startForegroundService(c, new Intent(c, SyncService.class));
        } catch (RuntimeException ignored) {  // background start not allowed: the app will retry when opened
        }
    }

    public static void stop(Context c) {
        c.stopService(new Intent(c, SyncService.class));
    }

    @Override public void onCreate() {
        super.onCreate();
        hub = Hub.get(this);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, getString(R.string.notif_channel), NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            getSystemService(NotificationManager.class).createNotificationChannel(ch);
        }
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            hub.prefs.putBool("background", false);
            stopSelf();
            return START_NOT_STICKY;
        }
        try {
            startForeground(ID, build());
        } catch (RuntimeException e) {
            stopSelf();
            return START_NOT_STICKY;
        }
        hub.startSync();
        hub.observe(refresh);
        return START_STICKY;
    }

    @Override public void onDestroy() {
        hub.unobserve(refresh);
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    private void updateNotification() {
        long now = System.currentTimeMillis();
        if (now - lastUpdate < 1500) return;
        lastUpdate = now;
        getSystemService(NotificationManager.class).notify(ID, build());
    }

    private Notification build() {
        LinkClient c = hub.client;
        int queue = hub.outbox.count();
        String title;
        switch (c.state()) {
            case ONLINE: title = getString(R.string.notif_online, c.pcName()); break;
            case FATAL: title = getString(R.string.notif_fatal); break;
            default: title = getString(R.string.notif_searching);
        }
        String text = queue > 0 ? getString(R.string.notif_queue, queue, Math.round(c.mbpsNow())) : getString(R.string.notif_idle, hub.history.sentToday());
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, SyncService.class).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE);
        return new NotificationCompat.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setContentTitle(title).setContentText(text).setOnlyAlertOnce(true).setOngoing(true)
                .setContentIntent(open).addAction(0, getString(R.string.notif_stop), stop).build();
    }
}
