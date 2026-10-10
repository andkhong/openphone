package com.example.shop;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.LinearLayout;

public class CartActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout col = Ui.column(this);
        Ui.text(col, "Your cart", 26);
        Ui.text(col, "USB-C cable, 2 m  x" + Math.max(Ui.cart, 1), 20);
        Ui.button(col, "Proceed to checkout", v -> startActivity(new Intent(this, CheckoutActivity.class)));
    }
}
