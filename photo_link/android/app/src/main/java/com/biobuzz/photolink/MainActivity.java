package com.biobuzz.photolink;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.KeyEvent;
import android.widget.TextView;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;
import androidx.fragment.app.Fragment;

/** Four pages: camera, photos, status, settings. Pairing happens once in PairActivity. */
public class MainActivity extends AppCompatActivity {
    private static final int ACCENT = 0xFFF5A524, DIM = 0xFF9AA0A6;
    private Hub hub;
    private final TextView[] tabs = new TextView[4];
    private int current = -1;
    private ActivityResultLauncher<Intent> pairLauncher;
    private ActivityResultLauncher<String> notifLauncher;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_main);
        hub = Hub.get(this);
        tabs[0] = findViewById(R.id.tabCamera);
        tabs[1] = findViewById(R.id.tabPhotos);
        tabs[2] = findViewById(R.id.tabStatus);
        tabs[3] = findViewById(R.id.tabSettings);
        for (int i = 0; i < 4; i++) {
            final int idx = i;
            tabs[i].setOnClickListener(v -> show(idx));
        }
        pairLauncher = registerForActivityResult(new ActivityResultContracts.StartActivityForResult(), r -> {
            if (r.getResultCode() == RESULT_OK) afterPairing();
            else if (hub.prefs.pairing() == null) finish();
        });
        notifLauncher = registerForActivityResult(new ActivityResultContracts.RequestPermission(), granted -> { });
        // the camera page (and its permission dialog) only opens once the phone is paired
        if (hub.prefs.pairing() != null) show(0);
    }

    @Override protected void onStart() {
        super.onStart();
        hub.setVisible(true);
        if (hub.prefs.pairing() == null) {
            pairLauncher.launch(new Intent(this, PairActivity.class));
        } else {
            afterPairing();
        }
    }

    @Override protected void onStop() {
        super.onStop();
        hub.setVisible(false);
    }

    private void afterPairing() {
        if (current < 0) show(0);
        hub.startSync();
        if (hub.prefs.background()) SyncService.start(this);
        if (Build.VERSION.SDK_INT >= 33 && !hub.prefs.notifAsked()
                && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            hub.prefs.putBool("notifAsked", true);
            notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS);
        }
    }

    public void show(int idx) {
        if (idx == current) return;
        current = idx;
        Fragment f;
        switch (idx) {
            case 1: f = new PhotosFragment(); break;
            case 2: f = new StatusFragment(); break;
            case 3: f = new SettingsFragment(); break;
            default: f = new CameraFragment();
        }
        getSupportFragmentManager().beginTransaction().replace(R.id.container, f).commit();
        for (int i = 0; i < 4; i++) tabs[i].setTextColor(i == idx ? ACCENT : DIM);
    }

    public void openPairing() {
        pairLauncher.launch(new Intent(this, PairActivity.class));
    }

    /** Volume buttons work as a shutter on the camera page. */
    @Override public boolean onKeyDown(int keyCode, KeyEvent e) {
        if ((keyCode == KeyEvent.KEYCODE_VOLUME_DOWN || keyCode == KeyEvent.KEYCODE_VOLUME_UP) && current == 0
                && hub.prefs.volumeShutter()) {
            Fragment f = getSupportFragmentManager().findFragmentById(R.id.container);
            if (f instanceof CameraFragment) {
                if (e.getRepeatCount() == 0) ((CameraFragment) f).shoot();
                return true;
            }
        }
        return super.onKeyDown(keyCode, e);
    }
}
