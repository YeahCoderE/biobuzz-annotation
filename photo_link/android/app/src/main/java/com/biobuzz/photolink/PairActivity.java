package com.biobuzz.photolink;

import android.Manifest;
import android.app.AlertDialog;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;

import com.biobuzz.photolink.core.LinkClient;
import com.biobuzz.photolink.core.Pairing;
import com.journeyapps.barcodescanner.ScanContract;
import com.journeyapps.barcodescanner.ScanOptions;

import java.util.ArrayList;
import java.util.List;

/** One-time pairing: scan the QR, pick a PC found on the network, or type its address and code. */
public class PairActivity extends AppCompatActivity {
    private Hub hub;
    private final Handler main = new Handler(Looper.getMainLooper());
    private TextView message, foundState;
    private LinearLayout foundList;
    private EditText hostInput, codeInput;
    private ActivityResultLauncher<ScanOptions> scanLauncher;
    private ActivityResultLauncher<String> camLauncher;
    private volatile boolean checking;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_pair);
        hub = Hub.get(this);
        message = findViewById(R.id.pairMessage);
        foundState = findViewById(R.id.foundState);
        foundList = findViewById(R.id.foundList);
        hostInput = findViewById(R.id.hostInput);
        codeInput = findViewById(R.id.codeInput);
        Button cancel = findViewById(R.id.btnCancel);
        if (hub.prefs.pairing() != null) {
            cancel.setVisibility(View.VISIBLE);
            cancel.setOnClickListener(v -> finish());
        }
        scanLauncher = registerForActivityResult(new ScanContract(), r -> {
            if (r.getContents() == null) return;
            Pairing p = Pairing.parse(r.getContents());
            if (p == null) show(R.string.pair_bad_link, true);
            else verify(p);
        });
        camLauncher = registerForActivityResult(new ActivityResultContracts.RequestPermission(), granted -> {
            if (granted) scan();
            else show(R.string.pair_no_camera, true);
        });
        findViewById(R.id.btnScan).setOnClickListener(v -> {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) scan();
            else camLauncher.launch(Manifest.permission.CAMERA);
        });
        findViewById(R.id.btnSearch).setOnClickListener(v -> search());
        findViewById(R.id.btnConnect).setOnClickListener(v -> connectManually());
        search();
    }

    private void show(int res, boolean warn) {
        message.setTextColor(warn ? 0xFFFF7043 : 0xFF9AA0A6);
        message.setText(res);
    }

    private void scan() {
        ScanOptions o = new ScanOptions();
        o.setDesiredBarcodeFormats(ScanOptions.QR_CODE);
        o.setPrompt(getString(R.string.pair_scan_prompt));
        o.setBeepEnabled(false);
        o.setOrientationLocked(false);
        o.setCaptureActivity(PortraitCaptureActivity.class);
        scanLauncher.launch(o);
    }

    // ---- PCs on the network --------------------------------------------------------------------------
    private void search() {
        foundState.setText(R.string.pair_searching);
        foundList.removeAllViews();
        hub.io.execute(() -> {
            List<LinkClient.Found> found = hub.client.discover(2500);
            main.post(() -> {
                if (isDestroyed()) return;
                foundState.setText(found.isEmpty() ? getString(R.string.pair_none_found) : "");
                for (LinkClient.Found f : found) {
                    Button btn = new Button(this);
                    btn.setAllCaps(false);
                    btn.setText(f.name + "   " + f.fromIp);
                    btn.setOnClickListener(v -> askCode(f));
                    foundList.addView(btn);
                }
            });
        });
    }

    private void askCode(LinkClient.Found f) {
        EditText input = new EditText(this);
        input.setHint(R.string.pair_code_hint);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);
        new AlertDialog.Builder(this).setTitle(f.name).setMessage(R.string.pair_ask_code).setView(input)
                .setPositiveButton(R.string.pair_connect, (d, w) -> {
                    if (!Pairing.validToken(input.getText().toString())) {
                        show(R.string.pair_bad_code, true);
                        return;
                    }
                    List<String> hosts = new ArrayList<>();
                    hosts.add(f.fromIp);
                    for (String ip : f.ips) if (!hosts.contains(ip)) hosts.add(ip);
                    Pairing p = new Pairing(hosts, f.port, input.getText().toString());
                    p.pcId = f.pcId;
                    p.pcName = f.name;
                    verify(p);
                }).setNegativeButton(R.string.cancel, null).show();
    }

    private void connectManually() {
        String host = hostInput.getText().toString().trim();
        String code = codeInput.getText().toString();
        int port = Pairing.DEFAULT_PORT;
        int colon = host.indexOf(':');
        if (colon > 0) {
            try {
                port = Integer.parseInt(host.substring(colon + 1).trim());
            } catch (NumberFormatException e) {
                show(R.string.pair_bad_link, true);
                return;
            }
            host = host.substring(0, colon).trim();
        }
        if (!Pairing.isPrivate(host)) {
            show(R.string.pair_private_only, true);
            return;
        }
        if (!Pairing.validToken(code) || port < 1 || port > 65535) {
            show(R.string.pair_bad_code, true);
            return;
        }
        List<String> hosts = new ArrayList<>();
        hosts.add(host);
        verify(new Pairing(hosts, port, code));
    }

    // ---- check the code by really connecting ------------------------------------------------------------
    private void verify(Pairing p) {
        if (checking) return;
        checking = true;
        show(R.string.pair_checking, false);
        hub.io.execute(() -> {
            Pairing old = hub.prefs.pairing();
            hub.client.start(p);
            hub.client.setPairing(p);
            int result = -1;  // 1 ok, 0 wrong code, -1 unreachable
            try {
                Thread.sleep(900);  // let the old connection drop before looking at the state
            } catch (InterruptedException ignored) {
            }
            long end = System.currentTimeMillis() + 12_000;
            while (System.currentTimeMillis() < end) {
                LinkClient.State s = hub.client.state();
                if (s == LinkClient.State.ONLINE) {
                    result = 1;
                    break;
                }
                if (s == LinkClient.State.FATAL) {
                    result = 0;
                    break;
                }
                try {
                    Thread.sleep(200);
                } catch (InterruptedException e) {
                    break;
                }
            }
            final int res = result;
            if (res == 1) {
                hub.prefs.savePairing(p);
                hub.lastFatal = "";
            } else if (old != null) {
                hub.client.setPairing(old);
            }
            main.post(() -> {
                checking = false;
                if (isDestroyed()) return;
                if (res == 1) {
                    setResult(RESULT_OK);
                    finish();
                } else {
                    show(res == 0 ? R.string.pair_bad_code : R.string.pair_unreachable, true);
                }
            });
        });
    }
}
