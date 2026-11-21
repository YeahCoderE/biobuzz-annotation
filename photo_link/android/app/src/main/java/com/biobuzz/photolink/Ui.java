package com.biobuzz.photolink;

import android.content.Context;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;

/** Tiny helpers to build the Status and Settings pages in code. */
final class Ui {
    private Ui() {}

    static int dp(Context c, int v) {
        return Math.round(v * c.getResources().getDisplayMetrics().density);
    }

    static LinearLayout page(Context c, ScrollView[] outScroll) {
        ScrollView sv = new ScrollView(c);
        sv.setBackgroundColor(0xFF14171C);
        sv.setFillViewport(true);
        LinearLayout col = new LinearLayout(c);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(dp(c, 14), dp(c, 14), dp(c, 14), dp(c, 24));
        sv.addView(col, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        outScroll[0] = sv;
        return col;
    }

    static TextView title(Context c, String t) {
        TextView v = new TextView(c);
        v.setText(t);
        v.setTextColor(0xFFF5A524);
        v.setTextSize(22);
        v.setTypeface(null, android.graphics.Typeface.BOLD);
        v.setPadding(dp(c, 4), dp(c, 6), 0, dp(c, 10));
        return v;
    }

    static LinearLayout card(Context c, LinearLayout parent, String heading) {
        LinearLayout card = new LinearLayout(c);
        card.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(0xFF1D2127);
        bg.setCornerRadius(dp(c, 14));
        card.setBackground(bg);
        card.setPadding(dp(c, 16), dp(c, 14), dp(c, 16), dp(c, 14));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = dp(c, 12);
        parent.addView(card, lp);
        if (heading != null) {
            TextView h = new TextView(c);
            h.setText(heading);
            h.setTextColor(0xFFFFCF70);
            h.setTextSize(15);
            h.setTypeface(null, android.graphics.Typeface.BOLD);
            h.setPadding(0, 0, 0, dp(c, 8));
            card.addView(h);
        }
        return card;
    }

    static TextView text(Context c, LinearLayout parent, String t, int color, int sp) {
        TextView v = new TextView(c);
        v.setText(t);
        v.setTextColor(color);
        v.setTextSize(sp);
        parent.addView(v);
        return v;
    }

    /** "label ........ value" row; returns the value view. */
    static TextView row(Context c, LinearLayout parent, String label) {
        LinearLayout r = new LinearLayout(c);
        r.setOrientation(LinearLayout.HORIZONTAL);
        r.setPadding(0, dp(c, 4), 0, dp(c, 4));
        TextView l = new TextView(c);
        l.setText(label);
        l.setTextColor(0xFF9AA0A6);
        l.setTextSize(14);
        TextView v = new TextView(c);
        v.setTextColor(0xFFE8EAED);
        v.setTextSize(14);
        v.setGravity(Gravity.END);
        r.addView(l, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        r.addView(v, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1.4f));
        parent.addView(r);
        return v;
    }

    static Button button(Context c, LinearLayout parent, String label, View.OnClickListener l) {
        Button b = new Button(c);
        b.setText(label);
        b.setAllCaps(false);
        b.setOnClickListener(l);
        parent.addView(b, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return b;
    }

    static Switch toggle(Context c, LinearLayout parent, String label, String hint, boolean on, android.widget.CompoundButton.OnCheckedChangeListener l) {
        Switch s = new Switch(c);
        s.setText(hint == null ? label : label + "\n" + hint);
        s.setTextColor(0xFFE8EAED);
        s.setTextSize(15);
        s.setChecked(on);
        s.setOnCheckedChangeListener(l);
        s.setPadding(0, dp(c, 8), 0, dp(c, 8));
        parent.addView(s, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return s;
    }

    static void dot(TextView v, int color) {
        v.setTextColor(color == 0 ? Color.WHITE : color);
    }
}
