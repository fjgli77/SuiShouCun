"""随手存 · 本地服务：PC 端与 Android 端共用同一份实现。

v6 架构（一个应用，两种打包形态）：
- PC：desktop_app.py 在本机 127.0.0.1 启动本服务，并用 Edge --app 打开桌面窗口；
- Android：APK 内嵌 CPython，在手机本机 127.0.0.1 启动同一个 Flask 服务，
  WebView 直接打开它，文件由 Java 原生写入手机存储。

两端各自独立运行，彼此不连接、不发现、不配对；不存在局域网地址、访问口令、
客户端密钥、二维码扫码。差异只体现在前端响应式样式与按端能力裁剪的功能上。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, make_response, request, send_file, send_from_directory

from .delivery import create_ticket, fetch as fetch_browser_download, fetch_thumbnail
from .downloader import download_entry
from .extractor import parse_text
from .links import detect_media
from .models import Progress
from .options import DownloadOptions, has_ffmpeg
from .qr import qr_svg
from .version import __version_short__

WEB_DIR = Path(__file__).resolve().parent / "web"
app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False

_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.RLock()
_DEFAULT_OUTDIR = Path.cwd() / "downloads"

# APP 内登录抖音自动捕获的登录态 Cookie：仅在本机保存与使用，只发往抖音，绝不上传。
_DATA_DIR = Path(__file__).resolve().parent / "data"
_COOKIE_STORE = _DATA_DIR / "cookie_store.txt"


def _load_cookie_store() -> str:
    try:
        if _COOKIE_STORE.exists():
            return _COOKIE_STORE.read_text(encoding="utf-8").strip()
    except Exception:                       # noqa: BLE001
        return ""
    return ""


def _save_cookie_store(cookie: str) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _COOKIE_STORE.write_text((cookie or "").strip(), encoding="utf-8")
    except Exception:                       # noqa: BLE001
        pass

_STATE: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8765,
    # "pc" / "android"；由启动入口显式指定，Android 入口见 android_entry.py
    "runtime": "pc",
}


def _is_local() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


# 把内部异常翻译成用户可读文案：主文案不含英文异常类名，原始信息留作技术详情。
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("403", "平台拒绝匿名访问（风控），请稍后重试或改用分享口令"),
    ("401", "该内容需要登录后才能访问"),
    ("404", "内容不存在或链接已失效"),
    ("429", "请求过于频繁被限流，请稍后再试"),
    ("timed out", "网络请求超时，请检查网络后重试"),
    ("timeout", "网络请求超时，请检查网络后重试"),
    ("connection", "网络连接失败，请检查网络后重试"),
    ("ssl", "加密连接异常，请检查网络环境后重试"),
    ("已过期", "下载链接已过期，请重新解析后再下载"),
    ("不可用", "媒体地址暂时不可用，请稍后重试"),
    ("没有产生文件", "下载未产生文件，请重试"),
)


def _friendly_error(exc: Exception, fallback: str = "操作失败，请稍后重试") -> str:
    """把异常翻译成面向用户的短文案（不含英文类名 / 堆栈）。"""
    text = f"{type(exc).__name__} {exc}".lower()
    for needle, hint in _ERROR_HINTS:
        if needle.lower() in text:
            return hint
    return fallback


def runtime() -> str:
    with _LOCK:
        return str(_STATE["runtime"])


@app.before_request
def guard_api():
    """只服务本机页面：拒绝任何非 127.0.0.1 的请求，杜绝被局域网当作跳板。"""
    if request.method == "OPTIONS":
        return make_response("", 204)
    if not _is_local():
        return jsonify({"error": "随手存只在本机运行，不提供远程访问"}), 403
    return None


@app.after_request
def cors_headers(resp):
    origin = request.headers.get("Origin")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers.add("Vary", "Origin")
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Range"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Expose-Headers"] = (
            "Content-Disposition, Content-Range, Accept-Ranges")
    return resp


# --------------------------------------------------------------------------- #
# 页面与静态资源：只有一个入口、只有一套前端
# --------------------------------------------------------------------------- #
def _page() -> Response:
    resp = make_response(send_from_directory(WEB_DIR, "index.html"))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
def index():
    return _page()


@app.get("/m")
def legacy_mobile_page():
    """保留旧书签 / 分享链接可用：v6 起移动端与 PC 端是同一个页面。"""
    return _page()


@app.get("/pc")
def legacy_desktop_page():
    return _page()


@app.get("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(WEB_DIR, filename)


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(WEB_DIR, "manifest.webmanifest")


@app.get("/sw.js")
def service_worker():
    resp = make_response(send_from_directory(WEB_DIR, "sw.js"))
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/config")
def api_config():
    current = runtime()
    return jsonify({
        "outdir": str(_DEFAULT_OUTDIR.resolve()),
        "ffmpeg": has_ffmpeg(),
        "default_options": DownloadOptions(outdir=_DEFAULT_OUTDIR).to_dict(),
        "platforms": ["抖音", "哔哩哔哩", "快手", "小红书", "微博", "YouTube", "通用网页"],
        "api_base": request.host_url.rstrip("/"),
        "browser_download": True,
        "watermark_free": True,
        "preview": True,
        "nocookie": True,
        "version": __version_short__,
        "runtime": current,
        "client": current,
        # 前端能力开关：Android 上有 Java 原生下载，PC 上交给浏览器
        "native_download": current == "android",
    })


@app.post("/api/auth")
def api_auth():
    """v6 没有口令与配对，任何本机请求都已是可信的。"""
    return jsonify({"ok": True, "client": runtime()})


@app.post("/api/set-cookie")
def api_set_cookie():
    """APP 内登录抖音后，由 Android 端把捕获的登录态 Cookie 存到本机（只发往抖音）。"""
    data = request.get_json(silent=True) or {}
    cookie = str(data.get("cookie") or "").strip()
    _save_cookie_store(cookie)
    return jsonify({"ok": True, "has_cookie": bool(cookie)})


@app.get("/api/cookie")
def api_get_cookie():
    """前端读取本机是否已存有登录态 Cookie（用于显示状态 / 自动填充）。"""
    c = _load_cookie_store()
    return jsonify({"cookie": c, "has_cookie": bool(c)})


@app.post("/api/shutdown")
def api_shutdown():
    """PC EXE 的退出入口：只允许本机界面调用；Android 端不提供退出。"""
    if not _is_local():
        return jsonify({"error": "只允许本机调用"}), 403

    def stop() -> None:
        time.sleep(0.6)
        os._exit(0)

    threading.Thread(target=stop, daemon=True, name="media_dl_shutdown").start()
    return jsonify({"ok": True, "message": "桌面端即将退出"})


@app.get("/api/qrcode")
def api_qrcode():
    """通用二维码工具（保留给外部脚本，界面已不再使用扫码）。"""
    data = str(request.args.get("data") or "")
    if not data.startswith("http"):
        return jsonify({"error": "无效的二维码内容"}), 400
    try:
        return Response(qr_svg(data), mimetype="image/svg+xml")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                          # noqa: BLE001
        return jsonify({"error": f"二维码生成失败：{exc}"}), 500


def _qrcode_available() -> bool:
    return True


@app.post("/api/detect")
def api_detect():
    data = request.get_json(silent=True) or {}
    return jsonify({"results": detect_media(str(data.get("text") or ""), expand=False)})


@app.post("/api/parse")
def api_parse():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "请粘贴链接或分享口令"}), 400
    opts = _opts_from_request(data)
    try:
        results = parse_text(text, opts)
        entries = [e for r in results for e in r.entries]
        ticket, items = create_ticket(entries, opts)
        return jsonify({
            "results": [r.to_dict() for r in results],
            "ticket": ticket,
            "items": items,
            "remove_watermark": opts.remove_watermark,
        })
    except Exception as exc:                          # noqa: BLE001
        return jsonify({"error": _friendly_error(exc, "解析失败，请确认链接可公开访问或稍后重试")}), 500


@app.get("/api/fetch")
def api_fetch():
    """下载或预览：流式转发解析结果里的媒体直链。

    preview=1 时以 inline + Range 支持返回，供结果栏直接预览图片 / 播放视频；
    否则以 attachment 交给浏览器（PC）或交给 APK 原生下载器（Android）。
    """
    ticket = str(request.args.get("ticket") or "")
    if not ticket:
        return jsonify({"error": "缺少下载票据，请重新解析"}), 400
    try:
        index = int(request.args.get("i") or 0)
        file_index = int(request.args.get("j") or -1)
    except ValueError:
        return jsonify({"error": "下载序号无效"}), 400
    as_zip = str(request.args.get("zip") or "1") != "0"
    preview = str(request.args.get("preview") or "0") == "1"
    range_header = request.headers.get("Range", "") if preview else ""
    quality_override = request.args.get("q")
    try:
        quality_override = int(quality_override) if quality_override else None
    except (TypeError, ValueError):
        quality_override = None
    try:
        return fetch_browser_download(ticket, index, file_index, as_zip,
                                      preview, range_header, quality_override)
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:                          # noqa: BLE001
        return jsonify({"error": _friendly_error(exc, "下载失败，请稍后重试")}), 502


@app.get("/api/thumb")
def api_thumb():
    """解析结果栏封面（服务端代理，避免防盗链与跨域）。"""
    ticket = str(request.args.get("ticket") or "")
    if not ticket:
        return jsonify({"error": "缺少票据"}), 400
    try:
        index = int(request.args.get("i") or 0)
    except ValueError:
        return jsonify({"error": "序号无效"}), 400
    try:
        return fetch_thumbnail(ticket, index)
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:                          # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.post("/api/jobs")
def api_create_job():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "请粘贴链接或分享口令"}), 400
    opts = _opts_from_request(data)
    jid = uuid.uuid4().hex[:12]
    job: dict[str, Any] = {
        "id": jid, "text": text[:2000], "options": opts.to_dict(),
        "status": "queued", "cards": {}, "entries": [], "errors": [],
        "files": [], "logs": [], "created": time.time(), "finished": None,
        "cancel": threading.Event(),
    }
    with _LOCK:
        _JOBS[jid] = job
    threading.Thread(target=_run_job, args=(jid,), daemon=True).start()
    return jsonify({"id": jid})


@app.get("/api/jobs/<jid>")
def api_get_job(jid: str):
    with _LOCK:
        job = _JOBS.get(jid)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify({k: v for k, v in job.items() if k != "cancel"})


@app.get("/api/jobs")
def api_list_jobs():
    with _LOCK:
        return jsonify([{
            "id": j["id"], "status": j["status"], "created": j["created"],
            "entries": len(j["entries"]), "files": len(j["files"]),
            "text": j["text"][:100],
        } for j in sorted(_JOBS.values(), key=lambda x: x["created"], reverse=True)[:30]])


@app.post("/api/jobs/<jid>/cancel")
def api_cancel_job(jid: str):
    with _LOCK:
        job = _JOBS.get(jid)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        job["cancel"].set()
        _log(job, "收到取消请求，正在停止...")
        return jsonify({"ok": True})


@app.get("/api/files")
def api_files():
    data = _current_outdir()
    files: list[dict[str, Any]] = []
    if data.exists():
        for p in sorted(data.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True)[:100]:
            if p.is_file() and not p.name.endswith((".part", ".ytdl")):
                files.append({
                    "name": p.name, "path": str(p),
                    "rel": str(p.relative_to(data)), "size": p.stat().st_size,
                    "time": p.stat().st_mtime,
                })
    return jsonify({"outdir": str(data), "files": files})


def _safe_file_path() -> Path | None:
    path = Path(str(request.args.get("path") or "")).resolve()
    root = _current_outdir().resolve()
    if not path.exists() or not path.is_file() or root not in path.parents:
        return None
    return path


@app.get("/api/file")
def api_file():
    path = _safe_file_path()
    if not path:
        return jsonify({"error": "文件不存在"}), 404
    return send_file(path, conditional=True)


@app.get("/api/download")
def api_download():
    path = _safe_file_path()
    if not path:
        return jsonify({"error": "文件不存在"}), 404
    return send_file(path, as_attachment=True, download_name=path.name, conditional=True)


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #
def _opts_from_request(data: dict[str, Any]) -> DownloadOptions:
    """界面不提供目录 / 自动读取浏览器 Cookie / 代理：统一由服务端固定。

    可选开关（画质、去水印、图集数量、短链展开、登录态 Cookie 等）由前端 options 传入。
    其中 cookie 是用户手动粘贴的抖音登录态，仅本机保存、只发往抖音，不上传任何服务器；
    cookies_browser（自动读取浏览器登录态）与 proxy 出于隐私 / 产品策略保持关闭。
    """
    raw = dict(data.get("options") or {})
    raw["outdir"] = str(_DEFAULT_OUTDIR)
    raw["cookies_browser"] = ""      # 不自动读取浏览器登录态（隐私）
    raw["cookie_file"] = ""          # 界面只走手动粘贴的 cookie 字符串
    raw["proxy"] = ""                # 产品策略：全程免代理
    # Cookie 优先用界面手动粘贴的；为空时回退到 APP 内登录抖音自动捕获并存储的登录态
    raw["cookie"] = (raw.get("cookie") or "").strip() or _load_cookie_store()
    return DownloadOptions.from_dict(raw)


def _current_outdir() -> Path:
    with _LOCK:
        for job in sorted(_JOBS.values(), key=lambda x: x["created"], reverse=True):
            outdir = str(job.get("options", {}).get("outdir") or "")
            if outdir:
                return Path(outdir)
    return _DEFAULT_OUTDIR.resolve()


def _log(job: dict[str, Any], message: str) -> None:
    with _LOCK:
        job["logs"].append({"t": time.strftime("%H:%M:%S"), "msg": message})
        if len(job["logs"]) > 200:
            del job["logs"][:-200]


def _run_job(jid: str) -> None:
    with _LOCK:
        job = _JOBS.get(jid)
    if not job:
        return
    opts = DownloadOptions.from_dict(job["options"])
    cancel: threading.Event = job["cancel"]

    def on_progress(p: Progress) -> None:
        if cancel.is_set():
            raise RuntimeError("任务已取消")
        with _LOCK:
            job["cards"][p.entry_id] = p.to_dict()
        if p.status in ("finished", "error"):
            _log(job, f"[{p.status}] {p.title} {p.message}")
        if p.files:
            with _LOCK:
                for f in p.files:
                    if f not in job["files"]:
                        job["files"].append(f)

    try:
        with _LOCK:
            job["status"] = "running"
        _log(job, f"开始解析 {len(job['text'].splitlines()) or 1} 行输入")
        parsed = parse_text(job["text"], opts)
        with _LOCK:
            job["entries"] = [e.to_dict() for r in parsed for e in r.entries]
        for r in parsed:
            _log(job, f"{r.link.platform_name}: 识别到 {len(r.entries)} 个可下载单元")
            if r.error:
                _log(job, f"解析失败 {r.link.url}: {r.error}")
                with _LOCK:
                    job["errors"].append(f"{r.link.url}: {r.error}")
            for w in r.warnings:
                _log(job, f"提示: {w}")

        for r in parsed:
            for entry in r.entries:
                if cancel.is_set():
                    break
                try:
                    on_progress(Progress(entry_id=entry.id, status="pending", title=entry.title))
                    files = download_entry(entry, opts, on_progress)
                    with _LOCK:
                        for f in files:
                            if f not in job["files"]:
                                job["files"].append(f)
                except RuntimeError as exc:
                    if "取消" not in str(exc):
                        friendly = _friendly_error(exc, "下载失败，请稍后重试")
                        _log(job, f"失败 {entry.title}: {exc}")
                        with _LOCK:
                            job["errors"].append(f"{entry.title}: {friendly}")
                            job["cards"][entry.id] = Progress(
                                entry_id=entry.id, status="error", title=entry.title,
                                message=friendly).to_dict()
                    break
                except Exception as exc:                  # noqa: BLE001
                    friendly = _friendly_error(exc, "下载失败，请稍后重试")
                    _log(job, f"失败 {entry.title}: {exc}")
                    with _LOCK:
                        job["errors"].append(f"{entry.title}: {friendly}")
                        job["cards"][entry.id] = Progress(
                            entry_id=entry.id, status="error", title=entry.title,
                            message=friendly).to_dict()
            if cancel.is_set():
                break

        with _LOCK:
            job["status"] = "cancelled" if cancel.is_set() else "done"
            job["finished"] = time.time()
        _log(job, f"任务结束：{len(job['files'])} 个文件，{len(job['errors'])} 项失败")
    except Exception as exc:                              # noqa: BLE001
        with _LOCK:
            job["status"] = "error"
            job["errors"].append(_friendly_error(exc, "任务执行异常，请重试"))
            job["finished"] = time.time()
        _log(job, f"任务异常: {exc}")


# --------------------------------------------------------------------------- #
# 桌面窗口 / 启动
# --------------------------------------------------------------------------- #
def _open_ui(url: str, app_mode: bool) -> None:
    if app_mode:
        candidates: list[str] = []
        if sys.platform == "win32":
            roots = [os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                     os.environ.get("LOCALAPPDATA")]
            for root in filter(None, roots):
                candidates.extend([
                    os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
                    os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
                ])
        else:
            candidates = ["google-chrome", "chromium", "chromium-browser", "microsoft-edge"]
        for exe in candidates:
            if os.path.exists(exe) or _which(exe):
                try:
                    subprocess.Popen([exe, f"--app={url}", "--window-size=1180,900"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except OSError:
                    continue
    try:
        webbrowser.open(url)
    except Exception:                                  # noqa: BLE001
        pass


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def _port_busy(port: int) -> bool:
    """Windows 下 SO_REUSEADDR 允许重复绑定同一端口，需主动探测避免双实例分流请求。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.35)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    outdir: Path | None = None,
    open_browser: bool = False,
    app_mode: bool = False,
    runtime: str | None = None,
) -> None:
    """启动本机服务。PC 与 Android 调的是同一个函数，只是参数不同。"""
    global _DEFAULT_OUTDIR
    if _port_busy(port):
        print(f"端口 {port} 已被占用：可能已有一个随手存正在运行。"
              f" 请直接使用 http://127.0.0.1:{port}/ ，或换端口加 --port {port + 1}",
              flush=True)
        raise SystemExit(1)
    current = runtime or os.environ.get("MEDIADL_RUNTIME") or "pc"
    with _LOCK:
        _STATE.update({"host": host, "port": port, "runtime": current})

    if outdir:
        _DEFAULT_OUTDIR = Path(outdir).expanduser().resolve()
    _DEFAULT_OUTDIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)

    local_url = f"http://127.0.0.1:{port}"
    print("\n" + "=" * 62)
    print(f"  随手存 v{__version_short__} · 一个应用，PC / Android 各自本机运行")
    print(f"  运行位置  : {'Android APK' if current == 'android' else 'Windows EXE'}")
    print(f"  界面地址  : {local_url}/")
    print("  下载方式  : " + ("Java 原生写入手机存储" if current == "android" else "交给浏览器下载管理器"))
    print("  自动识别 · 免 Cookie · 免代理 · 可选去除水印 · 结果可预览")
    print("  Ctrl+C 退出")
    print("=" * 62 + "\n", flush=True)

    if open_browser or app_mode:
        threading.Timer(0.8, lambda: _open_ui(local_url + "/", app_mode)).start()
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    run_server()
