package com.biobuzz.photolink;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Matrix;
import android.media.ExifInterface;
import android.util.LruCache;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;

/** Small previews of the photos taken (the originals are deleted after they reach the PC). */
public final class Thumbs {
    private final File dir;
    private final LruCache<String, Bitmap> cache = new LruCache<>(80);

    public Thumbs(File base) {
        dir = new File(base, "thumbs");
        dir.mkdirs();
    }

    public File fileFor(String id) { return new File(dir, id + ".jpg"); }

    /** Decodes the photo at about 1/8 size, upright, and stores it as a ~320 px JPEG. */
    public void create(File src, String id) {
        try {
            Bitmap b = decode(src, 320);
            if (b == null) return;
            try (FileOutputStream o = new FileOutputStream(fileFor(id))) {
                b.compress(Bitmap.CompressFormat.JPEG, 80, o);
            }
            cache.put(id, b);
        } catch (IOException ignored) {
        }
        prune();
    }

    public Bitmap get(String id) {
        Bitmap b = cache.get(id);
        if (b != null) return b;
        File f = fileFor(id);
        if (!f.exists()) return null;
        b = BitmapFactory.decodeFile(f.getPath());
        if (b != null) cache.put(id, b);
        return b;
    }

    /** Larger preview straight from a photo that is still on the phone. */
    public static Bitmap decode(File src, int maxSide) {
        BitmapFactory.Options o = new BitmapFactory.Options();
        o.inJustDecodeBounds = true;
        BitmapFactory.decodeFile(src.getPath(), o);
        int s = 1;
        while (Math.max(o.outWidth, o.outHeight) / (s * 2) >= maxSide) s *= 2;
        o = new BitmapFactory.Options();
        o.inSampleSize = s;
        Bitmap b = BitmapFactory.decodeFile(src.getPath(), o);
        if (b == null) return null;
        try {
            int rot;
            switch (new ExifInterface(src.getPath()).getAttributeInt(ExifInterface.TAG_ORIENTATION, 1)) {
                case ExifInterface.ORIENTATION_ROTATE_90: rot = 90; break;
                case ExifInterface.ORIENTATION_ROTATE_180: rot = 180; break;
                case ExifInterface.ORIENTATION_ROTATE_270: rot = 270; break;
                default: rot = 0;
            }
            if (rot != 0) {
                Matrix m = new Matrix();
                m.postRotate(rot);
                b = Bitmap.createBitmap(b, 0, 0, b.getWidth(), b.getHeight(), m, true);
            }
        } catch (IOException ignored) {
        }
        return b;
    }

    private void prune() {
        File[] files = dir.listFiles();
        if (files == null || files.length <= 400) return;
        java.util.Arrays.sort(files, (a, b) -> Long.compare(a.lastModified(), b.lastModified()));
        for (int i = 0; i < files.length - 300; i++) files[i].delete();
    }
}
