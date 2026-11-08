"""浏览器下载交付层。

网页端（PC / 手机）不再把文件写进服务端的 downloads/ 目录，也不再展示
「已下载文件」列表。解析结果被封成一个短时效的一次性票据，页面拿到的是
下载按钮；点击后由服务端流式转发媒体直链，并以 Content-Disposition:
attachment 交给浏览器自己的下载管理器，文件落在浏览器默认下载目录
（手机浏览器则落到手机本地）。

设计要点：
- 票据只保存解析阶段得到的直链，接口不接受任意 URL，避免变成开放代理；
- 视频可带多条候选 CDN 直链，首个不可达自动切换；
- 图集打包成 zip 一次交付；
- 没有直链（yt-dlp 来源）时临时下载到系统临时目录再转交，响应结束即清理，
  全程不碰 downloads/。
"""

from __future__ import annotations

import re
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlparse

import requests
from flask import Response

from .links import DESKTOP_UA, MOBILE_UA, safe_filename
from .models import MediaEntry
from .options import DownloadOptions, ytdlp_opts, select_quality_url

_TICKET_TTL = 15 * 60          # 15 分钟过期，直链本身也有时效
_MAX_TICKETS = 120
_TICKETS: dict[str, "Ticket"] = {}
_LOCK = threading.RLock()

_MIME_EXT = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "video/3gpp": ".3gp",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/webm": ".webm",
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/bmp": ".bmp", "image/avif": ".avif",
    "image/heic": ".heic", "image/tiff": ".tiff",
}
_KNOWN_EXT = set(_MIME_EXT.values()) | {".jpeg"}
_WATERMARK = re.compile(r"playwm|/playwm/|watermark=1", re.I)


@dataclass
class Resource:
    """一个待下载文件；urls 是同一文件的多个候选直链（CDN 节点 / 画质）。"""
    urls: list[str]
    name: str
    referer: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    kind: str = "video"


@dataclass
class DeliveryItem:
    id: str
    title: str
    kind: str
    kind_name: str
    platform: str
    platform_name: str
    watermark_free: bool
    source: str
    resources: list[Resource] = field(default_factory=list)
    page_url: str = ""                       # 无直链时交给 yt-dlp 临时下载
    cover: str = ""                          # 封面 / 首图，供结果栏预览
    options: DownloadOptions | None = None


@dataclass
class Ticket:
    created: float
    items: list[DeliveryItem]


# --------------------------------------------------------------------------- #
# 解析结果 -> 票据
# --------------------------------------------------------------------------- #
def _ext_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in _KNOWN_EXT else ""


def _clean_stem(title: str, fallback: str) -> str:
    stem = safe_filename(str(title or ""), 80) or fallback
    if Path(stem).suffix.lower() in _KNOWN_EXT:
        stem = Path(stem).stem
    return stem or fallback


def _video_candidates(entry: MediaEntry, remove_watermark: bool = True) -> list[str]:
    raw = entry.raw or {}
    if not remove_watermark:
        # 用户取消「去除水印」：优先平台原始（可能带水印）的官方保存地址
        kept = [str(u) for u in (raw.get("watermarked_video_urls") or []) if u]
        if kept:
            return kept
        all_urls = [str(u) for u in (raw.get("video_urls") or []) if u]
        return sorted(all_urls, key=lambda u: not bool(_WATERMARK.search(u)))
    urls = [str(u) for u in (raw.get("original_video_urls") or raw.get("video_urls") or []) if u]
    primary = str(raw.get("direct_video_url") or "")
    if primary and primary not in urls:
        urls.insert(0, primary)
    ytdlp_url = str(raw.get("ytdlp_url") or "")
    if ytdlp_url and ytdlp_url not in urls:
        urls.append(ytdlp_url)
    # 无水印优先：playwm / watermark=1 的地址一律排到最后
    urls.sort(key=lambda u: bool(_WATERMARK.search(u)))
    return urls


