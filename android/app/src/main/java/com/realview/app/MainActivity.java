package com.realview.app;

import android.Manifest;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.util.Base64;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

/**
 * Real View app: opens the Real View website full screen and adds what a browser cannot do,
 * mainly printing bills straight to a Bluetooth thermal printer (window.RVApp.printEscpos).
 * Everything else (screens, logins, data) is the website itself, so website updates need no new app.
 */
public class MainActivity extends Activity {
    private static final int REQ_FILE = 1;
    private static final int REQ_PERM_BT = 2;
    private static final int REQ_PERM_CAM = 3;
    private static final int REQ_PERM_STORAGE = 4;

    private WebView web;
    private ValueCallback<Uri[]> fileCallback;
    private PermissionRequest pendingCamera;
    private Runnable afterBluetoothPermission;
    private Printer printer;
    private String homeHost;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        homeHost = Uri.parse(BuildConfig.HOME_URL).getHost();
        printer = new Printer(this);

        web = new WebView(this);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);   // the online-order bell can ring
        s.setAllowFileAccess(false);
        s.setUserAgentString(s.getUserAgentString() + " RealViewApp/" + BuildConfig.VERSION_NAME);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(web, true);

        web.addJavascriptInterface(new Bridge(), "RVApp");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return openOutside(request.getUrl());
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), REQ_FILE);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }

            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> {
                    if (hasPermission(Manifest.permission.CAMERA)) {
                        request.grant(request.getResources());
                    } else {
                        pendingCamera = request;
                        requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_PERM_CAM);
                    }
                });
            }
        });

        web.setDownloadListener((url, userAgent, contentDisposition, mimeType, length) ->
                download(url, userAgent, contentDisposition, mimeType));

        if (Build.VERSION.SDK_INT <= 28 && !hasPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)) {
            requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_PERM_STORAGE);
        }

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(BuildConfig.HOME_URL);
        }
    }

    /** Our own pages open inside the app; WhatsApp, phone calls, maps, other sites open in their own apps. */
    private boolean openOutside(Uri uri) {
        String scheme = uri.getScheme() == null ? "" : uri.getScheme();
        if ((scheme.equals("https") || scheme.equals("http")) && homeHost.equalsIgnoreCase(uri.getHost())) {
            return false;
        }
        try {
            if (scheme.equals("intent")) {
                Intent intent = Intent.parseUri(uri.toString(), Intent.URI_INTENT_SCHEME);
                try {
                    startActivity(intent);
                } catch (ActivityNotFoundException e) {
                    String fallback = intent.getStringExtra("browser_fallback_url");
                    if (fallback != null) startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(fallback)));
                    else toast("The app for this link is not installed.");
                }
            } else {
                startActivity(new Intent(Intent.ACTION_VIEW, uri));
            }
        } catch (Exception e) {
            toast("Could not open this link.");
        }
        return true;
    }

    /** Files from the website (bill PDF, Excel) go to the phone's Downloads folder. */
    private void download(String url, String userAgent, String contentDisposition, String mimeType) {
        try {
            String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
            DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null) r.addRequestHeader("Cookie", cookie);
            r.addRequestHeader("User-Agent", userAgent);
            r.setMimeType(mimeType);
            r.setTitle(name);
            r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            r.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            DownloadManager dm = (DownloadManager) getSystemService(DOWNLOAD_SERVICE);
            dm.enqueue(r);
            toast("Downloading " + name);
        } catch (Exception e) {
            toast("Download failed: " + e.getMessage());
        }
    }

    /** What the website can call: window.RVApp.printEscpos(base64), RVApp.choosePrinter(), RVApp.printerName(). */
    private class Bridge {
        @JavascriptInterface
        public String printEscpos(String base64) {
            final byte[] data;
            try {
                data = Base64.decode(base64, Base64.DEFAULT);
            } catch (IllegalArgumentException e) {
                return "bad print data";
            }
            runOnUiThread(() -> withBluetooth(() -> printer.print(data)));
            return "printing";
        }

        @JavascriptInterface
        public void choosePrinter() {
            runOnUiThread(() -> withBluetooth(() -> printer.choose(null)));
        }

        @JavascriptInterface
        public String printerName() {
            return printer.savedName();
        }

        @JavascriptInterface
        public String version() {
            return BuildConfig.VERSION_NAME;
        }
    }

    private void withBluetooth(Runnable then) {
        if (Build.VERSION.SDK_INT >= 31 && !hasPermission(Manifest.permission.BLUETOOTH_CONNECT)) {
            afterBluetoothPermission = then;
            requestPermissions(new String[]{Manifest.permission.BLUETOOTH_CONNECT}, REQ_PERM_BT);
        } else {
            then.run();
        }
    }

    private boolean hasPermission(String permission) {
        return checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED;
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        boolean granted = results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED;
        if (requestCode == REQ_PERM_BT) {
            Runnable then = afterBluetoothPermission;
            afterBluetoothPermission = null;
            if (granted && then != null) then.run();
            else if (!granted) toast("Allow 'Nearby devices' so the app can use the Bluetooth printer.");
        } else if (requestCode == REQ_PERM_CAM && pendingCamera != null) {
            if (granted) pendingCamera.grant(pendingCamera.getResources());
            else pendingCamera.deny();
            pendingCamera = null;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_FILE && fileCallback != null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            fileCallback = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();   // stay logged in
    }

    void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_LONG).show();
    }
}
