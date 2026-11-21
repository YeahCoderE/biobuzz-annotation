package com.biobuzz.photolink;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.util.AttributeSet;
import android.view.View;

/** Rule-of-thirds lines over the camera preview. */
public class GridOverlay extends View {
    private final Paint paint = new Paint();

    public GridOverlay(Context c, AttributeSet a) {
        super(c, a);
        paint.setColor(0x88FFFFFF);
        paint.setStrokeWidth(2f);
    }

    @Override protected void onDraw(Canvas c) {
        float w = getWidth(), h = getHeight();
        for (int i = 1; i <= 2; i++) {
            c.drawLine(w * i / 3f, 0, w * i / 3f, h, paint);
            c.drawLine(0, h * i / 3f, w, h * i / 3f, paint);
        }
    }
}
