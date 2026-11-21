package com.biobuzz.photolink;

import android.app.AlertDialog;
import android.graphics.Bitmap;
import android.os.Bundle;
import android.text.format.DateFormat;
import android.text.format.Formatter;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
import androidx.fragment.app.Fragment;
import androidx.recyclerview.widget.GridLayoutManager;
import androidx.recyclerview.widget.RecyclerView;

import com.biobuzz.photolink.core.Outbox;

import java.io.File;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;

/** Queue (waiting / uploading) on top, then what was already sent. */
public class PhotosFragment extends Fragment {
    private static final int QUEUED = 0, UPLOADING = 3;

    private static final class Item {
        String id, name, code;
        int state;          // QUEUED, UPLOADING, History.SENT, History.REFUSED
        int percent;
        long takenAt, size;
        File file;
    }

    private Hub hub;
    private RecyclerView list;
    private TextView summary, empty;
    private final List<Item> items = new ArrayList<>();
    private final Runnable observer = this::reload;
    private Adapter adapter;

    @Nullable @Override
    public View onCreateView(@NonNull LayoutInflater inf, @Nullable ViewGroup c, @Nullable Bundle b) {
        View v = inf.inflate(R.layout.fragment_photos, c, false);
        hub = Hub.get(requireContext());
        list = v.findViewById(R.id.list);
        summary = v.findViewById(R.id.photosSummary);
        empty = v.findViewById(R.id.photosEmpty);
        list.setLayoutManager(new GridLayoutManager(requireContext(), 3));
        adapter = new Adapter();
        list.setAdapter(adapter);
        v.findViewById(R.id.btnDelQueued).setOnClickListener(x -> confirm(R.string.photos_del_queued_q, () -> {
            for (File f : hub.outbox.list()) if (!hub.outbox.isClaimed(f)) hub.outbox.done(f);
            reload();
        }));
        v.findViewById(R.id.btnClearHist).setOnClickListener(x -> confirm(R.string.photos_clear_hist_q, () -> {
            hub.history.clear();
            reload();
        }));
        return v;
    }

    @Override public void onStart() {
        super.onStart();
        hub.observe(observer);
        reload();
    }

    @Override public void onStop() {
        super.onStop();
        hub.unobserve(observer);
    }

    private void confirm(int msg, Runnable ok) {
        new AlertDialog.Builder(requireContext()).setMessage(msg)
                .setPositiveButton(R.string.ok, (d, w) -> ok.run()).setNegativeButton(R.string.cancel, null).show();
    }

    private void reload() {
        if (!isAdded() || getView() == null) return;
        items.clear();
        List<File> pending = hub.outbox.list();
        for (int i = pending.size() - 1; i >= 0; i--) {
            File f = pending.get(i);
            Item it = new Item();
            it.id = Outbox.idOf(f);
            it.file = f;
            it.takenAt = Outbox.takenOf(f);
            it.size = f.length();
            long[] pr = hub.progress.get(it.id);
            it.state = pr != null ? UPLOADING : QUEUED;
            it.percent = pr != null && pr[1] > 0 ? (int) (pr[0] * 100 / pr[1]) : 0;
            items.add(it);
        }
        int queued = items.size();
        for (History.Entry e : hub.history.list()) {
            Item it = new Item();
            it.id = e.id;
            it.state = e.state;
            it.name = e.name;
            it.code = e.code;
            it.takenAt = e.takenAt;
            it.size = e.size;
            items.add(it);
        }
        summary.setText(getString(R.string.photos_summary, queued, hub.history.sentToday(), hub.history.list().size()));
        empty.setVisibility(items.isEmpty() ? View.VISIBLE : View.GONE);
        adapter.notifyDataSetChanged();
    }

    private void details(Item it) {
        ImageView iv = new ImageView(requireContext());
        iv.setAdjustViewBounds(true);
        Bitmap bm = it.file != null && it.file.exists() ? Thumbs.decode(it.file, 900) : hub.thumbs.get(it.id);
        if (bm != null) iv.setImageBitmap(bm);
        String when = DateFormat.format("yyyy-MM-dd HH:mm:ss", new Date(it.takenAt)).toString();
        String state;
        switch (it.state) {
            case History.SENT: state = getString(R.string.state_sent) + (it.name == null ? "" : " → " + it.name); break;
            case History.REFUSED: state = getString(R.string.state_refused) + " (" + it.code + ")"; break;
            case UPLOADING: state = getString(R.string.state_uploading, it.percent); break;
            default: state = getString(R.string.state_queued);
        }
        AlertDialog.Builder b = new AlertDialog.Builder(requireContext()).setView(iv)
                .setTitle(when).setMessage(state + (it.size > 0 ? "\n" + Formatter.formatShortFileSize(requireContext(), it.size) : ""))
                .setPositiveButton(R.string.ok, null);
        if (it.state == QUEUED && it.file != null) {
            b.setNegativeButton(R.string.delete, (d, w) -> {
                hub.outbox.done(it.file);
                reload();
            });
        }
        b.show();
    }

    private final class Adapter extends RecyclerView.Adapter<Adapter.VH> {
        final class VH extends RecyclerView.ViewHolder {
            ImageView thumb;
            ProgressBar bar;
            TextView badge;

            VH(View v) {
                super(v);
                thumb = v.findViewById(R.id.thumb);
                bar = v.findViewById(R.id.bar);
                badge = v.findViewById(R.id.badge);
            }
        }

        @NonNull @Override public VH onCreateViewHolder(@NonNull ViewGroup p, int t) {
            return new VH(LayoutInflater.from(p.getContext()).inflate(R.layout.item_photo, p, false));
        }

        @Override public void onBindViewHolder(@NonNull VH h, int pos) {
            Item it = items.get(pos);
            h.thumb.setImageBitmap(null);
            h.thumb.setTag(it.id);
            Bitmap cached = hub.thumbs.get(it.id);
            if (cached != null) {
                h.thumb.setImageBitmap(cached);
            }
            switch (it.state) {
                case History.SENT: h.badge.setText("✓"); h.badge.setTextColor(0xFF4CAF50); break;
                case History.REFUSED: h.badge.setText("✕"); h.badge.setTextColor(0xFFFF7043); break;
                case UPLOADING: h.badge.setText("⬆ " + it.percent + "%"); h.badge.setTextColor(0xFFF5A524); break;
                default: h.badge.setText("⏳"); h.badge.setTextColor(0xFFE8EAED);
            }
            h.bar.setVisibility(it.state == UPLOADING ? View.VISIBLE : View.GONE);
            h.bar.setProgress(it.percent);
            h.itemView.setOnClickListener(v -> details(it));
        }

        @Override public int getItemCount() { return items.size(); }
    }
}
