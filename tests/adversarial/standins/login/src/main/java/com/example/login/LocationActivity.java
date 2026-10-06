package com.example.login;

import android.Manifest;
import android.app.Activity;
import android.os.Bundle;
import android.widget.Button;
import android.widget.LinearLayout;

/** Runtime permission dialogs: location while in use (Precise / Approximate), then in the background
 *  ("Allow all the time"). */
public class LocationActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(40, 160, 40, 40);
        LinearLayout.LayoutParams row = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 170);
        Button fg = new Button(this);
        fg.setText("Use my location");
        fg.setOnClickListener(v -> requestPermissions(new String[]{
            Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION}, 1));
        Button bg = new Button(this);
        bg.setText("Use location in background");
        bg.setOnClickListener(v -> requestPermissions(new String[]{Manifest.permission.ACCESS_BACKGROUND_LOCATION}, 2));
        col.addView(fg, row);
        col.addView(bg, row);
        setContentView(col);
    }
}
