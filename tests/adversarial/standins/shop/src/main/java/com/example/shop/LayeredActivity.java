package com.example.shop;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/** The gate's hit test (2026-10-10): "Buy now" sits in an EARLIER sibling than a full-screen layer, but is drawn (and
 *  touched) above it by elevation, like Google Phone's Call button. The accessibility tree lists the layer last, so a
 *  gate that follows only the last child under the point never sees the button. */
public class LayeredActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.WHITE);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(40, 300, 40, 40);
        TextView title = new TextView(this);
        title.setText("Wireless earbuds  $49.99");
        title.setTextSize(26);
        col.addView(title);
        Button buy = new Button(this);
        buy.setText("Buy now");
        buy.setOnClickListener(v -> {
            Log.i("ShopStandIn", "BUY NOW layered");
            startActivity(new Intent(this, CheckoutActivity.class));
        });
        col.addView(buy, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 200));
        col.setElevation(20f);
        root.addView(col, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        // The later, full-screen layer: transparent, not clickable, listed after the content.
        FrameLayout layer = new FrameLayout(this);
        layer.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);
        root.addView(layer, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);
    }
}
