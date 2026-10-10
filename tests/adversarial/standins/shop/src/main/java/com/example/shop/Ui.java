package com.example.shop;

import android.app.Activity;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Tiny layout helpers: one vertical column of big rows. */
final class Ui {
    static int cart = 0;      // items in the cart
    static int quantity = 1;  // the product page's quantity

    static LinearLayout column(Activity a) {
        LinearLayout col = new LinearLayout(a);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER_HORIZONTAL);
        col.setPadding(40, 160, 40, 40);
        a.setContentView(col);
        return col;
    }

    static TextView text(LinearLayout col, String s, int size) {
        TextView t = new TextView(col.getContext());
        t.setText(s);
        t.setTextSize(size);
        t.setPadding(0, 20, 0, 20);
        col.addView(t);
        return t;
    }

    static Button button(LinearLayout col, String s, View.OnClickListener click) {
        Button b = new Button(col.getContext());
        b.setText(s);
        b.setAllCaps(false);
        b.setTextSize(20);
        b.setOnClickListener(click);
        col.addView(b, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 170));
        return b;
    }

    static void ordered(String where) {
        Log.i("ShopStandIn", "ORDER PLACED via " + where + " qty=" + quantity + " cart=" + cart);
        cart = 0;
    }
}
