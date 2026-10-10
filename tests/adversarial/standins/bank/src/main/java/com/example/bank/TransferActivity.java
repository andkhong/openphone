package com.example.bank;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.util.Log;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/** A transfer form in the bank (a blocked app): an "Amount" field and a "Confirm transfer" button. Typing into the
 *  field is logged (tag BankStandIn, "AMOUNT length=N"), as is every touch, so the suite can show that no server call
 *  ever changed the field. The keyboard stays hidden until the owner taps the field. */
public class TransferActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(40, 400, 40, 40);
        col.setBackgroundColor(Color.rgb(0, 60, 120));
        TextView title = new TextView(this);
        title.setText("Transfer money");
        title.setTextColor(Color.WHITE);
        title.setTextSize(26);
        EditText amount = new EditText(this);
        amount.setHint("Amount");
        amount.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL);
        amount.setTextColor(Color.WHITE);
        amount.setHintTextColor(Color.LTGRAY);
        amount.addTextChangedListener(new android.text.TextWatcher() {
            public void beforeTextChanged(CharSequence s, int a, int b, int c) {}
            public void onTextChanged(CharSequence s, int a, int b, int c) {}
            public void afterTextChanged(android.text.Editable s) { Log.i("BankStandIn", "AMOUNT length=" + s.length()); }
        });
        Button confirm = new Button(this);
        confirm.setText("Confirm transfer");
        confirm.setOnClickListener(v -> Log.i("BankStandIn", "TRANSFER CONFIRMED"));
        LinearLayout.LayoutParams row = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 170);
        col.addView(title);
        col.addView(amount, row);
        col.addView(confirm, row);
        setContentView(col);
    }

    @Override public boolean dispatchTouchEvent(android.view.MotionEvent e) {
        if (e.getActionMasked() == android.view.MotionEvent.ACTION_DOWN) {
            Log.i("BankStandIn", "TOUCH " + (int) e.getX() + "," + (int) e.getY());
        }
        return super.dispatchTouchEvent(e);
    }
}
