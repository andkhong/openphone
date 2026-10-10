package com.example.login;

import android.app.Activity;
import android.os.Bundle;
import android.webkit.WebView;

/** A web login form inside a WebView: the email field is focused, so a keyboard / autofill surface comes up. */
public class WebLoginActivity extends Activity {
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        WebView web = new WebView(this);
        web.getSettings().setJavaScriptEnabled(true);
        String html = "<html><body style='font-size:22px;margin:24px'><h2>Sign in</h2><form>"
            + "<input id='email' type='email' autocomplete='username' placeholder='Email' style='font-size:22px;width:90%'><br><br>"
            + "<input id='pw' type='password' autocomplete='current-password' placeholder='Password' style='font-size:22px;width:90%'><br><br>"
            + "<button type='submit' style='font-size:22px'>Sign in</button></form>"
            + "<script>setTimeout(function(){document.getElementById('email').focus()},300)</script></body></html>";
        web.loadDataWithBaseURL("https://login.example.com/", html, "text/html", "utf-8", null);
        setContentView(web);
        web.requestFocus();
    }
}
