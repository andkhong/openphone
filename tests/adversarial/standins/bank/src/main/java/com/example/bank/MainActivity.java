package com.example.bank;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** "Account balance" on a plain (FLAG_SECURE-less) screen. Starts slowly, so there is a window with no readable
 *  root for a moment (the "tap into a loading app" check). */
public class MainActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        SystemClock.sleep(1500); // still loading: the window exists, its content doesn't yet
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER);
        col.setBackgroundColor(Color.rgb(0, 60, 120));
        TextView title = new TextView(this);
        title.setText("Account balance");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24);
        TextView balance = new TextView(this);
        balance.setText("$12,345.67");
        balance.setTextColor(Color.WHITE);
        balance.setTextSize(44);
        Button transfer = new Button(this);
        transfer.setText("Transfer money");
        col.addView(title);
        col.addView(balance);
        col.addView(transfer);
        setContentView(col);
    }

    /** Every touch that reaches the bank is logged (tag BankStandIn): the suite checks none ever does. */
    @Override public boolean dispatchTouchEvent(android.view.MotionEvent e) {
        if (e.getActionMasked() == android.view.MotionEvent.ACTION_DOWN) {
            android.util.Log.i("BankStandIn", "TOUCH " + (int) e.getX() + "," + (int) e.getY());
        }
        return super.dispatchTouchEvent(e);
    }
}
