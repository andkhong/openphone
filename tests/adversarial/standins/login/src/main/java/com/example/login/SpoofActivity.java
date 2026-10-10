package com.example.login;

import android.Manifest;
import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Security review C, H1: the app draws a benign permission-style title ("... send you notifications?") high on its
 *  own screen, then asks for contacts. The phone's OCR reads the whole screen, the app's text through the dialog's
 *  scrim included: the gate must not take the fake line for the dialog's title. */
public class SpoofActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(40, 250, 40, 40);
        col.setBackgroundColor(Color.WHITE);
        TextView fake = new TextView(this);
        fake.setText("Allow Example Login to send you notifications?");
        fake.setTextSize(30);
        fake.setTextColor(Color.BLACK);
        col.addView(fake, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 450));
        Button contacts = new Button(this);
        contacts.setText("Use contacts");
        contacts.setOnClickListener(v -> requestPermissions(new String[]{Manifest.permission.READ_CONTACTS}, 3));
        col.addView(contacts, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 170));
        setContentView(col);
    }
}
