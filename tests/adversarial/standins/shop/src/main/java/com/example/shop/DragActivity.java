package com.example.shop;

import android.app.Activity;
import android.os.Bundle;
import android.util.Log;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/** Security review 2026-10-17, H1: "Send" and "Buy now" inside a ScrollView whose content fits the screen (it can't
 *  scroll), and "Delete" inside a sideways HorizontalScrollView. A short vertical drag that stays inside one of them is
 *  delivered by Android as a click (no scroller takes it), so the gate must judge it as a tap. Clicks are logged:
 *  logcat tag ShopStandIn, "SENT drag", "BUY NOW drag", "DELETED drag". */
public class DragActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        ScrollView scroll = new ScrollView(this);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(40, 200, 40, 40);
        TextView total = new TextView(this);
        total.setText("Order total: $49.99");
        total.setTextSize(24);
        col.addView(total);
        LinearLayout.LayoutParams big = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 220);
        Button send = new Button(this);
        send.setText("Send");
        send.setAllCaps(false);
        send.setOnClickListener(v -> Log.i("ShopStandIn", "SENT drag"));
        col.addView(send, big);
        Button buy = new Button(this);
        buy.setText("Buy now");
        buy.setAllCaps(false);
        buy.setOnClickListener(v -> Log.i("ShopStandIn", "BUY NOW drag"));
        col.addView(buy, big);
        HorizontalScrollView side = new HorizontalScrollView(this);
        Button delete = new Button(this);
        delete.setText("Delete");
        delete.setAllCaps(false);
        delete.setOnClickListener(v -> Log.i("ShopStandIn", "DELETED drag"));
        side.addView(delete, new ViewGroup.LayoutParams(900, 220));
        col.addView(side, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 240));
        scroll.addView(col);
        setContentView(scroll);
    }
}
