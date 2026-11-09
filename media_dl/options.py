"""下载参数与 yt-dlp 选项构造。"""

from __future__ import annotations

import os
import re as _re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .links import DESKTOP_UA, MOBILE_UA

# 用户手动粘贴的抖音登录态 Cookie 只会发往抖音自身域名，绝不上传到任何服务器。
# 这些域名都写进 Netscape cookies.txt，yt-dlp 按域名匹配下发，从而突破图文 / 登录作品的风控。
_COOKIE_DOMAINS = (".douyin.com", ".amemv.com", ".snssdk.com", ".tiktok.com", ".ixigua.com")
_COOKIE_CACHE: dict[str, str] = {}


def write_cookie_file(cookie_str: str) -> "str | None":
    """把用户粘贴的原始 Cookie 头（"a=b; c=d" 或带 "Cookie: " 前缀）转成 Netscape
    cookies.txt，供 yt-dlp 的 cookiefile 使用；无法解析时返回 None。
    按内容缓存到临时文件，避免每次解析都重写。"""
    raw = (cookie_str or "").strip()
    if not raw:
        return None
    raw = _re.sub(r"^[Cc]ookie:\s*", "", raw).strip()
    pairs = [p.strip() for p in raw.split(";") if p.strip()]
    if not pairs:
        return None
    cache_key = raw
    if cache_key in _COOKIE_CACHE and os.path.exists(_COOKIE_CACHE[cache_key]):
        return _COOKIE_CACHE[cache_key]
    lines = ["# Netscape HTTP Cookie File", ""]
    for p in pairs:
        if "=" not in p:
            continue
        name, _, value = p.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if not name:
            continue
        for domain in _COOKIE_DOMAINS:
            lines.append("\t".join([domain, "TRUE", "/", "FALSE", "0", name, value]))
    if len(lines) <= 2:
        return None
    fd, path = tempfile.mkstemp(prefix="media_dl_cookies_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    _COOKIE_CACHE[cache_key] = path
    return path


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def select_quality_url(raw: dict, quality: "str | None") -> "str | None":
    """从 entry.raw 的 quality_variants 里挑选符合「画质上限」的直链。

    返回被选中的 url 字符串，或 None（调用方再按默认最高画质逻辑处理）。

    - quality 为 'best' / 空 / 非数字  -> 返回 None（交给默认最高画质）
    - 有精确匹配分辨率        -> 该分辨率直链
    - 无精确匹配但存在更低画质 -> 取最接近且不高于上限的那条（variants 已降序）
    - 请求的画质低于全部可选   -> 取最低画质兜底（无法再降）
    """
    if not quality or not str(quality).isdigit():
        return None
    variants = (raw or {}).get("quality_variants") if isinstance(raw, dict) else None
    if not variants:
        return None
    h = int(quality)
    for v in variants:
        if int(v.get("height") or 0) == h:
            return str(v["url"])
    lower = [v for v in variants if (int(v.get("height") or 0)) <= h]
    if lower:
        return str(lower[0]["url"])
    return str(variants[-1]["url"])


class _SilentLogger:
    """yt-dlp 的报错在解析阶段属于正常兜底路径，由上层统一收集后展示。"""
    def debug(self, msg: str) -> None: pass
    def info(self, msg: str) -> None: pass
    def warning(self, msg: str) -> None: pass
    def error(self, msg: str) -> None: pass


@dataclass
class DownloadOptions:
    outdir: Path = field(default_factory=lambda: Path.cwd() / "downloads")
    quality: str = "best"            # best | 2160 | 1080 | 720 | 480 | 360
    audio_only: bool = False
    audio_format: str = "mp3"
    write_thumbnail: bool = False
    merge_format: str = "mp4"
    max_images: int = 30             # 单个图集 / 网页最多下载多少张
    page_images: bool = True         # 解析失败时是否从网页抓图片
    remove_watermark: bool = True    # 勾选后优先无水印直链；关闭时保留平台原始文件
    cookies_browser: str = ""        # chrome | edge | firefox | brave | opera
    cookie_file: str = ""            # Netscape 格式 cookies.txt
    cookie: str = ""                 # 用户手动粘贴的抖音登录态 Cookie（仅本机保存，发往抖音）
    proxy: str = ""
    timeout: float = 20.0
    retries: int = 3
    concurrent_fragments: int = 4
    expand_shortlinks: bool = True

    def normalized(self) -> "DownloadOptions":
        self.outdir = Path(self.outdir).expanduser().resolve()
        self.outdir.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownloadOptions":
        allowed = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in (data or {}).items() if k in allowed and v is not None}
        if "outdir" in clean:
            clean["outdir"] = Path(str(clean["outdir"]))
        for bool_key in ("audio_only", "write_thumbnail", "page_images",
                         "expand_shortlinks", "remove_watermark"):
            if bool_key in clean and not isinstance(clean[bool_key], bool):
                clean[bool_key] = str(clean[bool_key]).lower() in ("1", "true", "yes", "on")
        for int_key in ("max_images", "retries", "concurrent_fragments"):
            if int_key in clean:
                try:
                    clean[int_key] = int(clean[int_key])
                except (TypeError, ValueError):
                    clean.pop(int_key)
        return cls(**clean).normalized()

    def to_dict(self) -> dict[str, Any]:
        return {
            "outdir": str(self.outdir),
            "quality": self.quality,
            "audio_only": self.audio_only,
            "audio_format": self.audio_format,
            "write_thumbnail": self.write_thumbnail,
            "merge_format": self.merge_format,
            "max_images": self.max_images,
            "page_images": self.page_images,
            "remove_watermark": self.remove_watermark,
            "cookies_browser": self.cookies_browser,
            "cookie_file": self.cookie_file,
            "cookie": self.cookie,
            "proxy": self.proxy,
            "timeout": self.timeout,
            "retries": self.retries,
        }


