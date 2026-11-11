package com.realview.app;

import android.annotation.SuppressLint;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.PowerManager;
import android.webkit.CookieManager;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.List;
import java.util.Map;

/**
 * Watches for new online orders while the app is closed or the phone is locked: every 15 seconds it asks the
 * website how many orders wait for confirmation (the same check the Orders page makes). When some wait, it shows a
 * loud lock-screen notification and rings again every 20 seconds until they are confirmed or rejected.
 * Android needs a small permanent notification ("watching for orders") while this runs.
 */
public class OrderWatch extends Service {
    static final String CH_ORDERS = "orders_v1";
    static final String CH_WATCH = "watch_v1";
    private static final int ID_WATCH = 1;
    private static final int ID_ORDER = 2;
    private static final long EVERY_MS = 15_000;
    private static final long RING_AGAIN_MS = 20_000;

    static volatile boolean appVisible = false;   // set by MainActivity: the page itself rings then

    private HandlerThread thread;
    private Handler handler;
    private PowerManager.WakeLock wake;
    private int fails = 0;
    private long lastRing = 0;
    private int lastLatest = 0;

    static void start(Context c) {
        prefs(c).edit().putBoolean("watch", true).apply();
        Intent i = new Intent(c, OrderWatch.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
        else c.startService(i);
    }

    static void stop(Context c) {
        prefs(c).edit().putBoolean("watch", false).apply();
        c.stopService(new Intent(c, OrderWatch.class));
    }

    static boolean wanted(Context c) {
        return prefs(c).getBoolean("watch", false);
    }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("orders", Context.MODE_PRIVATE);
    }

    static void channels(Context c) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        NotificationChannel orders = new NotificationChannel(CH_ORDERS, "New online orders",
                NotificationManager.IMPORTANCE_HIGH);
        orders.setDescription("Rings when a customer orders from the online menu");
        orders.enableVibration(true);
        orders.setVibrationPattern(new long[]{0, 600, 200, 600, 200, 600});
        orders.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        orders.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION),
                new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        nm.createNotificationChannel(orders);
        NotificationChannel watch = new NotificationChannel(CH_WATCH, "Watching for orders",
                NotificationManager.IMPORTANCE_MIN);
        watch.setDescription("The small notification shown while the app checks for orders");
        watch.setShowBadge(false);
        nm.createNotificationChannel(watch);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        channels(this);
        thread = new HandlerThread("order-watch");
        thread.start();
        handler = new Handler(thread.getLooper());
    }

    @SuppressLint("WakelockTimeout")
    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = builder(CH_WATCH)
                .setContentTitle("Real View")
                .setContentText("Watching for online orders")
                .setOngoing(true)
                .setPriority(Notification.PRIORITY_MIN)
                .build();
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(ID_WATCH, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(ID_WATCH, n);
        }
        if (wake == null) {   // keep checking while the screen is off
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "RealView:orders");
            wake.setReferenceCounted(false);
            wake.acquire();
        }
        handler.removeCallbacksAndMessages(null);
        handler.post(this::tick);
        return START_STICKY;
    }

    private void tick() {
        try {
            check();
        } catch (Exception ignored) {
        }
        handler.postDelayed(this::tick, EVERY_MS);
    }

    private void check() throws Exception {
        String home = BuildConfig.HOME_URL.replaceAll("/+$", "");
        String url = home + "/cafe/orders/pending.json";
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setInstanceFollowRedirects(false);
        c.setConnectTimeout(10_000);
        c.setReadTimeout(10_000);
        String cookie = CookieManager.getInstance().getCookie(home);
        if (cookie != null) c.setRequestProperty("Cookie", cookie);
        c.setRequestProperty("Accept", "application/json");
        c.setRequestProperty("User-Agent", "RealViewApp/" + BuildConfig.VERSION_NAME + " (order watch)");
        int code = c.getResponseCode();
        keepCookies(home, c.getHeaderFields());   // the login stays fresh
        if (code != 200) {
            c.disconnect();
            if (code == 301 || code == 302 || code == 401 || code == 403) notLoggedIn();
            return;
        }
        StringBuilder sb = new StringBuilder();
        try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"))) {
            String line;
            while ((line = r.readLine()) != null) sb.append(line);
        } finally {
            c.disconnect();
        }
        JSONObject j = new JSONObject(sb.toString());
        fails = 0;
        int count = j.optInt("count", 0);
        int latest = j.optInt("latest", 0);
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (count <= 0) {
            nm.cancel(ID_ORDER);
            lastLatest = 0;
            return;
        }
        long now = System.currentTimeMillis();
        boolean fresh = latest != lastLatest;
        if (appVisible && !fresh) return;            // the open page is ringing already
        if (!fresh && now - lastRing < RING_AGAIN_MS) return;
        lastRing = now;
        lastLatest = latest;
        Intent open = new Intent(this, MainActivity.class)
                .putExtra("open", home + "/cafe/orders")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 7, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = builder(CH_ORDERS)
                .setContentTitle("🔔 New online order" + (count > 1 ? "s (" + count + ")" : ""))
                .setContentText(count == 1 ? "1 order is waiting. Tap to confirm." : count + " orders are waiting. Tap to confirm.")
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setCategory(Notification.CATEGORY_ALARM)
                .setPriority(Notification.PRIORITY_MAX)
                .setDefaults(Notification.DEFAULT_ALL)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setNumber(count)
                .setOnlyAlertOnce(false)
                .build();
        nm.notify(ID_ORDER, n);
    }

    private void notLoggedIn() {
        if (++fails >= 20) {   // about 5 minutes signed out (or no order rights): stop until the app is opened again
            stop(this);
        }
    }

    private static void keepCookies(String home, Map<String, List<String>> headers) {
        if (headers == null) return;
        for (Map.Entry<String, List<String>> e : headers.entrySet()) {
            if (e.getKey() != null && e.getKey().equalsIgnoreCase("Set-Cookie") && e.getValue() != null) {
                for (String v : e.getValue()) CookieManager.getInstance().setCookie(home, v);
            }
        }
        CookieManager.getInstance().flush();
    }

    @SuppressWarnings("deprecation")
    private Notification.Builder builder(String channel) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, channel)
                : new Notification.Builder(this);
        return b.setSmallIcon(R.drawable.ic_stat_bell);
    }

    @Override
    public void onDestroy() {
        if (handler != null) handler.removeCallbacksAndMessages(null);
        if (thread != null) thread.quitSafely();
        if (wake != null && wake.isHeld()) wake.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    /** Starts the watch again after the phone restarts, if it was on. */
    public static class Boot extends android.content.BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent i) {
            if (wanted(c)) {
                try {
                    start(c);
                } catch (Exception ignored) {
                }
            }
        }
    }

    static Uri homeUri() {
        return Uri.parse(BuildConfig.HOME_URL);
    }
}
