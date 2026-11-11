"""下载执行：视频交给 yt-dlp，图片/图集走带防盗链头的流式下载。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

import requests

from .extractor import parse_text
from .links import MOBILE_UA, DESKTOP_UA, safe_filename
from .models import DownloadResult, MediaEntry, ParseResult, Progress
from .options import DownloadOptions, ytdlp_opts, select_quality_url

OnProgress = Callable[[Progress], None]

# yt-dlp 会读写同目录文件，串行执行更稳妥
_VIDEO_LOCK = threading.Lock()

_MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/bmp": ".bmp", "image/avif": ".avif",
    "image/heic": ".heic", "image/tiff": ".tiff",
}
_KNOWN_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif", ".heic", ".tiff"}


def noop(_: Progress) -> None:
    pass


def download_text(
    text: str,
    options: DownloadOptions | None = None,
    on_progress: OnProgress | None = None,
) -> DownloadResult:
    """解析并下载一段文本中的所有链接，是最常用的一站式入口。"""
    opts = (options or DownloadOptions()).normalized()
    parsed = parse_text(text, opts)
    return download_parsed(parsed, opts, on_progress)


def download_parsed(
    parsed: list[ParseResult],
    options: DownloadOptions | None = None,
    on_progress: OnProgress | None = None,
) -> DownloadResult:
    opts = (options or DownloadOptions()).normalized()
    cb = on_progress or noop
    out = DownloadResult(parsed=list(parsed))

    for result in parsed:
        if not result.entries:
            if result.error:
                out.errors.append(f"{result.link.url}: {result.error}")
            continue
        for entry in result.entries:
            cb(Progress(entry_id=entry.id, status="pending", title=entry.title))
            try:
                files = download_entry(entry, opts, cb)
                out.files.extend(files)
            except Exception as exc:                  # noqa: BLE001 - 批量任务单条失败不中断
                msg = str(exc).replace("\n", " ").strip()[:300]
                out.errors.append(f"{entry.title}: {msg}")
                cb(Progress(entry_id=entry.id, status="error", title=entry.title, message=msg))
    return out


def download_entry(
    entry: MediaEntry,
    options: DownloadOptions | None = None,
    on_progress: OnProgress | None = None,
    cancel_event: "threading.Event | None" = None,
) -> list[str]:
    opts = (options or DownloadOptions()).normalized()
    cb = on_progress or noop
    if entry.raw.get("direct_video_url"):
        return _download_direct_video(entry, opts, cb, cancel_event)
    if entry.kind in ("image", "gallery") and entry.images:
        return _download_images(entry, opts, cb, cancel_event)
    return _download_video(entry, opts, cb)


def _download_direct_video(
    entry: MediaEntry, opts: DownloadOptions, cb: OnProgress,
    cancel_event: "threading.Event | None" = None,
) -> list[str]:
    """抖音 / 快手：已拿到媒体直链，带 Referer 流式下载。

    解析器会给出多个 CDN 节点与画质候选，某个节点失败时自动切换下一个，
    避免单条直链失效就让整个任务失败。
    """
    quality_tag = f"_{opts.quality}p" if str(opts.quality).isdigit() else ""
    base_urls = [str(u) for u in (entry.raw.get("video_urls") or []) if u]
    # 画质增强：设置了具体「画质上限」时，优先挑选最符合的直链，其余作为 CDN 兜底
    chosen = select_quality_url(entry.raw, opts.quality)
    if chosen:
        base_urls = [chosen] + [u for u in base_urls if u != chosen]
        primary = str(entry.raw.get("direct_video_url") or "")
        if primary and primary not in base_urls:
            base_urls.append(primary)
    else:
        primary = str(entry.raw.get("direct_video_url") or "")
        if primary and primary not in base_urls:
            base_urls.insert(0, primary)
    urls = base_urls or [primary]
    folder = opts.outdir / (entry.platform if entry.platform != "generic" else "other")
    folder.mkdir(parents=True, exist_ok=True)
    # 文件名带画质档位：换画质重下时是新文件，不会静默拿到旧画质的已存在文件
    target = folder / f"{safe_filename(entry.title or 'video', 70)}_{entry.id}{quality_tag}.mp4"
    session = _resource_session(entry, opts)
    session.headers.update({"Referer": str(entry.raw.get("referer") or entry.url)})

    state = {"total": 0, "done": 0, "last": 0.0}

    def on_start(total: int, _ctype: str) -> None:
        state["total"] = total

    def on_chunk(size: int, written: int, total: int) -> None:
        state["done"] = written
        state["total"] = total
        now = time.time()
        if now - state["last"] < 0.15 and written < total:
            return
        state["last"] = now
        cb(Progress(
            entry_id=entry.id, status="downloading", title=entry.title,
            percent=(written / total * 100) if total else 0.0,
            downloaded=written, total=total,
            message="正在下载视频（已续传）" if written > size else "正在下载视频",
        ))

    path: Path | None = None
    last_error: Exception | None = None
    try:
        for index, url in enumerate(urls, start=1):
            state.update({"total": 0, "done": 0, "last": 0.0})
            try:
                path, _ = _stream_to_file(
                    session, url, target, min_size=1024,
                    on_start=on_start, on_chunk=on_chunk,
                    cancel_event=cancel_event,
                )
                break
            except _Cancelled:
                # 取消：保留 .part 供下次续传，直接向上抛
                raise
            except Exception as exc:                  # noqa: BLE001 - 切换备用直链
                last_error = exc
                # 仅当不是可续传的中断（如网络闪断）时清 .part；
                # 网络类错误保留 .part，下次重试可续传
                if not _is_network_error(exc):
                    target.with_name(target.name + ".part").unlink(missing_ok=True)
                if index < len(urls):
                    cb(Progress(
                        entry_id=entry.id, status="downloading", title=entry.title,
                        message=f"直链 {index} 不可用，切换备用节点 {index + 1}/{len(urls)}",
                    ))
        if path is None:
            cb(Progress(entry_id=entry.id, status="error", title=entry.title,
                        message=str(last_error or "直链下载失败")[:300]))
            raise last_error or RuntimeError("直链下载失败")
    finally:
        session.close()
    cb(Progress(
        entry_id=entry.id, status="finished", title=entry.title,
        percent=100.0, message="下载完成", files=[str(path)],
    ))
    return [str(path)]


def _is_network_error(exc: Exception) -> bool:
    """网络类异常：保留 .part 供续传；其余（内容为空等）清掉重下。"""
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    text = str(exc).lower()
    return any(k in text for k in ("timeout", "connection", "ssl", "网络"))


# --------------------------------------------------------------------------- #
# 视频 / 音频
# --------------------------------------------------------------------------- #
def _download_video(entry: MediaEntry, opts: DownloadOptions, cb: OnProgress) -> list[str]:
    import yt_dlp

    finished: list[str] = []
    last_emit = 0.0

    def hook(d: dict) -> None:
        nonlocal last_emit
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            now = time.time()
            # 限流，避免高频刷新 UI
            if now - last_emit < 0.15 and done < total:
                return
            last_emit = now
            cb(Progress(
                entry_id=entry.id, status="downloading", title=entry.title,
                percent=(done / total * 100) if total else 0.0,
                downloaded=done, total=total,
                speed=d.get("speed") or 0.0, eta=d.get("eta"),
                message=f"正在下载 {d.get('filename', '')}",
            ))
        elif status == "finished":
            name = d.get("filename")
            if name:
                finished.append(name)

    target_dir = opts.outdir / (entry.platform if entry.platform != "generic" else "other")
    target_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in target_dir.iterdir() if p.is_file()}

    ydl_opts = ytdlp_opts(opts, entry.platform, progress_hooks=[hook])
    with _VIDEO_LOCK:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                ydl.download([entry.url])
            except Exception as exc:                  # noqa: BLE001
                cb(Progress(entry_id=entry.id, status="error", title=entry.title,
                            message=str(exc)[:300]))
                raise

    after = [p for p in target_dir.iterdir() if p.is_file() and p.name not in before]
    files = [str(p) for p in after]
    if not files:
        # 断点续传 / 文件已存在时没有"新增文件"，回退到 hook 里记录的产物
        files = [f for f in finished if Path(f).exists()]
    cb(Progress(
        entry_id=entry.id, status="finished", title=entry.title,
        percent=100.0, message="下载完成" if files else "下载结束（可能文件已存在）",
        files=files,
    ))
    return files


# --------------------------------------------------------------------------- #
# 图片 / 图集
# --------------------------------------------------------------------------- #
def _download_images(
    entry: MediaEntry, opts: DownloadOptions, cb: OnProgress,
    cancel_event: "threading.Event | None" = None,
) -> list[str]:
    folder_name = safe_filename(entry.title) or "images"
    folder = opts.outdir / (entry.platform if entry.platform != "generic" else "other") / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    session = _resource_session(
        entry, opts, "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    )
    urls = entry.images[: max(1, opts.max_images)]
    total = len(urls)
    files: list[str] = []
    failures: list[str] = []

    for index, url in enumerate(urls, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled("下载已取消")
        cb(Progress(
            entry_id=entry.id, status="downloading", title=entry.title,
            percent=(index - 1) / total * 100,
            message=f"图片 {index}/{total}",
        ))
        try:
            path = _save_one_image(session, url, folder, f"{index:02d}_{folder_name}",
                                   cancel_event=cancel_event)
            files.append(str(path))
        except _Cancelled:
            raise
        except Exception as exc:                      # noqa: BLE001
            failures.append(str(exc)[:120])
        cb(Progress(
            entry_id=entry.id, status="downloading", title=entry.title,
            percent=index / total * 100,
            message=f"图片 {index}/{total}（失败 {len(failures)}）",
        ))
    session.close()

    if not files:
        raise RuntimeError("图片全部下载失败：" + "; ".join(failures[:3]))
    cb(Progress(
        entry_id=entry.id, status="finished", title=entry.title,
        percent=100.0, message=f"完成 {len(files)}/{total} 张" + (f"，失败 {len(failures)} 张" if failures else ""),
        files=files,
    ))
    return files


def _resource_session(entry: MediaEntry, opts: DownloadOptions, accept: str = "*/*") -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": MOBILE_UA if entry.platform in ("douyin", "kuaishou", "xiaohongshu") else DESKTOP_UA,
        "Referer": entry.url or "",
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if opts.cookie_file:
        from http.cookiejar import MozillaCookieJar
        try:
            jar = MozillaCookieJar(opts.cookie_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies = jar
        except Exception:                              # noqa: BLE001
            pass
    if opts.proxy:
        session.proxies.update({"http": opts.proxy, "https": opts.proxy})
    return session


class _Cancelled(RuntimeError):
    """下载被用户取消：调用方据此保留 .part 以便下次续传。"""


def _stream_to_file(
    session: requests.Session,
    url: str,
    target: Path,
    *,
    min_size: int = 128,
    on_start=None,
    on_chunk=None,
    cancel_event: "threading.Event | None" = None,
) -> tuple[Path, str]:
    """流式落盘，返回 (文件路径, Content-Type)。

    - 已存在的完整文件直接跳过；
    - 存在同名 .part 时发 Range 请求续传（响应 206 续写，200 则清空重下）；
    - 提供 cancel_event 时，写块间隙检查取消信号，命中抛 _Cancelled 并保留 .part。
    """
    tmp = target.with_name(target.name + ".part")

    # ---- 已有 .part：先试探是否支持续传 ---- #
    part_size = tmp.stat().st_size if tmp.exists() else 0
    resumed = False
    headers: dict[str, str] = {}
    if part_size > 0:
        probe = session.get(url, stream=True, timeout=60, allow_redirects=True,
                            headers={"Range": f"bytes={part_size}-"})
        accepts_range = probe.status_code == 206
        probe.close()
        if accepts_range:
            headers["Range"] = f"bytes={part_size}-"
            resumed = True
        else:
            # 服务器不支持续传，清空 .part 从头下载
            tmp.unlink(missing_ok=True)
            part_size = 0

    resp = session.get(url, stream=True, timeout=60, allow_redirects=True,
                       headers=headers or None)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if resp.status_code == 206:
        total = part_size + int(resp.headers.get("Content-Range", "0/0").split("/")[-1] or 0)
    else:
        total = int(resp.headers.get("Content-Length") or 0)
        if total and part_size:
            # 服务器忽略 Range 返回完整内容：丢弃 .part 重下
            tmp.unlink(missing_ok=True)
            part_size = 0
            resumed = False
    if on_start:
        override = on_start(total, content_type)
        if override is not None:
            target = override
            tmp = target.with_name(target.name + ".part")
    if target.exists() and target.stat().st_size > min_size:
        resp.close()
        return target, content_type
    try:
        mode = "ab" if resumed else "wb"
        written_before = part_size if resumed else 0
        with tmp.open(mode) as fp:
            for chunk in resp.iter_content(chunk_size=65536):
                if cancel_event is not None and cancel_event.is_set():
                    raise _Cancelled("下载已取消")
                if chunk:
                    fp.write(chunk)
                    written_before += len(chunk)
                    if on_chunk:
                        # 续传时把已有字节计入总进度
                        on_chunk(len(chunk), written_before, total)
    finally:
        resp.close()
    if not tmp.exists() or tmp.stat().st_size < min_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("资源内容为空或被拦截")
    tmp.replace(target)
    return target, content_type


def _save_one_image(
    session: requests.Session, url: str, folder: Path, stem: str,
    cancel_event: "threading.Event | None" = None,
) -> Path:
    def on_start(_total: int, content_type: str) -> Path:
        ext = _MIME_EXT.get(content_type) or _ext_from_url(url) or ".jpg"
        return folder / f"{safe_filename(stem, 60)}{ext}"

    guess = folder / f"{safe_filename(stem, 60)}{_ext_from_url(url) or '.jpg'}"
    path, _ctype = _stream_to_file(
        session, url, guess, on_start=on_start, cancel_event=cancel_event,
        on_chunk=lambda size, _written, _total: None,
    )
    return path


def _ext_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in _KNOWN_EXT else ""


def iter_files(options: DownloadOptions | None = None) -> Iterable[Path]:
    opts = (options or DownloadOptions()).normalized()
    if not opts.outdir.exists():
        return []
    return sorted(
        (p for p in opts.outdir.rglob("*") if p.is_file() and not p.name.endswith(".part")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