def ytdlp_opts(
    options: DownloadOptions,
    platform: str = "generic",
    *,
    extract_only: bool = False,
    progress_hooks: list[Callable[[dict], None]] | None = None,
) -> dict[str, Any]:
    """按当前参数构造 yt-dlp 配置。"""
    opts = options.normalized()
    subdir = platform if platform != "generic" else "other"
    outtmpl = str(opts.outdir / subdir / "%(title).80B_%(id)s.%(ext)s")

    if opts.audio_only:
        fmt = "ba/b"
    elif not has_ffmpeg():
        # 没有 ffmpeg 时无法合并「视频轨 + 音频轨」。B 站等平台只提供分离轨，
        # 写死 "b"（要求含音轨的单一文件）会直接报 "Requested format is not available"。
        # 这里在「带音轨的单文件 / 最佳视频轨 / 任意」之间逐级回退，保证总能拿到流。
        fmt = "b[acodec!=none]/bv*[acodec!=none]/b/bv*/best"
    elif opts.quality and opts.quality.isdigit():
        h = int(opts.quality)
        fmt = f"bv*[height<={h}]+ba/b[height<={h}]/b"
    else:
        fmt = "bv*+ba/b"

    ydl: dict[str, Any] = {
        "format": fmt,
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "logger": _SilentLogger(),
        "noplaylist": False,
        "ignoreerrors": False,
        "retries": opts.retries,
        "fragment_retries": opts.retries,
        "socket_timeout": opts.timeout,
        "concurrent_fragment_downloads": opts.concurrent_fragments,
        "merge_output_format": opts.merge_format,
        "writethumbnail": opts.write_thumbnail,
        "overwrites": False,
        "continuedl": True,
        "noprogress": True,
        "http_headers": {
            "User-Agent": MOBILE_UA if platform in ("douyin", "kuaishou", "xiaohongshu") else DESKTOP_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    }
    if extract_only:
        ydl["skip_download"] = True
    else:
        ydl["progress_hooks"] = progress_hooks or []
    if opts.audio_only and has_ffmpeg():
        ydl["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": opts.audio_format,
            "preferredquality": "192",
        }]
    if opts.cookies_browser:
        ydl["cookiesfrombrowser"] = (opts.cookies_browser, None, None, None)
    if opts.cookie_file:
        ydl["cookiefile"] = opts.cookie_file
    elif opts.cookie:
        # 用户手动粘贴的抖音登录态 Cookie：转成 Netscape 文件交给 yt-dlp，
        # 用于免 Cookie 通道取不到的抖音图文 / 登录作品（仅发往抖音，不上传）
        cookie_path = write_cookie_file(opts.cookie)
        if cookie_path:
            ydl["cookiefile"] = cookie_path
    if opts.proxy:
        ydl["proxy"] = opts.proxy
    return ydl
