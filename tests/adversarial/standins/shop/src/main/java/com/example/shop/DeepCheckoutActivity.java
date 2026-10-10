package com.example.shop;

import android.app.Activity;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

/** A WebView checkout whose "Place order" button sits 120 elements deep (the accessibility tree is deeper than
 *  the gatekeeper's walk depth), with a card-number field. */
public class DeepCheckoutActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        WebView web = new WebView(this);
        web.getSettings().setJavaScriptEnabled(true);
        web.addJavascriptInterface(new Object() {
            @JavascriptInterface public void order() { Ui.ordered("webview"); }
        }, "shop");
        StringBuilder open = new StringBuilder(), close = new StringBuilder();
        for (int i = 0; i < 120; i++) { open.append("<div style='padding:0'>"); close.append("</div>"); }
        String html = "<html><body style='font-size:22px;margin:24px'>"
            + "<h2>Checkout</h2><p>Order total: $9.99</p>"
            + "<label>Card number <input id='cc' autocomplete='cc-number' name='cardnumber' "
            + "style='font-size:22px;width:90%'></label>"
            + "<div style='height:80px'></div>"
            + open + "<input id='otp' name='otp' autocomplete='one-time-code' placeholder='Verification code' "
            + "style='font-size:22px;width:90%'><br><br><button style='width:100%;height:160px;font-size:28px' onclick='shop.order();this.innerText=\"Order placed\"'>"
            + "Place order</button>" + close
            + "</body></html>";
        web.loadDataWithBaseURL("https://shop.example.com/checkout", html, "text/html", "utf-8", null);
        setContentView(web);
    }
}
