package org.husk.fixture;

import android.app.Activity;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.text.Editable;
import android.text.TextWatcher;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Small real Java APK: framework rendering/input, live WebKit and clean finish. */
public final class MainActivity extends Activity {
    private static final String TAG = "HuskFixture";
    private final Handler handler = new Handler();
    private TextView status;
    private EditText editor;
    private WebView web;
    private int clicks;
    private int ticks;
    private boolean destroyed;
    private final Runnable ticker = new Runnable() {
        @Override public void run() {
            if (destroyed) return;
            ticks++;
            updateStatus();
            Log.i(TAG, "HUSK_FIXTURE tick=" + ticks + " clicks=" + clicks);
            // Intentionally keep running when the desktop window loses focus.
            handler.postDelayed(this, 1000);
        }
    };

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void updateStatus() {
        status.setText("JAVA clicks=" + clicks + "  background ticks=" + ticks);
    }

    private LinearLayout.LayoutParams row(int height) {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(height));
    }

    @Override protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        Log.i(TAG, "HUSK_FIXTURE onCreate");
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(12), dp(8), dp(12), dp(8));
        root.setBackgroundColor(Color.rgb(238, 242, 247));

        TextView title = new TextView(this);
        title.setText("HUSK JAVA + NATIVE WEBVIEW");
        title.setTextColor(Color.rgb(20, 35, 55));
        title.setTextSize(18);
        root.addView(title, row(32));

        status = new TextView(this);
        status.setTextColor(Color.BLACK);
        status.setTextSize(15);
        updateStatus();
        root.addView(status, row(32));

        Button count = new Button(this);
        count.setId(1001);
        count.setText("JAVA BUTTON: increment");
        count.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View ignored) {
                clicks++;
                updateStatus();
                Log.i(TAG, "HUSK_FIXTURE java_click=" + clicks);
            }
        });
        root.addView(count, row(48));

        editor = new EditText(this);
        editor.setId(1002);
        editor.setSingleLine(true);
        editor.setHint("JAVA INPUT: type here");
        editor.setTextColor(Color.BLACK);
        editor.setBackgroundColor(Color.WHITE);
        editor.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int start, int count, int after) {}
            @Override public void onTextChanged(CharSequence s, int start, int before, int count) {
                Log.i(TAG, "HUSK_FIXTURE java_text=" + s);
            }
            @Override public void afterTextChanged(Editable text) {}
        });
        root.addView(editor, row(48));

        Button finish = new Button(this);
        finish.setId(1003);
        finish.setText("FINISH CLEANLY (onDestroy + WebKit cleanup)");
        finish.setTextSize(12);
        finish.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View ignored) {
                Log.i(TAG, "HUSK_FIXTURE finish_requested");
                finish();
            }
        });
        root.addView(finish, row(48));

        web = new WebView(this);
        web.setId(1004);
        web.getSettings().setJavaScriptEnabled(true);
        web.setWebViewClient(new WebViewClient() {
            @Override public void onPageFinished(WebView view, String url) {
                Log.i(TAG, "HUSK_FIXTURE web_ready=" + url);
            }
        });
        root.addView(web, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        setContentView(root);
        web.loadDataWithBaseURL("https://husk-fixture.invalid/",
            "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
            + "<style>html,body{margin:0;min-height:100%;background:#19bd73;color:#102a23;font:16px sans-serif}"
            + "main{padding:16px}button,input{box-sizing:border-box;width:100%;padding:12px;margin:6px 0;font:16px sans-serif}"
            + "h2{font-size:18px;margin:4px 0 10px}</style></head>"
            + "<body><main><h2>NATIVE WKWEBVIEW - GREEN PANEL</h2>"
            + "<button id='web-button' onclick=\"window.n=(window.n||0)+1;document.getElementById('web-count').textContent='WEB clicks='+window.n;document.body.style.background='#35c5e5'\">WEB BUTTON: native hit test</button>"
            + "<p id='web-count'>WEB clicks=0</p>"
            + "<input id='web-input' placeholder='WEB INPUT: type here' oninput=\"document.getElementById('web-text').textContent='WEB text='+this.value\">"
            + "<p id='web-text'>WEB text=</p><p>This panel must appear in a real window screenshot.</p>"
            + "</main></body></html>", "text/html", "utf-8", null);
        handler.postDelayed(ticker, 1000);
    }

    @Override protected void onPause() {
        Log.i(TAG, "HUSK_FIXTURE onPause ticks=" + ticks);
        super.onPause();
    }

    @Override protected void onDestroy() {
        destroyed = true;
        handler.removeCallbacks(ticker);
        Log.i(TAG, "HUSK_FIXTURE onDestroy ticks=" + ticks + " clicks=" + clicks);
        if (web != null) web.destroy();
        super.onDestroy();
        Log.i(TAG, "HUSK_FIXTURE onDestroy_complete");
    }
}
