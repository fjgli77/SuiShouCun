# 随手存 · SuiShouCun

把抖音 / Bilibili / 快手 / 小红书 / 微博 等平台的视频、图片、图集「随手」存到本机的下载器。
**PC 桌面端与 Android 手机端共用同一套 Python 解析/下载内核，各自在本机运行，互不联网。**

---

## 功能特性

- **多平台解析**：抖音、Bilibili、快手、小红书、微博等。
- **去水印**：自动优选无水印直链（抖音移动端 `/play/` 通道），无则回退平台原文件。
- **画质增强**：下载选项设置画质上限（`best` / `2160` / `1080` / `720` / `480` / `360`），直链视频按设定画质下载，其余地址作兜底切换。低于平台全部可选画质时取最低兜底。
- **图集打包**：图片图集自动打包为 zip。
- **双端一致**：Windows EXE 与 Android APK 共用 `media_dl` 全部源码与 `media_dl/web` 前端，差异只在响应式样式与按端裁剪的功能。
- **移动端存相册**：Android 端下载的图片 / 视频直接写入系统相册（`DCIM/随手存`），图集 zip 仍走下载目录。
- **本机 Web 界面**：浏览器或独立窗口操作，扫码即用；支持单实例（已运行时直接打开同一界面）。

## 支持的站点

抖音、Bilibili、快手、小红书、微博，以及 yt-dlp 支持的主流视频站点（通过 `--audio-only` 等选项可提取音频）。

## 目录结构

```
desktop_app.py      Windows 桌面端 EXE 入口（无控制台窗口，--app 模式打开独立窗口）
build_exe.py        打包 Windows EXE（PyInstaller onefile）
build_exe.bat
media_dl/           共享的解析 / 下载 / 服务内核 + Web 前端
  web/              前端（index.html / app.js）
  cli.py            命令行入口（python -m media_dl）
  server.py         本机 Flask 服务与界面
  extractor.py     多平台解析编排
  nocookie.py      抖音等免登录直链解析（含画质候选）
  downloader.py    下载调度（含直链视频按画质选择）
  delivery.py       交付层（浏览器 / 安卓原生下载）
  options.py       下载选项与画质选择逻辑
  models.py / qr.py / links.py / android_entry.py ...
android/            Android 端源码与构建脚本
  java/ jni/ res/ AndroidManifest.xml
  build_apk.py      本地 Android SDK 打包 APK（内嵌 CPython 3.13，不依赖 Gradle）
  build_apk.bat
requirements.txt
selftest.py         端到端自测
```

## 快速开始

### 直接用成品（构建后）

- **Windows**：双击 `dist/随手存-电脑端.exe`，自动打开本机界面。
- **Android**：`adb install -r dist/随手存-手机版.apk`，打开应用即可。

### 从源码运行

```bash
pip install -r requirements.txt
python -m media_dl --web --open      # 打开本机界面
```

### 命令行下载

```bash
# 直接粘整段抖音分享口令 / 链接
python -m media_dl "8.87 复制打开抖音... https://v.douyin.com/xxx/"

# 指定保存目录与最高画质
python -m media_dl -o D:/media -q 1080 https://www.bilibili.com/video/BVxxxx

# 只下载音频 / 批量从文件读取
python -m media_dl --audio-only -f links.txt

# 仅解析并输出 JSON
python -m media_dl --parse-only --json https://v.kuaishou.com/xxxx
```

全部参数见 `python -m media_dl --help`。

## 构建

### Windows EXE

```bash
pip install pyinstaller
python build_exe.py
```

产物：`dist/随手存-电脑端.exe`（同时保留 `dist/SuiShouCun-PC.exe`）。

### Android APK

需要：Android SDK（含 `platforms/android-34`、`build-tools/36.0.0`）、NDK 28.2.x、JDK 8+、curl；将 SDK 路径写入环境变量 `ANDROID_HOME`。
首次构建会从 Maven Central 下载约 9MB 的 CPython 运行时并缓存到 `android/vendor/`，之后可离线构建。

```bash
python android/build_apk.py
```

产物：`dist/随手存-手机版.apk`（arm64-v8a，内嵌 CPython 3.13，本机服务 `127.0.0.1:8765`）。

## 说明

- 本工具仅用于个人合法拥有的内容备份与学习研究，请遵守各平台服务条款与当地法律法规。
- 部分内容需登录态才能解析，可用 `--cookies-from-browser chrome|edge|firefox` 或 `--cookie-file cookies.txt`。
- 画质受平台实际返回限制；若平台仅提供单一画质，选择更高档会自动回退到最高可用画质。
