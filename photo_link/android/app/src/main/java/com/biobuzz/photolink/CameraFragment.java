package com.biobuzz.photolink;

import android.Manifest;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Size;
import android.view.HapticFeedbackConstants;
import android.view.LayoutInflater;
import android.view.MotionEvent;
import android.view.ScaleGestureDetector;
import android.view.Surface;
import android.view.View;
import android.view.ViewGroup;
import android.view.OrientationEventListener;
import android.widget.ImageView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
import androidx.camera.core.AspectRatio;
import androidx.camera.core.Camera;
import androidx.camera.core.CameraSelector;
import androidx.camera.core.FocusMeteringAction;
import androidx.camera.core.ImageCapture;
import androidx.camera.core.ImageCaptureException;
import androidx.camera.core.MeteringPoint;
import androidx.camera.core.Preview;
import androidx.camera.core.ZoomState;
import androidx.camera.core.resolutionselector.AspectRatioStrategy;
import androidx.camera.core.resolutionselector.ResolutionSelector;
import androidx.camera.core.resolutionselector.ResolutionStrategy;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.camera.view.PreviewView;
import androidx.core.content.ContextCompat;
import androidx.fragment.app.Fragment;

import com.biobuzz.photolink.core.LinkClient;
import com.biobuzz.photolink.core.Outbox;
import com.google.common.util.concurrent.ListenableFuture;

import java.io.File;
import java.util.UUID;

public class CameraFragment extends Fragment {
    private Hub hub;
    private PreviewView preview;
    private View flash, grid, shutter, permCard;
    private TextView chip, btnFlash, queueBadge;
    private ImageView lastThumb;
    private ImageCapture imageCapture;
    private ProcessCameraProvider provider;
    private Camera camera;
    private boolean back = true, pinching, burst, holding;
    private OrientationEventListener orientation;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Runnable observer = this::refresh;
    private ActivityResultLauncher<String> permLauncher;

    @Nullable @Override
    public View onCreateView(@NonNull LayoutInflater inf, @Nullable ViewGroup c, @Nullable Bundle b) {
        View v = inf.inflate(R.layout.fragment_camera, c, false);
        hub = Hub.get(requireContext());
        preview = v.findViewById(R.id.preview);
        flash = v.findViewById(R.id.flash);
        grid = v.findViewById(R.id.gridOverlay);
        shutter = v.findViewById(R.id.shutter);
        permCard = v.findViewById(R.id.permCard);
        chip = v.findViewById(R.id.chipStatus);
        btnFlash = v.findViewById(R.id.btnFlash);
        queueBadge = v.findViewById(R.id.queueBadge);
        lastThumb = v.findViewById(R.id.lastThumb);
        preview.setScaleType(PreviewView.ScaleType.FIT_CENTER);
        permLauncher = registerForActivityResult(new ActivityResultContracts.RequestPermission(), granted -> {
            if (granted) startCamera();
            else permCard.setVisibility(View.VISIBLE);
        });
        grid.setVisibility(hub.prefs.grid() ? View.VISIBLE : View.GONE);
        v.findViewById(R.id.btnGrid).setOnClickListener(x -> {
            boolean on = grid.getVisibility() != View.VISIBLE;
            grid.setVisibility(on ? View.VISIBLE : View.GONE);
            hub.prefs.putBool("grid", on);
        });
        btnFlash.setOnClickListener(x -> {
            hub.prefs.putInt("flash", (hub.prefs.flash() + 1) % 3);
            applyFlash();
        });
        v.findViewById(R.id.btnLens).setOnClickListener(x -> {
            back = !back;
            startCamera();
        });
        lastThumb.setOnClickListener(x -> ((MainActivity) requireActivity()).show(1));
        permCard.setOnClickListener(x -> permLauncher.launch(Manifest.permission.CAMERA));
        setupShutter();
        setupTouch();
        orientation = new OrientationEventListener(requireContext()) {
            @Override public void onOrientationChanged(int o) {
                if (o == ORIENTATION_UNKNOWN || imageCapture == null) return;
                imageCapture.setTargetRotation((o >= 315 || o < 45) ? Surface.ROTATION_0
                        : o >= 225 ? Surface.ROTATION_90 : o >= 135 ? Surface.ROTATION_180 : Surface.ROTATION_270);
            }
        };
        return v;
    }

