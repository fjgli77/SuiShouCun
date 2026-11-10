package com.suishoucun.app;

/* 随手存 Android 端：与 PC 端是同一个应用，各自在本机运行。
 *
 * 1. 首次启动把 assets/py（CPython 标准库、media_dl 源码、纯 Python 依赖）
 *    解压到应用私有目录；
 * 2. JNI 启动内嵌 CPython，在 127.0.0.1:8765 跑与 PC 完全相同的 Flask 服务；
 * 3. WebView 打开本机页面；前端是同一份 index.html / app.js / style.css；
 * 4. 下载不经浏览器：JS Bridge 调 Java 原生写入 Download/随手存。
 *
 * 不存在「连接电脑」：没有 UDP 发现、没有手动地址、没有客户端密钥、没有口令。
 */

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.content.res.AssetManager;
import android.graphics.Color;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.DownloadListener;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public class MainActivity extends Activity {

    private static final int PORT = 8765;
    private static final String BASE = "http://127.0.0.1:" + PORT;
    private static final String DIR_NAME = "随手存";
    private static final String START_SCRIPT =
            "import sys\n" +
            "sys.argv = ['media_dl']\n" +
            "from media_dl.android_entry import main\n" +
            "main()\n";

    // 抖音登录态：本 App 不提供登录流程，Cookie 只从移动端抖音获取。
    // 移动端抖音网页（m.douyin.com）用的是系统 WebView 的 Cookie 存储；用户若已在
    // 移动端抖音（App 内浏览器 / 手机浏览器 / 本 WebView 历史会话）登录过，会话 Cookie
    // 就在 CookieManager 里，我们直接读取即可，不引导用户在我们的界面里登录。
    private static final String DY_MOBILE_URL = "https://m.douyin.com/";
    // 移动端抖音 UA：与手机浏览器一致，保证加载的是移动端页面而不是「抖音电脑版」推广页
    private static final String DY_MOBILE_UA =
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36";

    private WebView web;
    private LinearLayout splash;
    private TextView splashText;
    private FrameLayout rootView;
    private FrameLayout loginOverlay;
    private WebView loginWeb;
    private WebSettings loginSettings;
    private TextView loginTitle;
    private final android.os.Handler handler = new android.os.Handler(android.os.Looper.getMainLooper());
    private Runnable cookiePoll;
    private String savedCookie;
    private final ExecutorService pool = Executors.newFixedThreadPool(4);
    private volatile boolean serviceFailed = false;
    // 系统分享 / VIEW 带入的链接：服务就绪前先存着，页面加载完成后一次性交给前端。
    private volatile String pendingSharedText = null;
    // 前端是否已完成首次加载（boot 完成）。只有页面就绪后才能安全执行 JS。
    private volatile boolean webReady = false;
    // 正在进行的原生下载：taskId -> 取消标志（供前端任务中心「取消」按钮使用）
    private final ConcurrentHashMap<String, AtomicBoolean> downloadCancels = new ConcurrentHashMap<>();
    // 活动下载计数：用于前台服务通知的「正在保存 N 个文件」与结束判定
    private final java.util.concurrent.atomic.AtomicInteger activeDownloads =
            new java.util.concurrent.atomic.AtomicInteger(0);

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        rootView = new FrameLayout(this);
        rootView.setBackgroundColor(0xFF0D1222);

        web = new WebView(this);
        configureWebView();
        rootView.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        splash = buildSplash();
        rootView.addView(splash, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(rootView);

        // 冷启动：把分享 / VIEW 带入的链接先攒起来，等页面就绪后再交给前端
        captureShareIntent(getIntent());

        requestNotificationPermission();

        startPythonService();
        waitForService();
    }

    /** Android 13+ 通知需运行时授权；未授权不影响下载与前台服务，只是通知不可见。 */
    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT < 33) return;
        try {
            if (checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                    == android.content.pm.PackageManager.PERMISSION_GRANTED) {
                return;
            }
            requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 0x5101);
        } catch (Exception ignored) {
        }
    }

    /** 热启动（应用已在后台，且 launchMode=singleTop）：复用现有实例并接收新分享。 */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        captureShareIntent(intent);
    }

    /**
     * 从 ACTION_SEND（分享文本）或 ACTION_VIEW（打开链接）中取出链接文本。
     * 取到后暂存；若页面已就绪则立即注入，否则等 WebView 加载完成后再注入。
     */
    private void captureShareIntent(Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        String text = null;
        if (Intent.ACTION_SEND.equals(action)) {
            text = intent.getStringExtra(Intent.EXTRA_TEXT);
            if (text == null) text = intent.getStringExtra(Intent.EXTRA_SUBJECT);
        } else if (Intent.ACTION_VIEW.equals(action) && intent.getData() != null) {
            text = intent.getData().toString();
        }
        if (text == null || text.trim().isEmpty()) return;
        pendingSharedText = text.trim();
        deliverSharedText();
    }

    /** 把暂存的分享文本注入前端；页面未就绪时静默等待 onPageFinished。 */
    private void deliverSharedText() {
        final String text = pendingSharedText;
        if (text == null || !webReady) return;
        pendingSharedText = null;
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                try {
                    String js = "window.onSharedLink && window.onSharedLink(" + JSONObject.quote(text) + ");";
                    web.evaluateJavascript(js, null);
                } catch (Throwable ignored) {
                    // 页面尚未定义钩子时忽略，下次 onPageFinished 会再尝试
                }
            }
        });
    }

    // ------------------------------------------------------------------ //
    // WebView
    // ------------------------------------------------------------------ //
    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setUserAgentString(s.getUserAgentString() + " MediaDLAndroid/6.0");
        web.setBackgroundColor(0xFF0D1222);
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                // 页面（含 app.js 的钩子）就绪后，把冷启动时攒下的分享链接交给前端
                if (url != null && url.startsWith(BASE)) {
                    webReady = true;
                    deliverSharedText();
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    showError("页面加载失败：" + error.getDescription());
                }
            }
        });
        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent,
                                        String contentDisposition, String mimeType,
                                        long contentLength) {
                startNativeDownload(url, "media");
            }
        });
        web.addJavascriptInterface(new JsBridge(), "MediaDL");
    }

    private LinearLayout buildSplash() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setBackgroundColor(0xFF0D1222);
        box.setPadding(32, 24, 32, 24);

        // 应用图标（与桌面图标一致），软启动时视觉连续
        android.widget.ImageView logo = new android.widget.ImageView(this);
        try {
            logo.setImageResource(getApplicationInfo().icon);
        } catch (Exception ignored) { }
        LinearLayout.LayoutParams ilp = new LinearLayout.LayoutParams(dp(84), dp(84));
        logo.setLayoutParams(ilp);
        box.addView(logo);

        TextView title = new TextView(this);
        title.setText("随手存");
        title.setTextColor(Color.WHITE);
        title.setTextSize(26f);
        title.setGravity(Gravity.CENTER);
        title.setPadding(0, dp(18), 0, 0);
        box.addView(title);

        TextView sub = new TextView(this);
        sub.setText("视频 · 图文 · 一键保存到本机");
        sub.setTextColor(0xFF6D7A99);
        sub.setTextSize(13f);
        sub.setGravity(Gravity.CENTER);
        sub.setPadding(0, dp(6), 0, 0);
        box.addView(sub);

        // 不定长进度条：让用户知道「在加载」而不是卡死
        android.widget.ProgressBar bar = new android.widget.ProgressBar(
                this, null, android.R.attr.progressBarStyleHorizontal);
        bar.setIndeterminate(true);
        LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(dp(140), dp(4));
        blp.topMargin = dp(26);
        bar.setLayoutParams(blp);
        bar.getIndeterminateDrawable().setColorFilter(
                0xFF35E0B0, android.graphics.PorterDuff.Mode.SRC_IN);
        box.addView(bar);

        splashText = new TextView(this);
        splashText.setText("正在启动本机服务…\n首次运行需准备 Python 环境，请稍候");
        splashText.setTextColor(0xFF9AA7C4);
        splashText.setTextSize(14f);
        splashText.setGravity(Gravity.CENTER);
        splashText.setPadding(0, dp(18), 0, 0);
        box.addView(splashText);
        return box;
    }

    private void setSplash(final String text) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (splashText != null) splashText.setText(text);
            }
        });
    }

    private void showError(final String message) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (splash != null) splash.setVisibility(View.VISIBLE);
                if (splashText != null) {
                    splashText.setText(message + "\n\n日志："
                            + new File(getFilesDir(), "py/app.log").getAbsolutePath());
                }
            }
        });
    }

    // ------------------------------------------------------------------ //
    // 内嵌 Python 服务
    // ------------------------------------------------------------------ //
    private void startPythonService() {
        pool.execute(new Runnable() {
            @Override
            public void run() {
                try {
                    File home = prepareHome();
                    // 标准 CPython 布局：标准库解压到 home/lib/python3.13（含 lib-dynload），
                    // 这样 Py_InitializeFromConfig 才能按 PYTHONHOME 正常引导 encodings 等模块。
                    final String paths = home.getAbsolutePath() + "/lib/python3.13"
                            + ":" + home.getAbsolutePath() + "/lib/python3.13/lib-dynload"
                            + ":" + home.getAbsolutePath() + "/app"
                            + ":" + home.getAbsolutePath() + "/site"
                            + ":" + getApplicationInfo().nativeLibraryDir;
                    setSplash("正在启动本机服务…");
                    int rc = PyService.nativeStart(home.getAbsolutePath(), paths, START_SCRIPT);
                    if (rc != 0) {
                        serviceFailed = true;
                        showError("本机服务已退出（code " + rc + "）");
                    }
                } catch (final Throwable t) {
                    serviceFailed = true;
                    showError("Python 启动失败：" + t.getMessage());
                }
            }
        });
    }

    private void waitForService() {
        pool.execute(new Runnable() {
            @Override
            public void run() {
                for (int i = 0; i < 400; i++) {
                    if (serviceFailed) return;
                    if (probe()) {
                        runOnUiThread(new Runnable() {
                            @Override
                            public void run() {
                                splash.setVisibility(View.GONE);
                                web.loadUrl(BASE + "/");
                            }
                        });
                        return;
                    }
                    try {
                        Thread.sleep(200);
                    } catch (InterruptedException e) {
                        return;
                    }
                }
                showError("本机服务启动超时，请重启 App");
            }
        });
    }

    private boolean probe() {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(BASE + "/api/config").openConnection();
            conn.setConnectTimeout(400);
            conn.setReadTimeout(400);
            conn.setRequestMethod("GET");
            return conn.getResponseCode() == 200;
        } catch (Exception ignored) {
            return false;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    /** 把 assets/py 展开到 filesDir/py；用 VERSION 标记避免每次启动都解压。 */
    private File prepareHome() throws IOException {
        File home = new File(getFilesDir(), "py");
        String version = readAssetText("py/VERSION").trim();
        File stamp = new File(home, ".stamp");
        if (version.equals(readText(stamp))
                && new File(home, "stdlib.zip").exists()
                && new File(home, "app/media_dl/__init__.py").exists()
                && new File(home, "site/flask/__init__.py").exists()
                && new File(home, "lib/python3.13/encodings/__init__.py").exists()) {
            return home;
        }
        setSplash("正在准备运行环境（首次启动约需十几秒）…");
        deleteRecursively(home);
        if (!home.mkdirs() && !home.isDirectory()) {
            throw new IOException("无法创建应用私有目录");
        }
        extractAssets("py", home);
        prepareStdlib(home);
        writeText(stamp, version);
        return home;
    }

    /** 标准库：解压 stdlib.zip 到 home/lib/python3.13，并从 assets 解压 C 扩展到
     *  lib-dynload。C 扩展随 APK assets 打包（assets/py/native/<abi>），不依赖系统把
     *  lib/<abi> 的 .so 提取到 nativeLibraryDir（雷电模拟器等环境不会提取全部标准库
     *  扩展，否则 import math 等会报 ModuleNotFoundError）。 */
    private void prepareStdlib(File home) throws IOException {
        File target = new File(home, "lib/python3.13");
        File marker = new File(target, ".ready");
        File dyn = new File(target, "lib-dynload");
        boolean dynHasSo = dyn.isDirectory() && soCount(dyn) > 0;
        if (marker.exists() && new File(target, "encodings/__init__.py").exists()
                && dyn.isDirectory() && dynHasSo) {
            return;
        }
        deleteRecursively(target);
        if (!target.mkdirs() && !target.isDirectory()) {
            throw new IOException("无法创建标准库目录");
        }
        unzip(new File(home, "stdlib.zip"), target);
        if (!dyn.mkdirs() && !dyn.isDirectory()) {
            throw new IOException("无法创建 lib-dynload 目录");
        }
        // 1) 优先从 assets 解压标准库 C 扩展（一定随包，跨环境一致）
        extractAssets("py/native/" + Build.CPU_ABI, dyn);
        // 2) 兜底：部分环境会把 APK lib/<abi> 的 .so 提取到 nativeLibraryDir，也一并复制
        //    （同名会被 assets 版本覆盖，无副作用）
        File soDir = new File(getApplicationInfo().nativeLibraryDir);
        File[] sos = soDir.listFiles();
        if (sos != null) {
            for (File so : sos) {
                if (so.isFile() && so.getName().endsWith(".so")) {
                    copyFile(so, new File(dyn, so.getName()));
                }
            }
        }
        if (soCount(dyn) == 0) {
            throw new IOException("标准库 C 扩展解压失败：lib-dynload 为空（请检查 assets/py/native/"
                    + Build.CPU_ABI + "）");
        }
        writeText(marker, "1");
    }

    /** 统计目录下 .so 文件个数（含子目录无需递归，lib-dynload 是平铺的）。 */
    private static int soCount(File dir) {
        File[] files = dir.listFiles();
        if (files == null) return 0;
        int n = 0;
        for (File f : files) if (f.isFile() && f.getName().endsWith(".so")) n++;
        return n;
    }

    private void extractAssets(String assetDir, File outDir) throws IOException {
        AssetManager am = getAssets();
        String[] children = am.list(assetDir);
        if (children == null) return;
        byte[] buffer = new byte[65536];
        for (String child : children) {
            String assetPath = assetDir + "/" + child;
            File out = new File(outDir, child);
            String[] sub = am.list(assetPath);
            if (sub != null && sub.length > 0) {
                if (!out.mkdirs() && !out.isDirectory()) {
                    throw new IOException("无法创建目录 " + out);
                }
                extractAssets(assetPath, out);
            } else {
                File parent = out.getParentFile();
                if (parent != null && !parent.exists() && !parent.mkdirs()) {
                    throw new IOException("无法创建目录 " + parent);
                }
                InputStream in = null;
                OutputStream os = null;
                try {
                    in = am.open(assetPath);
                    os = new FileOutputStream(out);
                    int n;
                    while ((n = in.read(buffer)) > 0) os.write(buffer, 0, n);
                    os.flush();
                } finally {
                    closeQuietly(in);
                    closeQuietly(os);
                }
            }
        }
    }

    private String readAssetText(String path) throws IOException {
        InputStream in = getAssets().open(path);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try {
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        } finally {
            closeQuietly(in);
        }
        return new String(out.toByteArray(), "UTF-8");
    }

    private static void unzip(File zip, File outDir) throws IOException {
        byte[] buf = new byte[65536];
        try (java.util.zip.ZipInputStream zis =
                     new java.util.zip.ZipInputStream(new java.io.FileInputStream(zip))) {
            java.util.zip.ZipEntry e;
            while ((e = zis.getNextEntry()) != null) {
                if (e.isDirectory()) { zis.closeEntry(); continue; }
                File f = new File(outDir, e.getName());
                File parent = f.getParentFile();
                if (parent != null && !parent.exists() && !parent.mkdirs()) {
                    throw new IOException("无法创建目录 " + parent);
                }
                java.io.OutputStream os = new java.io.FileOutputStream(f);
                try {
                    int n;
                    while ((n = zis.read(buf)) > 0) os.write(buf, 0, n);
                    os.flush();
                } finally {
                    closeQuietly(os);
                }
                zis.closeEntry();
            }
        }
    }

    private static void copyFile(File src, File dst) throws IOException {
        byte[] buf = new byte[65536];
        java.io.InputStream in = new java.io.FileInputStream(src);
        java.io.OutputStream os = new java.io.FileOutputStream(dst);
        try {
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
            os.flush();
        } finally {
            closeQuietly(in);
            closeQuietly(os);
        }
    }

    private static String readText(File file) {
        if (!file.exists()) return "";
        InputStream in = null;
        try {
            in = new java.io.FileInputStream(file);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[1024];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            return new String(out.toByteArray(), "UTF-8").trim();
        } catch (IOException e) {
            return "";
        } finally {
            closeQuietly(in);
        }
    }

    private static void writeText(File file, String text) throws IOException {
        OutputStream os = new FileOutputStream(file);
        try {
            os.write(text.getBytes("UTF-8"));
            os.flush();
        } finally {
            closeQuietly(os);
        }
    }

    private static void deleteRecursively(File file) {
        if (file == null || !file.exists()) return;
        if (file.isDirectory()) {
            File[] children = file.listFiles();
            if (children != null) {
                for (File child : children) deleteRecursively(child);
            }
        }
        // 仅清理本应用私有 filesDir/py 下的内容
        file.delete();
    }

    // ------------------------------------------------------------------ //
    // JS Bridge：原生下载到手机本地
    // ------------------------------------------------------------------ //
    public class JsBridge {
        @JavascriptInterface
        public boolean isApk() {
            return true;
        }

        @JavascriptInterface
        public String runtime() {
            return "android";
        }

        /**
         * 拉取系统分享 / VIEW 带入的链接（供前端主动消费）。
         * 与 push 通道（onSharedLink）互为兜底：无论注入发生在页面就绪前还是后，都不丢分享内容。
         * 读取即清空，避免同一链接被重复填入。
         */
        @JavascriptInterface
        public String takeSharedText() {
            String text = pendingSharedText;
            pendingSharedText = null;
            return text == null ? "" : text;
        }

        @JavascriptInterface
        public boolean download(String url, String title) {
            return download(url, title, "");
        }

        /** 带任务 ID 的下载：前端任务中心据此关联进度与取消。 */
        @JavascriptInterface
        public boolean download(String url, String title, String taskId) {
            if (url == null || url.length() == 0) return false;
            String full = url.startsWith("http") ? url : BASE + url;
            startNativeDownload(full, title == null ? "media" : title, taskId);
            return true;
        }

        /** 取消某个正在进行的原生下载（前端任务中心的「取消」按钮）。 */
        @JavascriptInterface
        public boolean cancelDownload(String taskId) {
            if (taskId == null || taskId.isEmpty()) return false;
            AtomicBoolean flag = downloadCancels.get(taskId);
            if (flag == null) return false;
            flag.set(true);
            return true;
        }

        /**
         * 列出已保存到手机的文件（DCIM/随手存 与 Download/随手存）。
         * 移动端的下载走原生写入相册，不落在 Python 服务的下载目录里，因此
         * /api/files 在手机上读不到；这里直接查 MediaStore，让「已下载」列表在手机上也可用。
         * 返回 JSON 数组字符串：[{name,size,time,kind}]，按时间倒序。
         */
        @JavascriptInterface
        public String listDownloads() {
            org.json.JSONArray arr = new org.json.JSONArray();
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    collectMedia(arr, MediaStore.Images.Media.EXTERNAL_CONTENT_URI, "image");
                    collectMedia(arr, MediaStore.Video.Media.EXTERNAL_CONTENT_URI, "video");
                    collectMedia(arr, MediaStore.Downloads.EXTERNAL_CONTENT_URI, "other");
                } else {
                    File dir = new File(Environment.getExternalStoragePublicDirectory(
                            Environment.DIRECTORY_DCIM), DIR_NAME);
                    collectFiles(arr, dir);
                    File ddir = new File(Environment.getExternalStoragePublicDirectory(
                            Environment.DIRECTORY_DOWNLOADS), DIR_NAME);
                    collectFiles(arr, ddir);
                }
            } catch (Exception ignored) {
            }
            return arr.toString();
        }

        @JavascriptInterface
        public void toast(final String message) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    Toast.makeText(MainActivity.this, message, Toast.LENGTH_SHORT).show();
                }
            });
        }

        /** 读取系统剪贴板文本。
         * 页面跑在 http://127.0.0.1（非安全上下文），navigator.clipboard 在 WebView 里
         * 不可用，前端点「粘贴」会直接抛错。这里用 Android ClipBoardManager 原生读取，
         * 完全绕开浏览器安全上下文限制（需在主线程访问剪贴板）。 */
        @JavascriptInterface
        public String readClipboard() {
            final String[] holder = new String[]{""};
            final java.util.concurrent.CountDownLatch latch =
                    new java.util.concurrent.CountDownLatch(1);
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    try {
                        android.content.ClipboardManager cm =
                                (android.content.ClipboardManager)
                                        getSystemService(CLIPBOARD_SERVICE);
                        if (cm != null && cm.hasPrimaryClip()
                                && cm.getPrimaryClip() != null
                                && cm.getPrimaryClip().getItemCount() > 0) {
                            CharSequence cs = cm.getPrimaryClip()
                                    .getItemAt(0).coerceToText(MainActivity.this);
                            if (cs != null) holder[0] = cs.toString();
                        }
                    } catch (Exception ignored) {
                    } finally {
                        latch.countDown();
                    }
                }
            });
            try {
                latch.await(1500, java.util.concurrent.TimeUnit.MILLISECONDS);
            } catch (InterruptedException ignored) {
            }
            return holder[0];
        }

        /** 同步移动端抖音登录态：不弹任何自建登录界面，只打开移动端抖音页面读取 Cookie。
         * 若移动端抖音已是登录状态（本 WebView 此前登录过 / 刚在页面上登录完），
         * 轮询会自动命中并保存，用户无需在系统里做任何登录操作。 */
        @JavascriptInterface
        public void openDouyinLogin() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    syncDouyinCookieVisible();
                }
            });
        }

        /** 静默探测：不显示界面，后台加载移动端抖音读取已有登录态。
         * 注意：@JavascriptInterface 方法由 WebView 在 JavaBridge 线程回调（非 UI 线程），
         * 而这里要 new WebView()，必须切回主线程，否则
         * 「Calling View methods on another thread than the UI thread」直接闪退。 */
        @JavascriptInterface
        public void probeDouyinCookie() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    probeDouyinCookieSilently();
                }
            });
        }
    }

    // ------------------------------------------------------------------ //
    // 移动端抖音登录态：本 App 不自建登录流程。
    // 打开的只是「移动端抖音」页面；只要移动端抖音已是登录状态，会话 Cookie 就在
    // 系统 WebView 的 CookieStore 里，直接读取并同步给本机 Python 服务（localhost），
    // 解析抖音时自动带上。Cookie 仅本机保存、只发往抖音，不上传任何服务器。
    // ------------------------------------------------------------------ //
    /** 可见模式：加载移动端抖音页面（用户可在其中确认/完成登录），自动轮询捕获。 */
    private void syncDouyinCookieVisible() {
        openDouyinWeb(true);
    }

    /** 静默模式：后台加载移动端抖音读取已有登录态，不打扰用户（不显示任何界面）。 */
    private void probeDouyinCookieSilently() {
        openDouyinWeb(false);
    }

    private void openDouyinWeb(final boolean visible) {
        // 兜底：WebView 必须在主线程创建，否则抛 IllegalStateException 直接闪退。
        // 任何调用方（尤其 @JavascriptInterface 回调）若不在主线程，这里统一切回去。
        if (android.os.Looper.myLooper() != android.os.Looper.getMainLooper()) {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    openDouyinWeb(visible);
                }
            });
            return;
        }
        if (loginWeb == null) {
            loginWeb = new WebView(this);
            WebSettings ls = loginWeb.getSettings();
            loginSettings = ls;
            ls.setJavaScriptEnabled(true);
            ls.setDomStorageEnabled(true);
            ls.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
            ls.setUseWideViewPort(true);
            ls.setLoadWithOverviewMode(true);
            // 移动端 UA：加载移动端抖音页面（与手机浏览器一致）。
            ls.setUserAgentString(DY_MOBILE_UA);
            CookieManager cm = CookieManager.getInstance();
            cm.setAcceptCookie(true);
            cm.setAcceptThirdPartyCookies(loginWeb, true);
            loginWeb.setWebViewClient(new WebViewClient() {
                @Override
                public void onPageFinished(WebView view, String url) {
                    checkDouyinLogin();
                }

                @Override
                public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                    String u = request.getUrl().toString();
                    if (u.startsWith("snssdk") || u.startsWith("douyin://")
                            || u.startsWith("aweme://") || u.startsWith("bdhttps")) {
                        return true;
                    }
                    return false;
                }
            });
        }

        if (visible) {
            if (loginOverlay == null) {
                buildDouyinOverlay();
            }
            loginOverlay.setVisibility(View.VISIBLE);
            if (loginWeb.getParent() == null) {
                ((LinearLayout) loginOverlay.getChildAt(0)).addView(loginWeb,
                        new LinearLayout.LayoutParams(
                                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
            }
        }
        // 已有登录态：立刻读取一次（无需等页面加载，Cookie 就在本地存储里）
        checkDouyinLogin();
        // 可见模式一直轮询；静默探测只轮询 20 次（约 20 秒），读不到即停
        startCookiePolling(visible ? 0 : 20);
        // 服务尚未就绪（首启）等场景下，重新挂载后再加载
        loginWeb.stopLoading();
        loginWeb.loadUrl(DY_MOBILE_URL);
    }

    /** 可见模式的浮层：顶部提示 + 移动端抖音网页（用户已登录则无需任何操作）。 */
    private void buildDouyinOverlay() {
        loginOverlay = new FrameLayout(this);
        loginOverlay.setBackgroundColor(0xFF0D1222);
        loginOverlay.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setLayoutParams(new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(12), dp(10), dp(12), dp(10));
        bar.setBackgroundColor(0xFF171F38);
        bar.setElevation(dp(4));

        loginTitle = new TextView(this);
        loginTitle.setText("读取移动端抖音登录态…");
        loginTitle.setTextColor(Color.WHITE);
        loginTitle.setTextSize(14f);
        loginTitle.setSingleLine(true);
        loginTitle.setEllipsize(android.text.TextUtils.TruncateAt.END);
        loginTitle.setLayoutParams(new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1));

        Button done = new Button(this);
        done.setText("立即读取");
        done.setTextSize(13f);
        done.setBackgroundColor(0xFF35E0B0);
        done.setTextColor(0xFF06251D);
        done.setPadding(dp(14), dp(6), dp(14), dp(6));
        done.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (!checkDouyinLogin()) {
                    Toast.makeText(MainActivity.this,
                            "还没读到登录态：请先在移动端抖音 App 或本页登录过抖音再试",
                            Toast.LENGTH_LONG).show();
                }
            }
        });

        Button close = new Button(this);
        close.setText("关闭");
        close.setTextSize(13f);
        close.setBackgroundColor(0xFF2A3354);
        close.setTextColor(Color.WHITE);
        close.setPadding(dp(14), dp(6), dp(14), dp(6));
        close.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                hideDouyinLogin();
            }
        });

        bar.addView(loginTitle);
        bar.addView(done);
        bar.addView(close);
        col.addView(bar);
        loginOverlay.addView(col);
        rootView.addView(loginOverlay);
    }

    /** 登录层是否已隐藏（用于延时任务里判断是否还需要继续操作页面）。 */
    private boolean loginOverlayHidden() {
        return loginOverlay == null || loginOverlay.getVisibility() != View.VISIBLE;
    }

    private void setLoginHint(final String text) {
        if (loginTitle == null) return;
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (loginTitle != null) loginTitle.setText(text);
            }
        });
    }

    /** 轮询捕获登录态：用户登录后往往不会主动点按钮，这里每秒自检一次，命中即自动保存。
     * maxTicks<=0 表示不计次数（可见模式：用户可能还在页面上操作，需一直等）；
     * 静默探测传入有限次数，读不到就停，避免后台无限轮询白耗电。
     */
    private void startCookiePolling() {
        startCookiePolling(0);
    }

    private void startCookiePolling(final int maxTicks) {
        stopCookiePolling();
        final int[] tick = new int[]{0};
        cookiePoll = new Runnable() {
            @Override
            public void run() {
                if (checkDouyinLogin()) return;
                tick[0]++;
                if (maxTicks > 0 && tick[0] >= maxTicks) {
                    cookiePoll = null;
                    return;
                }
                handler.postDelayed(this, 1000);
            }
        };
        handler.postDelayed(cookiePoll, 1200);
    }

    private void stopCookiePolling() {
        if (cookiePoll != null) {
            handler.removeCallbacks(cookiePoll);
            cookiePoll = null;
        }
    }

    private void hideDouyinLogin() {
        stopCookiePolling();
        if (loginWeb != null && loginWeb.getParent() instanceof LinearLayout) {
            ((LinearLayout) loginWeb.getParent()).removeView(loginWeb);
        }
        if (loginOverlay != null) loginOverlay.setVisibility(View.GONE);
    }

    /** 读取抖音域下的 Cookie 串（PC 主域、移动域、SSO/passport 域都尝试）。
     * 登录态 Cookie（sessionid 等）在 .douyin.com 下全域可见，无论从移动端还是 PC 端登录。 */
    private String readDouyinCookie() {
        CookieManager cm = CookieManager.getInstance();
        StringBuilder sb = new StringBuilder();
        for (String host : new String[]{"https://m.douyin.com", "https://www.douyin.com",
                                        "https://douyin.com", "https://sso.douyin.com",
                                        "https://passport.douyin.com"}) {
            String c = cm.getCookie(host);
            if (c != null && c.length() > 0) {
                if (sb.length() > 0) sb.append("; ");
                sb.append(c);
            }
        }
        return sb.toString();
    }

    /** 判断 Cookie 串是否含抖音登录态标识。
     * 只认真正代表「已登录」的键（sessionid / sid_tt / sid_guard）；
     * 不用 uid_tt、passport_csrf_token —— 它们在未登录的匿名会话里也会下发，
     * 会造成「一打开就误判为已登录」。 */
    private static boolean hasLoginCookie(String cookie) {
        if (cookie == null || cookie.isEmpty()) return false;
        for (String key : new String[]{"sessionid_ss", "sessionid", "sid_tt", "sid_guard"}) {
            if (cookie.contains(key)) return true;
        }
        return false;
    }

    /** 检测登录态：命中即自动保存并关闭浮层，返回是否已获取登录态。 */
    private boolean checkDouyinLogin() {
        // Cookie 可能还在内存未落盘，先 flush 再读，避免「已登录但读不到 sessionid」。
        try { CookieManager.getInstance().flush(); } catch (Exception ignored) { }
        String cookie = readDouyinCookie();
        if (hasLoginCookie(cookie)) {
            if (savedCookie != null && savedCookie.equals(cookie)) return true;
            savedCookie = cookie;
            saveDouyinCookie(cookie);
            stopCookiePolling();
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    Toast.makeText(MainActivity.this, "已获取移动端抖音登录态，已自动保存",
                            Toast.LENGTH_SHORT).show();
                    hideDouyinLogin();
                }
            });
            return true;
        }
        return false;
    }

    /** 把登录态 Cookie 推送到本机 Python 服务（localhost），解析时自动使用。 */
    private void saveDouyinCookie(final String cookie) {
        // 同时存到应用私有 SharedPreferences，作为本地状态展示与兜底
        getSharedPreferences("suishoucun", MODE_PRIVATE)
                .edit().putString("douyin_cookie", cookie).apply();
        pool.execute(new Runnable() {
            @Override
            public void run() {
                HttpURLConnection conn = null;
                try {
                    URL u = new URL(BASE + "/api/set-cookie");
                    conn = (HttpURLConnection) u.openConnection();
                    conn.setConnectTimeout(3000);
                    conn.setReadTimeout(3000);
                    conn.setRequestMethod("POST");
                    conn.setDoOutput(true);
                    conn.setRequestProperty("Content-Type", "application/json");
                    String body = "{\"cookie\":" + JSONObject.quote(cookie) + "}";
                    conn.getOutputStream().write(body.getBytes("UTF-8"));
                    conn.getInputStream().close();
                    // 通知前端刷新「已自动获取登录态」状态
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            try {
                                web.evaluateJavascript(
                                        "window.onCookieSaved && window.onCookieSaved()", null);
                            } catch (Exception ignored) { }
                        }
                    });
                } catch (Exception ignored) {
                    // 保存失败不阻断：至少 WebView / SharedPreferences 中已有
                } finally {
                    if (conn != null) conn.disconnect();
                }
            }
        });
    }

    private int dp(int px) {
        return (int) (px * getResources().getDisplayMetrics().density + 0.5f);
    }

    private void startNativeDownload(final String url, final String title) {
        startNativeDownload(url, title, null);
    }

    /** 带任务 ID 的下载：进度与取消都按 taskId 归属，供前端任务中心展示。 */
    private void startNativeDownload(final String url, final String title, final String taskId) {
        final String tid = (taskId == null || taskId.isEmpty()) ? ("t" + System.nanoTime()) : taskId;
        // 下载期间以前台服务保活：切后台 / 锁屏也不会被系统回收；通知栏显示进度与取消。
        activeDownloads.incrementAndGet();
        DownloadService.cancelAllRequested.set(false);   // 新一轮下载清掉上一次的全局取消信号
        DownloadService.update(this, activeDownloads.get(), 0);
        pool.execute(new Runnable() {
            @Override
            public void run() {
                doDownload(url, title, tid);
            }
        });
    }

    private void doDownload(String url, String title, String taskId) {
        HttpURLConnection conn = null;
        InputStream in = null;
        Sink sink = null;
        String filename = safeName(title);
        final AtomicBoolean cancelFlag = new AtomicBoolean(false);
        if (taskId != null) downloadCancels.put(taskId, cancelFlag);
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(90000);
            conn.setInstanceFollowRedirects(true);
            conn.setRequestProperty("User-Agent", "MediaDLAndroid/6.0");
            int code = conn.getResponseCode();
            if (code >= 400) throw new Exception("HTTP " + code + "（请重新解析链接）");

            String disposition = conn.getHeaderField("Content-Disposition");
            String mime = conn.getContentType();
            filename = pickName(disposition, title, mime);
            long total = conn.getContentLength();
            sink = openSink(filename, mime);
            in = conn.getInputStream();
            byte[] buffer = new byte[65536];
            long written = 0;
            long lastNotify = 0;
            int n;
            while ((n = in.read(buffer)) > 0) {
                if (cancelFlag.get() || DownloadService.cancelAllRequested.get()) {
                    // 用户取消（界面按钮或通知栏「取消」）：丢弃已写内容，回调 cancelled
                    abortSink(sink);
                    sink = null;
                    callback("cancelled", filename, taskId, "");
                    return;
                }
                sink.out.write(buffer, 0, n);
                written += n;
                if (written - lastNotify > 512 * 1024) {
                    lastNotify = written;
                    callback("progress", filename, taskId,
                            progJson(written, total, 0));
                }
            }
            sink.out.flush();
            finishSink(sink);
            callback("done", filename, taskId, progJson(written, total, 0));
        } catch (Exception e) {
            if (sink != null) abortSink(sink);
            callback("error", filename, taskId,
                    e.getMessage() == null ? "下载失败" : e.getMessage());
        } finally {
            if (taskId != null) downloadCancels.remove(taskId);
            closeQuietly(in);
            if (sink != null) closeQuietly(sink.out);
            if (conn != null) conn.disconnect();
            // 结束一个下载：计数减一；全部结束后撤下前台服务通知
            int left = activeDownloads.decrementAndGet();
            if (left <= 0) {
                activeDownloads.set(0);
                DownloadService.stop(this);
            } else {
                DownloadService.update(this, activeDownloads.get(), 0);
            }
        }
    }

    /** 构造进度 JSON 字符串（作为 callback 的 message 参数，前端 JSON.parse）。 */
    private static String progJson(long written, long total, long speed) {
        StringBuilder sb = new StringBuilder("{");
        sb.append("\"bytes\":").append(written);
        sb.append(",\"total\":").append(total < 0 ? 0 : total);
        sb.append(",\"speed\":").append(speed);
        sb.append("}");
        return sb.toString();
    }

    private static class Sink {
        OutputStream out;
        Uri uri;
        String path;

        Sink(OutputStream out, Uri uri, String path) {
            this.out = out;
            this.uri = uri;
            this.path = path;
        }
    }

    /** 0=图片 / 1=视频 / 2=其它（仍走 Download/随手存）。 */
    private static int mediaTypeOf(String mime) {
        if (mime == null) return 2;
        String m = mime.toLowerCase().split(";")[0];
        if (m.startsWith("image/")) return 0;
        if (m.startsWith("video/")) return 1;
        return 2;
    }

    private Sink openSink(String filename, String mime) throws Exception {
        String type = mime == null ? "application/octet-stream" : mime.split(";")[0];
        int media = mediaTypeOf(type);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // 图片 / 视频写入相册（DCIM/随手存），使其在系统图库里可见；
            // 其它类型（如 zip 图集）仍落 Download/随手存。
            Uri collection;
            String relPath;
            if (media == 0) {
                collection = MediaStore.Images.Media.EXTERNAL_CONTENT_URI;
                relPath = Environment.DIRECTORY_DCIM + "/" + DIR_NAME;
            } else if (media == 1) {
                collection = MediaStore.Video.Media.EXTERNAL_CONTENT_URI;
                relPath = Environment.DIRECTORY_DCIM + "/" + DIR_NAME;
            } else {
                collection = MediaStore.Downloads.EXTERNAL_CONTENT_URI;
                relPath = Environment.DIRECTORY_DOWNLOADS + "/" + DIR_NAME;
            }
            ContentValues cv = new ContentValues();
            cv.put(MediaStore.Downloads.DISPLAY_NAME, filename);
            cv.put(MediaStore.Downloads.MIME_TYPE, type);
            cv.put(MediaStore.Images.Media.RELATIVE_PATH, relPath);
            cv.put(MediaStore.Images.Media.IS_PENDING, 1);
            Uri uri = getContentResolver().insert(collection, cv);
            if (uri == null) throw new Exception("无法在相册/下载目录创建文件");
            OutputStream os = getContentResolver().openOutputStream(uri);
            if (os == null) throw new Exception("无法打开下载文件");
            return new Sink(os, uri, null);
        }
        // 旧版本：直接写 DCIM/随手存（图片 / 视频）或 Download/随手存，再扫描进图库
        File dir;
        if (media == 0 || media == 1) {
            dir = new File(Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DCIM), DIR_NAME);
        } else {
            dir = new File(Environment.getExternalStoragePublicDirectory(
                    Environment.DIRECTORY_DOWNLOADS), DIR_NAME);
        }
        if (!dir.exists() && !dir.mkdirs()) throw new Exception("无法创建保存目录");
        File file = new File(dir, filename);
        return new Sink(new FileOutputStream(file), Uri.fromFile(file), file.getAbsolutePath());
    }

    private void finishSink(Sink sink) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && sink.uri != null) {
            ContentValues done = new ContentValues();
            done.put(MediaStore.Downloads.IS_PENDING, 0);
            getContentResolver().update(sink.uri, done, null, null);
        } else if (sink.path != null) {
            MediaScannerConnection.scanFile(this, new String[]{sink.path}, null, null);
        }
    }

    private void abortSink(Sink sink) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && sink.uri != null) {
            try {
                getContentResolver().delete(sink.uri, null, null);
            } catch (Exception ignored) {
            }
        }
    }

    /** 查询 MediaStore 指定集合里、位于「随手存」目录下的媒体，追加到 JSON 数组。 */
    private void collectMedia(org.json.JSONArray arr, Uri collection, String kind) {
        String rel = (kind.equals("other")
                ? Environment.DIRECTORY_DOWNLOADS + "/" + DIR_NAME
                : Environment.DIRECTORY_DCIM + "/" + DIR_NAME) + "/";
        String[] proj = {
                MediaStore.MediaColumns.DISPLAY_NAME,
                MediaStore.MediaColumns.SIZE,
                MediaStore.MediaColumns.DATE_ADDED,
                MediaStore.MediaColumns.RELATIVE_PATH,
        };
        android.database.Cursor c = null;
        try {
            c = getContentResolver().query(collection, proj, null, null,
                    MediaStore.MediaColumns.DATE_ADDED + " DESC");
            if (c == null) return;
            int iName = c.getColumnIndex(MediaStore.MediaColumns.DISPLAY_NAME);
            int iSize = c.getColumnIndex(MediaStore.MediaColumns.SIZE);
            int iTime = c.getColumnIndex(MediaStore.MediaColumns.DATE_ADDED);
            int iRel = c.getColumnIndex(MediaStore.MediaColumns.RELATIVE_PATH);
            int n = 0;
            while (c.moveToNext() && n < 200) {
                String name = iName >= 0 ? c.getString(iName) : null;
                String relPath = iRel >= 0 ? c.getString(iRel) : "";
                if (name == null || relPath == null || !relPath.contains(DIR_NAME)) continue;
                org.json.JSONObject o = new org.json.JSONObject();
                o.put("name", name);
                o.put("size", iSize >= 0 ? c.getLong(iSize) : 0);
                o.put("time", iTime >= 0 ? c.getLong(iTime) : 0);
                o.put("kind", kind);
                arr.put(o);
                n++;
            }
        } catch (Exception ignored) {
        } finally {
            if (c != null) c.close();
        }
    }

    /** 旧版本（Android 9 及以下）：直接遍历目录，按修改时间倒序。 */
    private void collectFiles(org.json.JSONArray arr, File dir) {
        try {
            if (dir == null || !dir.isDirectory()) return;
            File[] list = dir.listFiles();
            if (list == null) return;
            java.util.Arrays.sort(list, new java.util.Comparator<File>() {
                @Override
                public int compare(File a, File b) {
                    return Long.compare(b.lastModified(), a.lastModified());
                }
            });
            int n = 0;
            for (File f : list) {
                if (!f.isFile() || n >= 200) continue;
                org.json.JSONObject o = new org.json.JSONObject();
                o.put("name", f.getName());
                o.put("size", f.length());
                o.put("time", f.lastModified() / 1000);
                o.put("kind", guessKind(f.getName()));
                arr.put(o);
                n++;
            }
        } catch (Exception ignored) {
        }
    }

    private static String guessKind(String name) {
        String n = name == null ? "" : name.toLowerCase();
        if (n.endsWith(".jpg") || n.endsWith(".jpeg") || n.endsWith(".png")
                || n.endsWith(".webp") || n.endsWith(".gif")) return "image";
        if (n.endsWith(".mp4") || n.endsWith(".mov") || n.endsWith(".mkv")
                || n.endsWith(".webm")) return "video";
        if (n.endsWith(".mp3") || n.endsWith(".m4a") || n.endsWith(".aac")) return "audio";
        return "other";
    }

    private void callback(final String state, final String name, final String taskId, final String message) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                String js = "window.onNativeDownload && window.onNativeDownload("
                        + JSONObject.quote(state) + ","
                        + JSONObject.quote(name) + ","
                        + JSONObject.quote(taskId == null ? "" : taskId) + ","
                        + JSONObject.quote(message == null ? "" : message) + ")";
                try {
                    web.evaluateJavascript(js, null);
                } catch (Exception ignored) {
                }
            }
        });
    }

    private static String safeName(String title) {
        String s = title == null ? "media" : title;
        s = s.replaceAll("[\\\\/:*?\"<>|\\r\\n]", "_").trim();
        if (s.length() > 80) s = s.substring(0, 80);
        if (s.length() == 0) s = "media";
        return s;
    }

    private static String pickName(String disposition, String title, String mime) {
        if (disposition != null) {
            int star = disposition.indexOf("filename*=");
            if (star >= 0) {
                String v = disposition.substring(star + 10).trim();
                int semi = v.indexOf(';');
                if (semi > 0) v = v.substring(0, semi);
                v = v.trim();
                if (v.length() > 1 && v.startsWith("\"") && v.endsWith("\""))
                    v = v.substring(1, v.length() - 1);
                int ticks = v.indexOf("''");
                if (ticks >= 0) v = v.substring(ticks + 2);
                try {
                    return safeName(URLDecoder.decode(v, "UTF-8"));
                } catch (Exception e) {
                    return safeName(v);
                }
            }
            int plain = disposition.indexOf("filename=");
            if (plain >= 0) {
                String v = disposition.substring(plain + 9).trim();
                int semi = v.indexOf(';');
                if (semi > 0) v = v.substring(0, semi);
                v = v.trim().replace("\"", "");
                if (v.length() > 0) return safeName(v);
            }
        }
        String ext = ".bin";
        if (mime != null) {
            String m = mime.toLowerCase();
            if (m.contains("mp4")) ext = ".mp4";
            else if (m.contains("jpeg") || m.contains("jpg")) ext = ".jpg";
            else if (m.contains("png")) ext = ".png";
            else if (m.contains("webp")) ext = ".webp";
            else if (m.contains("gif")) ext = ".gif";
            else if (m.contains("zip")) ext = ".zip";
            else if (m.contains("mpeg") || m.contains("mp3")) ext = ".mp3";
            else if (m.contains("webm")) ext = ".webm";
        }
        return safeName(title) + ext;
    }

    private static void closeQuietly(java.io.Closeable c) {
        if (c != null) {
            try {
                c.close();
            } catch (Exception ignored) {
            }
        }
    }

    @Override
    public void onBackPressed() {
        if (loginOverlay != null && loginOverlay.getVisibility() == View.VISIBLE) {
            hideDouyinLogin();
            return;
        }
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (loginWeb != null) {
            loginWeb.stopLoading();
            loginWeb.destroy();
            loginWeb = null;
        }
        pool.shutdownNow();
    }
}
