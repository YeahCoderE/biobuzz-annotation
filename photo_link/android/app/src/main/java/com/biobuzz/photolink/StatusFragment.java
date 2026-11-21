package com.biobuzz.photolink;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.format.Formatter;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
import androidx.core.content.ContextCompat;
import androidx.fragment.app.Fragment;

import com.biobuzz.photolink.core.LinkClient;
import com.biobuzz.photolink.core.Pairing;

import java.util.List;

/** Connection, transfer numbers, background permissions, tools and the log. */
public class StatusFragment extends Fragment {
    private Hub hub;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Runnable observer = this::update;
    private TextView connTitle, connSub, vLatency, vSpeed, vAvg, vQueue, vSent, vToday, vService, vBattery, vNotif, vWifi, log, speedResult;
    private ActivityResultLauncher<String> notifLauncher;
    private long sessionStart;

    @Nullable @Override
    public View onCreateView(@NonNull LayoutInflater inf, @Nullable ViewGroup c, @Nullable Bundle b) {
        android.content.Context ctx = requireContext();
        hub = Hub.get(ctx);
        sessionStart = System.currentTimeMillis();
        notifLauncher = registerForActivityResult(new ActivityResultContracts.RequestPermission(), g -> update());
        ScrollView[] sv = new ScrollView[1];
        LinearLayout col = Ui.page(ctx, sv);
        col.addView(Ui.title(ctx, getString(R.string.tab_status)));

        LinearLayout conn = Ui.card(ctx, col, null);
        connTitle = Ui.text(ctx, conn, "", 0xFFE8EAED, 20);
        connTitle.setTypeface(null, android.graphics.Typeface.BOLD);
        connSub = Ui.text(ctx, conn, "", 0xFF9AA0A6, 13);
        vLatency = Ui.row(ctx, conn, getString(R.string.st_latency));
        vWifi = Ui.row(ctx, conn, getString(R.string.st_connections));

        LinearLayout tr = Ui.card(ctx, col, getString(R.string.st_transfer));
        vSpeed = Ui.row(ctx, tr, getString(R.string.st_speed));
        vAvg = Ui.row(ctx, tr, getString(R.string.st_avg));
        vQueue = Ui.row(ctx, tr, getString(R.string.st_queue));
        vSent = Ui.row(ctx, tr, getString(R.string.st_sent_session));
        vToday = Ui.row(ctx, tr, getString(R.string.st_sent_today));

        LinearLayout bg = Ui.card(ctx, col, getString(R.string.st_background));
        vService = Ui.row(ctx, bg, getString(R.string.st_service));
        vNotif = Ui.row(ctx, bg, getString(R.string.st_notif));
        vBattery = Ui.row(ctx, bg, getString(R.string.st_battery));
        Ui.button(ctx, bg, getString(R.string.st_fix_battery), v -> batteryExemption());
        Ui.button(ctx, bg, getString(R.string.st_fix_notif), v -> {
            if (Build.VERSION.SDK_INT >= 33) notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS);
            else startActivity(new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, ctx.getPackageName()));
        });

        LinearLayout tools = Ui.card(ctx, col, getString(R.string.st_tools));
        Ui.button(ctx, tools, getString(R.string.st_reconnect), v -> {
            hub.client.setPairing(hub.prefs.pairing() != null ? hub.prefs.pairing() : hub.client.pairing());
            Toast.makeText(ctx, R.string.st_reconnecting, Toast.LENGTH_SHORT).show();
        });
        Ui.button(ctx, tools, getString(R.string.st_find_pc), v -> findPc());
        Ui.button(ctx, tools, getString(R.string.st_speedtest), v -> speedTest());
        speedResult = Ui.text(ctx, tools, "", 0xFFF5A524, 15);
        Ui.button(ctx, tools, getString(R.string.st_repair), v -> ((MainActivity) requireActivity()).openPairing());
        Ui.button(ctx, tools, getString(R.string.st_share_log), v -> shareLog());

        LinearLayout lg = Ui.card(ctx, col, getString(R.string.st_log));
        log = Ui.text(ctx, lg, "", 0xFF9AA0A6, 11);
        log.setTypeface(android.graphics.Typeface.MONOSPACE);
        log.setTextIsSelectable(true);
        return sv[0];
    }

    @Override public void onStart() {
        super.onStart();
        hub.observe(observer);
        update();
    }

    @Override public void onStop() {
        super.onStop();
        hub.unobserve(observer);
    }

    private void update() {
        if (!isAdded() || getView() == null) return;
        LinkClient c = hub.client;
        Pairing p = c.pairing();
        switch (c.state()) {
            case ONLINE:
                connTitle.setText("● " + getString(R.string.chip_online, c.pcName()));
                connTitle.setTextColor(0xFF4CAF50);
                connSub.setText(c.host() + (p != null ? ":" + p.port : ""));
                break;
            case FATAL:
                connTitle.setText("● " + getString(R.string.chip_fatal));
                connTitle.setTextColor(0xFFFF7043);
                connSub.setText(fatalText(c.fatalCode()));
                break;
            default:
                connTitle.setText("● " + getString(R.string.chip_connecting));
                connTitle.setTextColor(0xFFF5A524);
                connSub.setText(p != null ? p.pcName + "  " + android.text.TextUtils.join(", ", p.hosts) : "");
        }
        vLatency.setText(c.latencyMs() >= 0 && c.state() == LinkClient.State.ONLINE ? c.latencyMs() + " ms" : "-");
        vWifi.setText(String.valueOf(c.sockets()));
        vSpeed.setText(String.format("%.1f Mbps", c.mbpsNow()));
        double secs = Math.max(1, (System.currentTimeMillis() - sessionStart) / 1000.0);
        vAvg.setText(String.format("%.1f Mbps", c.sentBytes() * 8 / 1e6 / secs));
        vQueue.setText(hub.outbox.count() + " · " + Formatter.formatShortFileSize(requireContext(), hub.outbox.bytes()));
        vSent.setText(c.sentCount() + " · " + Formatter.formatShortFileSize(requireContext(), c.sentBytes()));
        vToday.setText(String.valueOf(hub.history.sentToday()));
        vService.setText(hub.prefs.background() ? getString(R.string.on) : getString(R.string.off));
        boolean notif = Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(requireContext(), Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
        vNotif.setText(notif ? getString(R.string.allowed) : getString(R.string.not_allowed));
        vNotif.setTextColor(notif ? 0xFF4CAF50 : 0xFFFF7043);
        PowerManager pm = (PowerManager) requireContext().getSystemService(android.content.Context.POWER_SERVICE);
        boolean ignoring = pm != null && pm.isIgnoringBatteryOptimizations(requireContext().getPackageName());
        vBattery.setText(ignoring ? getString(R.string.unrestricted) : getString(R.string.optimized));
        vBattery.setTextColor(ignoring ? 0xFF4CAF50 : 0xFFFF7043);
        List<String> lines = c.logLines();
        StringBuilder sb = new StringBuilder();
        for (int i = Math.max(0, lines.size() - 25); i < lines.size(); i++) sb.append(lines.get(i)).append('\n');
        log.setText(sb.length() == 0 ? "-" : sb.toString().trim());
    }

    private String fatalText(String code) {
        if ("blocked".equals(code)) return getString(R.string.fatal_blocked);
        return getString(R.string.fatal_code);
    }

    private void batteryExemption() {
        try {
            startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + requireContext().getPackageName())));
        } catch (RuntimeException e) {
            startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        }
    }

    private void findPc() {
        Toast.makeText(requireContext(), R.string.st_searching, Toast.LENGTH_SHORT).show();
        hub.io.execute(() -> {
            List<LinkClient.Found> found = hub.client.discover(2500);
            Pairing p = hub.prefs.pairing();
            String msg = found.isEmpty() ? getString(R.string.st_none_found) : getString(R.string.st_found, found.get(0).name, found.get(0).fromIp);
            if (p != null) {
                for (LinkClient.Found f : found) {
                    if (p.pcId.isEmpty() || p.pcId.equals(f.pcId)) {
                        p.addHostFirst(f.fromIp);
                        p.port = f.port;
                        hub.prefs.savePairing(p);
                        hub.client.setPairing(p);
                        break;
                    }
                }
            }
            main.post(() -> {
                if (isAdded()) Toast.makeText(requireContext(), msg, Toast.LENGTH_LONG).show();
            });
        });
    }

    private void speedTest() {
        speedResult.setText(R.string.st_testing);
        hub.io.execute(() -> {
            double mbps = hub.client.speedTest(12L << 20);
            main.post(() -> {
                if (!isAdded()) return;
                speedResult.setText(mbps < 0 ? getString(R.string.st_test_failed) : getString(R.string.st_test_result, mbps));
            });
        });
    }

    private void shareLog() {
        StringBuilder sb = new StringBuilder("BioBuzz Photo Link 2.0\n" + Build.MANUFACTURER + " " + Build.MODEL + " Android " + Build.VERSION.RELEASE + "\n");
        sb.append("state=").append(hub.client.state()).append(" queue=").append(hub.outbox.count())
                .append(" speed=").append(String.format("%.1f", hub.client.mbpsNow())).append(" Mbps\n\n");
        for (String l : hub.client.logLines()) sb.append(l).append('\n');
        Intent i = new Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, sb.toString());
        startActivity(Intent.createChooser(i, getString(R.string.st_share_log)));
    }
}
