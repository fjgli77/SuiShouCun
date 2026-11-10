package com.suishoucun.app;

/* 随手存 · 下载前台服务（保活 + 通知栏进度）。
 *
 * 为什么需要它：下载线程跑在 MainActivity 的线程池里。App 退到后台后，进程优先级下降，
 * 系统可能在内存紧张时直接回收，导致「以为在下载、其实早被杀」。Android 的规则是
 * ——只要进程里有一个正在运行的前台服务（foreground service），系统就不会轻易回收它。
 * 因此这里用一个「守护型」前台服务：下载期间常驻，通知栏显示总体进度与「取消」入口。
 *
 * 设计要点：
 * - 服务本身不搬字节，只负责「宣告前台 + 通知控制」；真正的下载仍在 MainActivity 的池里，
 *   两者同进程，故前台服务足以保护下载线程不被回收。
 * - 取消入口不直接持有 Activity 引用（避免内存泄漏）：通知上的「取消」把静态标志
 *   cancelAllRequested 置位，下载循环每轮同时检查该标志，效果等同于用户点界面上的取消。
 * - Android 13+ 通知需运行时授权；未授权也不影响前台服务运行，只是通知不可见。
 */

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

import java.util.concurrent.atomic.AtomicBoolean;

public class DownloadService extends Service {

    public static final String ACTION_START = "com.suishoucun.app.dl.START";
    public static final String ACTION_UPDATE = "com.suishoucun.app.dl.UPDATE";
    public static final String ACTION_STOP = "com.suishoucun.app.dl.STOP";
    public static final String ACTION_CANCEL_ALL = "com.suishoucun.app.dl.CANCEL_ALL";

    public static final String EXTRA_DONE = "done";
    public static final String EXTRA_TOTAL = "total";

    private static final String CHANNEL_ID = "suishoucun_download";
    private static final int NOTIFY_ID = 0x5101;

    /** 通知栏「取消」触发的全局取消信号：下载循环每轮检查它（与界面取消等效）。 */
    public static final AtomicBoolean cancelAllRequested = new AtomicBoolean(false);

    private NotificationManager nm;
    private volatile boolean foreground = false;

    @Override
    public void onCreate() {
        super.onCreate();
        nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        ensureChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_CANCEL_ALL.equals(action)) {
            cancelAllRequested.set(true);
            return START_NOT_STICKY;
        }
        if (ACTION_STOP.equals(action)) {
            stopForegroundCompat();
            stopSelf();
            return START_NOT_STICKY;
        }
        int done = intent == null ? 0 : intent.getIntExtra(EXTRA_DONE, 0);
        int total = intent == null ? 0 : intent.getIntExtra(EXTRA_TOTAL, 0);
        // 每次 START / UPDATE 都发一次通知；首次同时进入前台状态
        Notification n = buildNotification(done, total);
        if (!foreground) {
            startForegroundCompat(n);
            foreground = true;
        } else {
            try { nm.notify(NOTIFY_ID, n); } catch (Exception ignored) { }
        }
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        foreground = false;
        super.onDestroy();
    }

    // ------------------------------------------------------------------ //
    private void ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O || nm == null) return;
        if (nm.getNotificationChannel(CHANNEL_ID) != null) return;
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, "下载任务", NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("随手存正在保存文件时的进度提示");
        ch.setShowBadge(false);
        nm.createNotificationChannel(ch);
    }

    private Notification buildNotification(int done, int total) {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) piFlags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent contentPi = PendingIntent.getActivity(this, 0, open, piFlags);

        Intent cancel = new Intent(this, DownloadService.class);
        cancel.setAction(ACTION_CANCEL_ALL);
        PendingIntent cancelPi = PendingIntent.getService(this, 1, cancel, piFlags);

        String title = "随手存 · 正在保存";
        String text = total > 1 ? ("正在保存 " + done + "/" + total + " 个文件")
                                : "正在保存到手机（DCIM/随手存）";

        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            b = new Notification.Builder(this, CHANNEL_ID);
        } else {
            b = new Notification.Builder(this);
        }
        b.setContentTitle(title)
         .setContentText(text)
         .setSmallIcon(android.R.drawable.stat_sys_download)
         .setOngoing(true)
         .setOnlyAlertOnce(true)
         .setContentIntent(contentPi)
         .addAction(android.R.drawable.ic_menu_close_clear_cancel, "取消", cancelPi);
        if (total > 0) {
            b.setProgress(total, done, total <= 0);
        } else {
            b.setProgress(0, 0, true);
        }
        return b.build();
    }

    @SuppressWarnings("deprecation")
    private void startForegroundCompat(Notification n) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIFY_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            } else {
                startForeground(NOTIFY_ID, n);
            }
        } catch (Exception e) {
            // 极端情况下（如系统限制）前台化失败，也不能让下载流程崩掉
            try { startForeground(NOTIFY_ID, n); } catch (Exception ignored) { }
        }
    }

    @SuppressWarnings("deprecation")
    private void stopForegroundCompat() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                stopForeground(STOP_FOREGROUND_REMOVE);
            } else {
                stopForeground(true);
            }
        } catch (Exception ignored) { }
    }

    // ------------------------------------------------------------------ //
    // 供 MainActivity 调用的静态便捷入口
    // ------------------------------------------------------------------ //
    public static void start(Context ctx, int done, int total) {
        try {
            Intent i = new Intent(ctx, DownloadService.class);
            i.setAction(ACTION_START);
            i.putExtra(EXTRA_DONE, done);
            i.putExtra(EXTRA_TOTAL, total);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i);
            else ctx.startService(i);
        } catch (Exception ignored) { }
    }

    public static void update(Context ctx, int done, int total) {
        try {
            Intent i = new Intent(ctx, DownloadService.class);
            i.setAction(ACTION_UPDATE);
            i.putExtra(EXTRA_DONE, done);
            i.putExtra(EXTRA_TOTAL, total);
            ctx.startService(i);
        } catch (Exception ignored) { }
    }

    public static void stop(Context ctx) {
        try {
            Intent i = new Intent(ctx, DownloadService.class);
            i.setAction(ACTION_STOP);
            ctx.startService(i);
        } catch (Exception ignored) { }
    }
}
