package com.example.shop;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.util.Log;
import android.view.Gravity;
import android.widget.Button;
import android.widget.FrameLayout;

/** The agent indicator's Stop pill (2026-10-17): a top-right "Menu" button right where the pill sits by default, like an
 *  app toolbar's overflow button. An agent's tap on it must move the pill out of the way and reach the button. */
public class ToolbarActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.WHITE);
        Button menu = new Button(this);
        menu.setText("Menu");
        menu.setContentDescription("More options");
        menu.setOnClickListener(v -> Log.i("ShopStandIn", "MENU tapped"));
        root.addView(menu, new FrameLayout.LayoutParams(440, 320, Gravity.TOP | Gravity.END));
        setContentView(root);
    }
}
