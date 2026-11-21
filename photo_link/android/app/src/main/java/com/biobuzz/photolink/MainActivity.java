package com.biobuzz.photolink;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.HapticFeedbackConstants;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.OrientationEventListener;
import android.view.Surface;
import android.view.View;
import android.view.ScaleGestureDetector;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AppCompatActivity;
import androidx.camera.core.Camera;
import androidx.camera.core.CameraSelector;
import androidx.camera.core.FocusMeteringAction;
import androidx.camera.core.ImageCapture;
import androidx.camera.core.ImageCaptureException;
import androidx.camera.core.MeteringPoint;
import androidx.camera.core.Preview;
import androidx.camera.core.ZoomState;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.camera.view.PreviewView;
import androidx.core.content.ContextCompat;

import com.google.common.util.concurrent.ListenableFuture;
import com.journeyapps.barcodescanner.ScanContract;
import com.journeyapps.barcodescanner.ScanOptions;

import java.io.File;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

public class MainActivity extends AppCompatActivity {
    private static final int OK_COLOR = 0xFF4CAF50;
    private static final int WARN_COLOR = 0xFFFF7043;

    private final Handler main = new Handler(Looper.getMainLooper());

    private View cameraScreen, pairScreen, flash, manualBox, cancelButton;
    private PreviewView preview;
    private TextView status, pairTitle, pairHelp, pairMessage;
    private Button primaryButton;
    private EditText hostInput, codeInput;

    private ActivityResultLauncher<String> permissionLauncher;
    private ActivityResultLauncher<ScanOptions> scanLauncher;

    private Pairing pairing;
    private Uploader uploader;
    private ImageCapture imageCapture;
    private ProcessCameraProvider cameraProvider;
    private Camera camera;
    private OrientationEventListener orientation;
    private boolean permissionAsked, permissionDenied, wantPairing;
    private boolean pinching;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        cameraScreen = findViewById(R.id.cameraScreen);
        pairScreen = findViewById(R.id.pairScreen);
        flash = findViewById(R.id.flash);
        manualBox = findViewById(R.id.manualBox);
        cancelButton = findViewById(R.id.cancelButton);
        preview = findViewById(R.id.preview);
        status = findViewById(R.id.status);
        pairTitle = findViewById(R.id.pairTitle);
        pairHelp = findViewById(R.id.pairHelp);
        pairMessage = findViewById(R.id.pairMessage);
        primaryButton = findViewById(R.id.primaryButton);
        hostInput = findViewById(R.id.hostInput);
        codeInput = findViewById(R.id.codeInput);
        preview.setScaleType(PreviewView.ScaleType.FIT_CENTER);

        Net.register(this);
        pairing = Pairing.load(this);

        permissionLauncher = registerForActivityResult(new ActivityResultContracts.RequestPermission(), granted -> {
            if (!granted) permissionDenied = true;
            route();
        });
        scanLauncher = registerForActivityResult(new ScanContract(), result -> {
            if (result.getContents() == null) return;
            Pairing p = Pairing.parse(result.getContents());
            if (p == null) showPairMessage(R.string.pair_bad_link);
            else verifyAndSave(p);
        });

        findViewById(R.id.shutter).setOnClickListener(v -> shoot());
        findViewById(R.id.settingsButton).setOnClickListener(v -> {
            wantPairing = true;
            route();
        });
        cancelButton.setOnClickListener(v -> {
            wantPairing = false;
            route();
        });
        findViewById(R.id.connectButton).setOnClickListener(v -> connectManually());
        setupTouch();