def _resource_from_entry(entry: MediaEntry, opts: DownloadOptions | None = None) -> list[Resource]:
    raw = entry.raw or {}
    remove_watermark = bool(getattr(opts, "remove_watermark", True)) if opts else True
    stem = _clean_stem(entry.title, "media")
    referer = str(raw.get("referer") or entry.url or "")
    headers = {str(k): str(v) for k, v in (raw.get("ytdlp_headers") or {}).items()}

    if entry.kind in ("image", "gallery") and entry.images:
        out: list[Resource] = []
        for index, url in enumerate(entry.images, start=1):
            name = f"{index:02d}_{stem}" if len(entry.images) > 1 else stem
            out.append(Resource(urls=[url], name=name, referer=referer,
                                headers=headers, kind="image"))
        return out

    urls = _video_candidates(entry, remove_watermark)
    # 画质增强：设置了具体「画质上限」时把对应直链提到候选首位，其余作 CDN 兜底
    quality = getattr(opts, "quality", "best") if opts else "best"
    chosen = select_quality_url(raw, quality)
    if chosen:
        urls = [chosen] + [u for u in urls if u != chosen]
    if urls:
        return [Resource(urls=urls, name=stem, referer=referer,
                         headers=headers, kind="video")]
    return []


def item_from_entry(entry: MediaEntry, opts: DownloadOptions) -> DeliveryItem:
    remove_watermark = bool(getattr(opts, "remove_watermark", True))
    return DeliveryItem(
        id=entry.id,
        title=entry.title,
        kind=entry.kind,
        kind_name=entry.kind_name,
        platform=entry.platform,
        platform_name=entry.platform_name,
        watermark_free=bool(entry.raw.get("watermark_free")) and remove_watermark,
        source=entry.source,
        resources=_resource_from_entry(entry, opts),
        page_url=entry.url,
        cover=str(entry.thumbnail or ""),
        options=opts,
    )


def _gc() -> None:
    now = time.time()
    for key in [k for k, v in _TICKETS.items() if now - v.created > _TICKET_TTL]:
        _TICKETS.pop(key, None)
    while len(_TICKETS) > _MAX_TICKETS:
        _TICKETS.pop(next(iter(_TICKETS)), None)


def create_ticket(entries: list[MediaEntry], opts: DownloadOptions) -> tuple[str, list[dict[str, Any]]]:
    """把解析结果存成票据，返回给前端可渲染的下载项摘要。"""
    items = [item_from_entry(e, opts) for e in entries]
    ticket = secrets.token_urlsafe(18)
    with _LOCK:
        _gc()
        _TICKETS[ticket] = Ticket(created=time.time(), items=items)

    summary: list[dict[str, Any]] = []
    link = f"ticket={quote(ticket, safe='')}"
    for index, item in enumerate(items):
        count = len(item.resources) or 1
        remove_watermark = bool(getattr(item.options, "remove_watermark", True))
        summary.append({
            "index": index,
            "id": item.id,
            "title": item.title,
            "kind": item.kind,
            "kind_name": item.kind_name,
            "platform": item.platform,
            "platform_name": item.platform_name,
            "watermark_free": item.watermark_free,
            "watermark_option": "remove" if remove_watermark else "keep",
            "source": item.source,
            "count": count,
            "mode": "zip" if count > 1 else ("direct" if item.resources else "ytdlp"),
            "download_url": f"/api/fetch?{link}&i={index}",
            "preview_url": f"/api/fetch?{link}&i={index}&j=0&zip=0&preview=1",
            "thumb_url": f"/api/thumb?{link}&i={index}",
            "preview_kind": "image" if item.kind in ("image", "gallery") else "video",
        })
    return ticket, summary


def get_item(ticket: str, index: int) -> DeliveryItem:
    with _LOCK:
        entry = _TICKETS.get(ticket)
        if not entry or time.time() - entry.created > _TICKET_TTL:
            _TICKETS.pop(ticket, None)
            raise LookupError("下载链接已过期，请重新解析")
        if index < 0 or index >= len(entry.items):
            raise LookupError("下载项不存在")
        return entry.items[index]


