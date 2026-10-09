package com.example.login;

import android.app.Activity;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** A screen whose window is hidden from accessibility services (ACCESSIBILITY_DATA_SENSITIVE_YES on the decor view,
 *  Android 14+: what the permission controller does), so OpenMob sees an application window with no package and no
 *  tree: the gatekeeper's `builtin:unknown_window`. Not a permission dialog: its text never says "Allow X to …".
 *  Every touch and the button are logged (tag LoginStandIn, "HIDDEN TOUCH" / "HIDDEN TAP"). */
public class HiddenActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(40, 400, 40, 40);
        TextView title = new TextView(this);
        title.setText("Private screen");
        title.setTextSize(26);
        TextView body = new TextView(this);
        body.setText("This window is hidden from accessibility services.");
        Button confirm = new Button(this);
        confirm.setText("Confirm transfer");
        confirm.setOnClickListener(v -> Log.i("LoginStandIn", "HIDDEN TAP"));
        LinearLayout.LayoutParams row = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 170);
        col.addView(title);
        col.addView(body);
        col.addView(confirm, row);
        setContentView(col);
        if (Build.VERSION.SDK_INT >= 34) {
            getWindow().getDecorView().setAccessibilityDataSensitive(View.ACCESSIBILITY_DATA_SENSITIVE_YES);
        }
    }

    @Override public boolean dispatchTouchEvent(android.view.MotionEvent e) {
        if (e.getActionMasked() == android.view.MotionEvent.ACTION_DOWN) {
            Log.i("LoginStandIn", "HIDDEN TOUCH " + (int) e.getX() + "," + (int) e.getY());
        }
        return super.dispatchTouchEvent(e);
    }
}