        orientation = new OrientationEventListener(this) {
            @Override public void onOrientationChanged(int o) {
                if (o == ORIENTATION_UNKNOWN || imageCapture == null) return;
                int rotation = (o >= 315 || o < 45) ? Surface.ROTATION_0
                        : o >= 225 ? Surface.ROTATION_90
                        : o >= 135 ? Surface.ROTATION_180 : Surface.ROTATION_270;
                imageCapture.setTargetRotation(rotation);
            }
        };
    }

    @Override
    protected void onStart() {
        super.onStart();
        route();
    }

    @Override
    protected void onStop() {
        super.onStop();
        orientation.disable();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (uploader != null) uploader.stop();
        Net.unregister(this);
    }

    // ---- screens -----------------------------------------------------------------------------
    private boolean hasCameraPermission() {
        return ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
    }

    private void route() {
        if (!hasCameraPermission()) {
            showPermission();
            if (!permissionAsked) {  // ask right away on the first start
                permissionAsked = true;
                permissionLauncher.launch(Manifest.permission.CAMERA);
            }
        } else if (pairing == null || wantPairing) {
            showPairing();
        } else {
            showCamera();
        }
    }

    private void showPermission() {
        cameraScreen.setVisibility(View.GONE);
        pairScreen.setVisibility(View.VISIBLE);
        manualBox.setVisibility(View.GONE);
        cancelButton.setVisibility(View.GONE);
        pairTitle.setText(R.string.perm_title);
        pairHelp.setText(permissionDenied ? getString(R.string.perm_denied) + "\n\n" + getString(R.string.perm_why)
                : getString(R.string.perm_why));
        pairMessage.setText("");
        boolean blocked = permissionDenied && !shouldShowRequestPermissionRationale(Manifest.permission.CAMERA);
        primaryButton.setText(blocked ? R.string.perm_settings : R.string.perm_grant);
        primaryButton.setOnClickListener(v -> {
            if (blocked) {
                startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.fromParts("package", getPackageName(), null)));
            } else {
                permissionLauncher.launch(Manifest.permission.CAMERA);
            }
        });
    }

    private void showPairing() {
        stopCamera();
        cameraScreen.setVisibility(View.GONE);
        pairScreen.setVisibility(View.VISIBLE);
        manualBox.setVisibility(View.VISIBLE);
        cancelButton.setVisibility(pairing != null ? View.VISIBLE : View.GONE);
        pairTitle.setText(R.string.pair_title);
        pairHelp.setText(R.string.pair_help);
        primaryButton.setText(R.string.pair_scan);
        primaryButton.setOnClickListener(v -> scan());
    }

    private void showCamera() {
        pairScreen.setVisibility(View.GONE);
        cameraScreen.setVisibility(View.VISIBLE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (uploader == null) {
            uploader = new Uploader(this, pairing, () -> main.post(this::updateStatus));
            uploader.start();
        }
        orientation.enable();
        startCamera();
        updateStatus();
    }

    private void showPairMessage(int res) {
        pairMessage.setTextColor(WARN_COLOR);
        pairMessage.setText(res);
    }

    // ---- pairing -----------------------------------------------------------------------------
    private void scan() {
        ScanOptions options = new ScanOptions();
        options.setDesiredBarcodeFormats(ScanOptions.QR_CODE);
        options.setPrompt(getString(R.string.pair_scan_prompt));
        options.setBeepEnabled(false);
        options.setOrientationLocked(false);
        options.setCaptureActivity(PortraitCaptureActivity.class);
        scanLauncher.launch(options);
    }

    private void connectManually() {
        String host = hostInput.getText().toString().trim();
        String code = codeInput.getText().toString().trim();
        int port = Pairing.DEFAULT_PORT;
        int colon = host.indexOf(':');
        if (colon > 0) {
            try {
                port = Integer.parseInt(host.substring(colon + 1).trim());
            } catch (NumberFormatException e) {
                showPairMessage(R.string.pair_bad_link);
                return;
            }
            host = host.substring(0, colon).trim();
        }
        if (!Pairing.isPrivate(host)) {
            showPairMessage(R.string.pair_private_only);
            return;
        }
        if (!Pairing.validToken(code) || port < 1 || port > 65535) {
            showPairMessage(R.string.pair_bad_code);
            return;
        }
        List<String> hosts = new ArrayList<>();
        hosts.add(host);
        verifyAndSave(new Pairing(hosts, port, code));
    }

    private void verifyAndSave(Pairing p) {
        pairMessage.setTextColor(0xFF9AA0A6);
        pairMessage.setText(R.string.pair_checking);
        new Thread(() -> {
            int result = -1;
            String good = null;
            for (String host : p.hosts) {
                int code = Net.ping(host, p.port, p.token);
                if (code == 200) {
                    good = host;
                    result = 200;
                    break;
                }
                if (code == 403) {
                    result = 403;
                    break;
                }
            }
            final String goodHost = good;
            final int res = result;
            main.post(() -> {
                if (isDestroyed()) return;
                if (res == 200) {
                    p.lastGood = goodHost;
                    p.save(this);
                    pairing = p;
                    wantPairing = false;
                    if (uploader != null) uploader.setPairing(p);
                    pairMessage.setTextColor(OK_COLOR);
                    pairMessage.setText(R.string.pair_ok);
                    route();
                } else {
                    showPairMessage(res == 403 ? R.string.pair_bad_code : R.string.pair_unreachable);
                }
            });
        }, "pair-check").start();
    }

    // ---- camera ------------------------------------------------------------------------------
    private void startCamera() {
        ListenableFuture<ProcessCameraProvider> future = ProcessCameraProvider.getInstance(this);
        future.addListener(() -> {
            try {
                ProcessCameraProvider provider = future.get();
                cameraProvider = provider;
                Preview previewUse = new Preview.Builder().build();
                previewUse.setSurfaceProvider(preview.getSurfaceProvider());
                imageCapture = new ImageCapture.Builder()
                        .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                        .build();
                provider.unbindAll();
                camera = provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, previewUse, imageCapture);
            } catch (Exception e) {
                Toast.makeText(this, R.string.camera_failed, Toast.LENGTH_LONG).show();
            }
        }, ContextCompat.getMainExecutor(this));
    }

    private void stopCamera() {
        if (cameraProvider != null) cameraProvider.unbindAll();
        imageCapture = null;
        camera = null;
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    private void shoot() {
        if (imageCapture == null || uploader == null) return;
        String id = UUID.randomUUID().toString().replace("-", "").substring(0, 16);
        long takenAt = System.currentTimeMillis();
        File dir = uploader.outbox();
        File tmp = new File(dir, id + "_" + takenAt + ".tmp");
        File done = new File(dir, id + "_" + takenAt + ".jpg");
        flash.setAlpha(0.7f);
        flash.animate().alpha(0f).setDuration(160).start();
        findViewById(R.id.shutter).performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY);
        imageCapture.takePicture(new ImageCapture.OutputFileOptions.Builder(tmp).build(),
                ContextCompat.getMainExecutor(this), new ImageCapture.OnImageSavedCallback() {
                    @Override public void onImageSaved(ImageCapture.OutputFileResults results) {
                        if (tmp.renameTo(done) && uploader != null) uploader.wakeUp();  // goes out right away
                        updateStatus();
                    }

                    @Override public void onError(ImageCaptureException exception) {
                        tmp.delete();
                        Toast.makeText(MainActivity.this, R.string.shot_failed, Toast.LENGTH_SHORT).show();
                    }
                });
    }

    /** Volume buttons work as a shutter. */
    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if ((keyCode == KeyEvent.KEYCODE_VOLUME_DOWN || keyCode == KeyEvent.KEYCODE_VOLUME_UP)
                && cameraScreen.getVisibility() == View.VISIBLE) {
            if (event.getRepeatCount() == 0) shoot();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    /** Tap to focus, pinch to zoom. */
    private void setupTouch() {
        ScaleGestureDetector scale = new ScaleGestureDetector(this, new ScaleGestureDetector.SimpleOnScaleGestureListener() {
            @Override public boolean onScale(ScaleGestureDetector d) {
                pinching = true;
                if (camera != null) {
                    ZoomState zoom = camera.getCameraInfo().getZoomState().getValue();
                    if (zoom != null) camera.getCameraControl().setZoomRatio(zoom.getZoomRatio() * d.getScaleFactor());
                }
                return true;
            }
        });
        preview.setOnTouchListener((v, e) -> {
            scale.onTouchEvent(e);
            if (e.getActionMasked() == MotionEvent.ACTION_DOWN) pinching = false;
            if (e.getActionMasked() == MotionEvent.ACTION_UP && !pinching && camera != null) {
                MeteringPoint point = preview.getMeteringPointFactory().createPoint(e.getX(), e.getY());
                camera.getCameraControl().startFocusAndMetering(new FocusMeteringAction.Builder(point).build());
                v.performClick();
            }
            return true;
        });
    }

    // ---- status ------------------------------------------------------------------------------
    private void updateStatus() {
        if (uploader == null || isDestroyed()) return;
        int pending = uploader.pending();
        String text;
        int color;
        if (uploader.authFailed()) {
            text = getString(R.string.status_auth);
            color = WARN_COLOR;
        } else if (uploader.isOnline()) {
            text = getString(R.string.status_online, uploader.sent());
            color = OK_COLOR;
        } else {
            text = getString(R.string.status_offline);
            color = WARN_COLOR;
        }
        if (pending > 0 && !uploader.authFailed()) text += " · " + getString(R.string.status_pending, pending);
        status.setText(text);
        status.setTextColor(color);
    }
}