# --------------------------------------------------------------------------- #
# 流式转发
# --------------------------------------------------------------------------- #
def _session(resource: Resource, platform: str, direct: bool) -> requests.Session:
    s = requests.Session()
    if direct:
        s.trust_env = False
    s.headers.update({
        "User-Agent": MOBILE_UA if platform in ("douyin", "kuaishou", "xiaohongshu") else DESKTOP_UA,
        "Referer": resource.referer or "",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if resource.headers:
        s.headers.update(resource.headers)
    return s


def _open(resource: Resource, platform: str,
          range_header: str = "") -> tuple[requests.Response, requests.Session, str]:
    """逐条尝试候选直链；系统代理不通时自动直连重试。

    预览时转发 Range，让 <video> 可以拖动进度；源站不支持 Range 时自动降级为
    完整请求，不影响播放。
    """
    last: Exception | None = None
    extra = {"Range": range_header} if range_header else None
    for url in resource.urls:
        for direct in (False, True):
            session = _session(resource, platform, direct)
            try:
                resp = session.get(url, stream=True, timeout=(10, 60),
                                   allow_redirects=True, headers=extra)
                if range_header and resp.status_code >= 400:
                    resp.close()
                    resp = session.get(url, stream=True, timeout=(10, 60),
                                       allow_redirects=True)
                if resp.status_code >= 400:
                    resp.close()
                    session.close()
                    last = RuntimeError(f"HTTP {resp.status_code}")
                    continue
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                return resp, session, ctype
            except Exception as exc:                 # noqa: BLE001 - 换节点 / 换直连
                session.close()
                last = exc
    raise RuntimeError(f"媒体直链不可用：{str(last or '无可用地址')[:160]}")


def _final_name(resource: Resource, content_type: str, url: str) -> str:
    ext = (_MIME_EXT.get(content_type) or _ext_from_url(url)
           or (".mp4" if resource.kind == "video" else ".jpg"))
    return safe_filename(resource.name, 80) + ext


def _disposition(filename: str, inline: bool = False) -> str:
    ascii_name = re.sub(r"[^ -~]", "_", filename).replace('"', "_").replace("\\", "_")
    kind = "inline" if inline else "attachment"
    return (f'{kind}; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}")


def _single_response(resource: Resource, item: DeliveryItem,
                     preview: bool = False, range_header: str = "") -> Response:
    resp, session, ctype = _open(resource, item.platform,
                                 range_header if preview else "")
    url = resp.url or resource.urls[0]
    filename = _final_name(resource, ctype, url)
    headers = {
        "Content-Disposition": _disposition(filename, inline=preview),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    length = resp.headers.get("Content-Length")
    if length and length.isdigit() and not resp.headers.get("Content-Encoding"):
        headers["Content-Length"] = length
    if preview:
        headers["Accept-Ranges"] = "bytes"
        for name in ("Content-Range", "Accept-Ranges", "Content-Type"):
            if resp.headers.get(name):
                headers[name] = resp.headers[name]

    def generate() -> Iterator[bytes]:
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            resp.close()
            session.close()

    return Response(generate(), status=resp.status_code if preview else 200,
                    mimetype=ctype or "application/octet-stream", headers=headers)


def _zip_response(item: DeliveryItem) -> Response:
    """图集打包成 zip，一次交给浏览器。临时文件落在系统临时目录，用完即删。"""
    buf = tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024)
    written = 0
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as zf:
        for resource in item.resources:
            resp = None
            session = None
            try:
                resp, session, ctype = _open(resource, item.platform)
                name = _final_name(resource, ctype, resp.url or resource.urls[0])
                info = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
                info.compress_type = zipfile.ZIP_STORED       # 图片本身已压缩，避免重复耗时
                with zf.open(info, "w", force_zip64=True) as out:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            out.write(chunk)
                written += 1
            except Exception:                                  # noqa: BLE001 - 跳过坏图
                continue
            finally:
                if resp is not None:
                    resp.close()
                if session is not None:
                    session.close()
    if not written:
        buf.close()
        raise RuntimeError("图集下载失败：所有图片地址都不可用")
    buf.seek(0)
    zip_name = _clean_stem(item.title, "images") + ".zip"
    headers = {"Content-Disposition": _disposition(zip_name), "Cache-Control": "no-store"}
    size = buf.seek(0, 2)
    buf.seek(0)
    headers["Content-Length"] = str(size)

    def generate() -> Iterator[bytes]:
        try:
            while True:
                chunk = buf.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            buf.close()

    return Response(generate(), mimetype="application/zip", headers=headers)


def _ytdlp_response(item: DeliveryItem) -> Response:
    """没有直链的站点：临时用 yt-dlp 下载到系统临时目录，再转交给浏览器。"""
    import yt_dlp

    workdir = Path(tempfile.mkdtemp(prefix="media_dl_browser_"))
    opts = item.options or DownloadOptions()
    ydl_opts = ytdlp_opts(opts, item.platform)
    ydl_opts["outtmpl"] = str(workdir / "%(title).80B_%(id)s.%(ext)s")
    ydl_opts["quiet"] = True
    ydl_opts["no_warnings"] = True
    ydl_opts["noprogress"] = True
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([item.page_url])
    except Exception as exc:                                   # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError(f"下载失败：{str(exc)[:200]}") from exc

    files = [p for p in workdir.rglob("*")
             if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not files:
        shutil.rmtree(workdir, ignore_errors=True)
        raise RuntimeError("下载失败：没有产生文件")
    path = max(files, key=lambda p: p.stat().st_size)
    suffix = path.suffix.lower()
    ctype = next((k for k, v in _MIME_EXT.items() if v == suffix), "application/octet-stream")
    headers = {
        "Content-Disposition": _disposition(path.name),
        "Content-Length": str(path.stat().st_size),
        "Cache-Control": "no-store",
    }

    def generate() -> Iterator[bytes]:
        try:
            with path.open("rb") as fp:
                while True:
                    chunk = fp.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return Response(generate(), mimetype=ctype, headers=headers)


def fetch(ticket: str, index: int, file_index: int = -1, as_zip: bool = False,
          preview: bool = False, range_header: str = "") -> Response:
    """生成下载响应；preview=True 时以 inline 返回，供结果栏预览图片 / 视频。"""
    item = get_item(ticket, index)
    if not item.resources:
        return _ytdlp_response(item)
    if preview:
        which = file_index if file_index >= 0 else 0
        if which >= len(item.resources):
            raise LookupError("预览序号不存在")
        return _single_response(item.resources[which], item, preview=True,
                                range_header=range_header)
    if len(item.resources) > 1 and as_zip:
        return _zip_response(item)
    if file_index >= 0:
        if file_index >= len(item.resources):
            raise LookupError("图片序号不存在")
        return _single_response(item.resources[file_index], item)
    if len(item.resources) > 1:
        return _zip_response(item)
    return _single_response(item.resources[0], item)


# 1x1 透明 PNG，封面取不到时占位，避免结果栏出现破图
def _blank_png() -> bytes:
    import struct

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + \
            struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
            + chunk(b"IEND", b""))


_BLANK_PNG = _blank_png()


def fetch_thumbnail(ticket: str, index: int) -> Response:
    """结果栏封面：优先解析结果自带的封面，图集则用第一张图。"""
    item = get_item(ticket, index)
    resource: Resource | None = None
    if item.cover:
        resource = Resource(urls=[item.cover], name="cover",
                            referer=item.page_url, kind="image")
    elif item.resources and item.resources[0].kind == "image":
        resource = item.resources[0]
    if not resource:
        return Response(_BLANK_PNG, mimetype="image/png")

    resp = None
    session = None
    try:
        resp, session, ctype = _open(resource, item.platform)
        data = b""
        for chunk in resp.iter_content(chunk_size=65536):
            data += chunk
            if len(data) > 4 * 1024 * 1024:
                break
        media_type = ctype or "image/jpeg"
    except Exception:                                  # noqa: BLE001
        return Response(_BLANK_PNG, mimetype="image/png")
    finally:
        if resp is not None:
            resp.close()
        if session is not None:
            session.close()
    if not data or not media_type.startswith("image/"):
        return Response(_BLANK_PNG, mimetype="image/png")
    return Response(data, mimetype=media_type,
                    headers={"Cache-Control": "no-store"})