    @Override public void onStart() {
        super.onStart();
        hub.observe(observer);
        orientation.enable();
        if (ContextCompat.checkSelfPermission(requireContext(), Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            permCard.setVisibility(View.GONE);
            startCamera();
        } else {
            permCard.setVisibility(View.VISIBLE);
            permLauncher.launch(Manifest.permission.CAMERA);
        }
        refresh();
    }

    @Override public void onStop() {
        super.onStop();
        hub.unobserve(observer);
        orientation.disable();
        if (provider != null) provider.unbindAll();
        imageCapture = null;
        camera = null;
    }

    // ---- camera --------------------------------------------------------------------------------------
    private void startCamera() {
        ListenableFuture<ProcessCameraProvider> future = ProcessCameraProvider.getInstance(requireContext());
        future.addListener(() -> {
            try {
                provider = future.get();
                int mp = hub.prefs.megapixels();
                ResolutionSelector.Builder rs = new ResolutionSelector.Builder()
                        .setAspectRatioStrategy(AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY);
                if (mp == 0) {
                    rs.setResolutionStrategy(ResolutionStrategy.HIGHEST_AVAILABLE_STRATEGY);
                } else {
                    int w = (int) Math.round(Math.sqrt(mp * 1_000_000d * 4 / 3)), h = w * 3 / 4;
                    rs.setResolutionStrategy(new ResolutionStrategy(new Size(w, h), ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER));
                }
                Preview pv = new Preview.Builder()
                        .setResolutionSelector(new ResolutionSelector.Builder()
                                .setAspectRatioStrategy(AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY).build()).build();
                pv.setSurfaceProvider(preview.getSurfaceProvider());
                imageCapture = new ImageCapture.Builder()
                        .setCaptureMode(hub.prefs.fastCapture() ? ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY : ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                        .setJpegQuality(hub.prefs.quality())
                        .setResolutionSelector(rs.build()).build();
                provider.unbindAll();
                camera = provider.bindToLifecycle(getViewLifecycleOwner(),
                        back ? CameraSelector.DEFAULT_BACK_CAMERA : CameraSelector.DEFAULT_FRONT_CAMERA, pv, imageCapture);
                applyFlash();
            } catch (Exception e) {
                if (isAdded()) Toast.makeText(requireContext(), R.string.camera_failed, Toast.LENGTH_LONG).show();
            }
        }, ContextCompat.getMainExecutor(requireContext()));
    }

    private void applyFlash() {
        int f = hub.prefs.flash();
        btnFlash.setText(f == 0 ? "⚡✕" : f == 1 ? "⚡A" : "⚡");
        if (imageCapture != null) {
            imageCapture.setFlashMode(f == 0 ? ImageCapture.FLASH_MODE_OFF : f == 1 ? ImageCapture.FLASH_MODE_AUTO : ImageCapture.FLASH_MODE_ON);
        }
    }

    public void shoot() {
        if (imageCapture == null || !isAdded()) return;
        String id = UUID.randomUUID().toString().replace("-", "").substring(0, 16);
        long takenAt = System.currentTimeMillis();
        File tmp = hub.outbox.tempFile(id, takenAt);
        flash.setAlpha(0.6f);
        flash.animate().alpha(0f).setDuration(140).start();
        if (hub.prefs.haptic()) shutter.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY);
        imageCapture.takePicture(new ImageCapture.OutputFileOptions.Builder(tmp).build(),
                ContextCompat.getMainExecutor(requireContext()), new ImageCapture.OnImageSavedCallback() {
                    @Override public void onImageSaved(@NonNull ImageCapture.OutputFileResults r) {
                        hub.photoTaken(tmp, id);
                        main.postDelayed(CameraFragment.this::refresh, 400);
                        if (burst && holding) main.postDelayed(CameraFragment.this::shoot, 120);
                    }

                    @Override public void onError(@NonNull ImageCaptureException e) {
                        tmp.delete();
                        if (isAdded()) Toast.makeText(requireContext(), R.string.shot_failed, Toast.LENGTH_SHORT).show();
                    }
                });
    }

    /** Tap = one photo, hold = continuous shooting until released. */
    private void setupShutter() {
        final Runnable startBurst = () -> {
            if (holding) {
                burst = true;
                shoot();
            }
        };
        shutter.setOnTouchListener((v, e) -> {
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    holding = true;
                    burst = false;
                    v.setPressed(true);
                    main.postDelayed(startBurst, 450);
                    return true;
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    v.setPressed(false);
                    main.removeCallbacks(startBurst);
                    if (e.getActionMasked() == MotionEvent.ACTION_UP && !burst) shoot();
                    holding = false;
                    burst = false;
                    return true;
                default:
                    return true;
            }
        });
    }

    /** Tap to focus, pinch to zoom. */
    private void setupTouch() {
        ScaleGestureDetector scale = new ScaleGestureDetector(requireContext(), new ScaleGestureDetector.SimpleOnScaleGestureListener() {
            @Override public boolean onScale(ScaleGestureDetector d) {
                pinching = true;
                if (camera != null) {
                    ZoomState z = camera.getCameraInfo().getZoomState().getValue();
                    if (z != null) camera.getCameraControl().setZoomRatio(z.getZoomRatio() * d.getScaleFactor());
                }
                return true;
            }
        });
        preview.setOnTouchListener((v, e) -> {
            scale.onTouchEvent(e);
            if (e.getActionMasked() == MotionEvent.ACTION_DOWN) pinching = false;
            if (e.getActionMasked() == MotionEvent.ACTION_UP && !pinching && camera != null) {
                MeteringPoint p = preview.getMeteringPointFactory().createPoint(e.getX(), e.getY());
                camera.getCameraControl().startFocusAndMetering(new FocusMeteringAction.Builder(p).build());
                v.performClick();
            }
            return true;
        });
    }

    // ---- status chip + last photo ----------------------------------------------------------------------
    private void refresh() {
        if (!isAdded() || getView() == null) return;
        LinkClient c = hub.client;
        int q = hub.outbox.count();
        String s;
        int color;
        switch (c.state()) {
            case ONLINE:
                s = getString(R.string.chip_online, c.pcName());
                color = 0xFF4CAF50;
                break;
            case FATAL:
                s = getString(R.string.chip_fatal);
                color = 0xFFFF7043;
                break;
            default:
                s = getString(R.string.chip_connecting);
                color = 0xFFF5A524;
        }
        chip.setText("● " + s);
        chip.setTextColor(color);
        queueBadge.setText(q > 0 ? getString(R.string.badge_queue, q) : getString(R.string.badge_sent, hub.history.sentToday()));
        File newest = null;
        java.util.List<File> pending = hub.outbox.list();
        String lastId = null;
        if (!pending.isEmpty()) lastId = Outbox.idOf(pending.get(pending.size() - 1));
        else if (!hub.history.list().isEmpty()) lastId = hub.history.list().get(0).id;
        if (lastId != null) {
            Bitmap b = hub.thumbs.get(lastId);
            if (b != null) lastThumb.setImageBitmap(b);
        }
    }
}
