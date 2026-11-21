package com.biobuzz.photolink;

import android.app.AlertDialog;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
import androidx.fragment.app.Fragment;

import com.biobuzz.photolink.core.Pairing;

/** Quality, upload, camera and background options. Changes apply at once (camera: next time it opens). */
public class SettingsFragment extends Fragment {
    private Hub hub;

    @Nullable @Override
    public View onCreateView(@NonNull LayoutInflater inf, @Nullable ViewGroup c, @Nullable Bundle b) {
        android.content.Context ctx = requireContext();
        hub = Hub.get(ctx);
        Prefs p = hub.prefs;
        ScrollView[] sv = new ScrollView[1];
        LinearLayout col = Ui.page(ctx, sv);
        col.addView(Ui.title(ctx, getString(R.string.tab_settings)));

        // ---- PC ----
        LinearLayout pc = Ui.card(ctx, col, getString(R.string.set_pc));
        Pairing pr = p.pairing();
        Ui.text(ctx, pc, pr == null ? "-" : pr.pcName + "\n" + android.text.TextUtils.join(", ", pr.hosts) + ":" + pr.port, 0xFFE8EAED, 15);
        Ui.button(ctx, pc, getString(R.string.set_change_pc), v -> ((MainActivity) requireActivity()).openPairing());
        Ui.button(ctx, pc, getString(R.string.set_forget_pc), v -> new AlertDialog.Builder(ctx).setMessage(R.string.set_forget_q)
                .setPositiveButton(R.string.ok, (d, w) -> {
                    hub.client.stop();
                    p.clearPairing();
                    SyncService.stop(ctx);
                    ((MainActivity) requireActivity()).openPairing();
                }).setNegativeButton(R.string.cancel, null).show());

        // ---- photo ----
        LinearLayout ph = Ui.card(ctx, col, getString(R.string.set_photo));
        final TextView mp = Ui.row(ctx, ph, getString(R.string.set_resolution));
        mp.setText(mpLabel(p.megapixels()));
        mp.setOnClickListener(v -> choose(R.string.set_resolution, new String[]{"8 MP", "12 MP", "16 MP", getString(R.string.set_max)},
                new int[]{8, 12, 16, 0}, p.megapixels(), val -> {
                    p.putInt("mp", val);
                    mp.setText(mpLabel(val));
                }));
        mp.setTextColor(0xFFF5A524);
        final TextView ql = Ui.row(ctx, ph, getString(R.string.set_quality));
        ql.setText(p.quality() + "%");
        SeekBar sb = new SeekBar(ctx);
        sb.setMax(30);
        sb.setProgress(p.quality() - 70);
        sb.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(SeekBar s, int v, boolean user) {
                ql.setText((70 + v) + "%");
                if (user) p.putInt("quality", 70 + v);
            }

            @Override public void onStartTrackingTouch(SeekBar s) { }
            @Override public void onStopTrackingTouch(SeekBar s) { }
        });
        ph.addView(sb);
        Ui.text(ctx, ph, getString(R.string.set_quality_hint), 0xFF9AA0A6, 12);
        Ui.toggle(ctx, ph, getString(R.string.set_fast), getString(R.string.set_fast_hint), p.fastCapture(), (s, on) -> p.putBool("fast", on));

        // ---- upload ----
        LinearLayout up = Ui.card(ctx, col, getString(R.string.set_upload));
        final TextView cn = Ui.row(ctx, up, getString(R.string.set_connections));
        cn.setText(String.valueOf(p.connections()));
        cn.setTextColor(0xFFF5A524);
        cn.setOnClickListener(v -> choose(R.string.set_connections, new String[]{"1", "2", "3", "4"}, new int[]{1, 2, 3, 4}, p.connections(), val -> {
            p.putInt("connections", val);
            hub.client.setConnections(val);
            cn.setText(String.valueOf(val));
            hub.client.reconnectNow();
        }));
        Ui.text(ctx, up, getString(R.string.set_connections_hint), 0xFF9AA0A6, 12);
        Ui.toggle(ctx, up, getString(R.string.set_keep_copy), getString(R.string.set_keep_copy_hint), p.keepCopy(), (s, on) -> p.putBool("keepCopy", on));

        // ---- camera ----
        LinearLayout cam = Ui.card(ctx, col, getString(R.string.set_camera));
        Ui.toggle(ctx, cam, getString(R.string.set_grid), null, p.grid(), (s, on) -> p.putBool("grid", on));
        Ui.toggle(ctx, cam, getString(R.string.set_volume), getString(R.string.set_volume_hint), p.volumeShutter(), (s, on) -> p.putBool("volume", on));
        Ui.toggle(ctx, cam, getString(R.string.set_haptic), null, p.haptic(), (s, on) -> p.putBool("haptic", on));

        // ---- background ----
        LinearLayout bg = Ui.card(ctx, col, getString(R.string.set_background));
        Ui.toggle(ctx, bg, getString(R.string.set_service), getString(R.string.set_service_hint), p.background(), (s, on) -> {
            p.putBool("background", on);
            if (on) SyncService.start(ctx);
            else SyncService.stop(ctx);
        });

        LinearLayout about = Ui.card(ctx, col, getString(R.string.set_about));
        Ui.text(ctx, about, "BioBuzz Photo Link 2.0\n" + getString(R.string.set_about_text), 0xFF9AA0A6, 13);
        return sv[0];
    }

    private String mpLabel(int mp) {
        return mp == 0 ? getString(R.string.set_max) : mp + " MP";
    }

    private interface IntCb { void on(int v); }

    private void choose(int title, String[] labels, int[] values, int current, IntCb cb) {
        int sel = 0;
        for (int i = 0; i < values.length; i++) if (values[i] == current) sel = i;
        new AlertDialog.Builder(requireContext()).setTitle(title).setSingleChoiceItems(labels, sel, (d, which) -> {
            cb.on(values[which]);
            d.dismiss();
        }).setNegativeButton(R.string.cancel, null).show();
    }
}
