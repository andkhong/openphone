package com.example.shop;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.util.Log;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

public class ProductActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        Ui.quantity = 1;
        LinearLayout col = Ui.column(this);
        Ui.text(col, "USB-C cable, 2 m", 26);
        Ui.text(col, "$9.99", 30);
        LinearLayout row = new LinearLayout(this);
        TextView qty = new TextView(this);
        Button minus = new Button(this);
        minus.setText("-");
        minus.setContentDescription("Decrease quantity");
        minus.setOnClickListener(v -> { if (Ui.quantity > 1) Ui.quantity--; qty.setText("Qty " + Ui.quantity); });
        qty.setText("Qty 1");
        qty.setTextSize(22);
        qty.setPadding(40, 0, 40, 0);
        Button plus = new Button(this);
        plus.setText("+");
        plus.setContentDescription("Increase quantity");
        plus.setOnClickListener(v -> {
            Ui.quantity++;
            qty.setText("Qty " + Ui.quantity);
            Log.i("ShopStandIn", "QUANTITY " + Ui.quantity);
        });
        row.addView(minus);
        row.addView(qty);
        row.addView(plus);
        col.addView(row);
        Ui.button(col, "Add to cart", v -> { Ui.cart += Ui.quantity; Log.i("ShopStandIn", "ADDED cart=" + Ui.cart); });
        Ui.button(col, "Cart", v -> startActivity(new Intent(this, CartActivity.class)));
        Ui.button(col, "Buy now", v -> {
            Log.i("ShopStandIn", "BUY NOW qty=" + Ui.quantity);
            startActivity(new Intent(this, CheckoutActivity.class));
        });
    }
}
