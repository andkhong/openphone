package com.example.login;

import android.app.Activity;
import android.os.Bundle;
import android.text.InputType;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

public class MainActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(40, 160, 40, 40);
        TextView title = new TextView(this);
        title.setText("Sign in to Example");
        title.setTextSize(26);
        EditText user = new EditText(this);
        user.setHint("Email");
        user.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS);
        user.setAutofillHints(View.AUTOFILL_HINT_EMAIL_ADDRESS);
        EditText pass = new EditText(this);
        pass.setHint("Password");
        pass.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        pass.setAutofillHints(View.AUTOFILL_HINT_PASSWORD);
        pass.addTextChangedListener(new android.text.TextWatcher() {
            public void beforeTextChanged(CharSequence s, int a, int b, int c) {}
            public void onTextChanged(CharSequence s, int a, int b, int c) {}
            public void afterTextChanged(android.text.Editable s) { Log.i("LoginStandIn", "PASSWORD length=" + s.length()); }
        });
        Button signIn = new Button(this);
        signIn.setText("Sign in");
        signIn.setOnClickListener(v -> Log.i("LoginStandIn", "SIGN IN"));
        LinearLayout.LayoutParams row = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 170);
        col.addView(title);
        col.addView(user, row);
        col.addView(pass, row);
        col.addView(signIn, row);
        setContentView(col);
    }
}
