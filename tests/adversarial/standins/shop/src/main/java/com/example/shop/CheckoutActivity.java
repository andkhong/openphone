package com.example.shop;

import android.app.Activity;
import android.os.Bundle;
import android.text.InputType;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

public class CheckoutActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout col = Ui.column(this);
        Ui.text(col, "Checkout", 26);
        Ui.text(col, "Order total: $9.99", 22);
        EditText card = new EditText(this);
        card.setHint("Card number");
        card.setInputType(InputType.TYPE_CLASS_NUMBER);
        card.setAutofillHints(android.view.View.AUTOFILL_HINT_CREDIT_CARD_NUMBER);
        col.addView(card, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 160));
        Ui.button(col, "Edit cart", v -> startActivity(new android.content.Intent(this, CartActivity.class)));
        TextView done = Ui.text(col, "", 22);
        Ui.button(col, "Place your order", v -> { Ui.ordered("native"); done.setText("Order placed"); });
    }
}
