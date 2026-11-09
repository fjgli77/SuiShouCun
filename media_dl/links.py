"""链接清洗、短链展开与「平台 / 媒体类型」自动识别。

分享口令通常是这样一段文本：

    8.87 复制打开抖音，看看【某某的作品】... https://v.douyin.com/abc123/ 复制此链接...

本模块负责把这类文本还原成干净的 URL，并在完全不联网的情况下判断
平台（抖音 / 快手 / B站 ...）与媒体类型（视频 / 图片 / 图集）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 中文分享文案里常见的结尾标点，不能算进 URL
_TRAILING_JUNK = "），。！？、；：…】》」』,;!?)>\"'\\"
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；！？）】》]+", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"(?<![\w/])(?:www\.)?[\w-]+\.(?:com|cn|tv|net|me|app)(?:/[^\s<>]*)?", re.IGNORECASE)

_SHORT_HOSTS = {
    "v.douyin.com", "v.kuaishou.com", "b23.tv", "bili2233.cn", "t.cn",
    "dwz.cn", "url.cn", "m.tb.cn", "xhslink.com", "sourl.cn",
}

_PLATFORM_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("douyin", "抖音", ("douyin.com", "iesdouyin.com", "amemv.com", "douyinvod.com",
                        "snssdk.com", "douyinpic.com", "byteimg.com", "pstatp.com", "aweme.cn")),
    ("bilibili", "哔哩哔哩", ("bilibili.com", "b23.tv", "bili2233.cn", "bilibili.tv", "hdslb.com", "bilicdn")),
    ("kuaishou", "快手", ("kuaishou.com", "kuaishou.cn", "kwai.com", "kwai.cn", "chenzhongtech.com",
                          "kuaishouapp.com", "gifshow.com", "kwimgs.com", "yximgs.com")),
    ("xiaohongshu", "小红书", ("xiaohongshu.com", "xhslink.com", "xhscdn.com")),
    ("weibo", "微博", ("weibo.com", "weibo.cn", "sinaimg.cn", "t.cn")),
    ("weixin", "微信", ("mp.weixin.qq.com", "weixin.qq.com")),
    ("youtube", "YouTube", ("youtube.com", "youtu.be")),
    ("twitter", "X / Twitter", ("twitter.com", "x.com", "t.co")),
    ("instagram", "Instagram", ("instagram.com",)),
    ("tiktok", "TikTok", ("tiktok.com",)),
    ("zhihu", "知乎", ("zhihu.com",)),
    ("pipixia", "皮皮虾", ("pipix.com",)),
]

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".avif", ".tiff", ".tif")
_VIDEO_EXT = (".mp4", ".m3u8", ".mov", ".webm", ".mkv", ".flv", ".avi")

KIND_NAMES = {
    "video": "视频",
    "image": "图片",
    "gallery": "图集",
    "audio": "音频",
    "page": "网页",
    "unknown": "待识别",
}


@dataclass
class Link:
    raw: str                    # 用户输入的原始 URL
    url: str                    # 展开短链后的 URL
    platform: str               # 平台 key，未知为 "generic"
    platform_name: str
    kind: str = "video"         # video | gallery | image | page
    shortlink: bool = False     # 是否由短链展开而来

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, "待识别")

    def describe(self) -> str:
        return f"[{self.platform_name}] {self.kind_name} {self.url}"


def extract_urls(text: str) -> list[str]:
    """从任意文本（含整段分享口令、多行批量链接）中提取 URL。"""
    if not text:
        return []
    found: list[str] = []
    for raw in _URL_RE.findall(text):
        cleaned = _clean_url(raw)
        if cleaned and cleaned not in found:
            found.append(cleaned)
    if not found:
        # 退一步：允许没有 http:// 前缀的裸域名
        for raw in _BARE_URL_RE.findall(text):
            cleaned = _clean_url("https://" + raw)
            if cleaned not in found:
                found.append(cleaned)
    return found


def _clean_url(url: str) -> str:
    url = url.strip().rstrip(_TRAILING_JUNK)
    # 分享文本里常见的尾部转义字符
    url = url.replace("\\/", "/")
    while url.endswith(("/", "#", "?", "&", "=")):
        url = url[:-1]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return url


def detect_platform(url: str) -> tuple[str, str]:
    host = (urlparse(url).netloc or "").lower()
    for key, name, needles in _PLATFORM_RULES:
        if any(host == n or host.endswith("." + n) or n in host for n in needles):
            return key, name
    return "generic", "通用网页"


def guess_kind(url: str, platform: str) -> str:
    """仅凭 URL 形态判断媒体类型，不联网。真实类型以解析器结果为准。

    覆盖：图片/视频直链、抖音视频与图文、快手视频与图集、B站稿件与动态、小红书笔记等。
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    whole = (path + "?" + query)

    # 1. 直链最明确，优先判断
    if path.endswith(_IMAGE_EXT) or re.search(r"\.(jpe?g|png|webp|gif|heic|avif)", path):
        return "image"
    if path.endswith(_VIDEO_EXT) or re.search(r"\.(mp4|m3u8|mov|webm|mkv|flv)(\?|$)", whole):
        return "video"

    # 2. 平台特征
    if platform == "douyin":
        if any(k in whole for k in ("/note/", "/slide/", "/album/", "/gallery/",
                                    "type=image", "image_list", "images=", "modal_id=")) and "/video/" not in path:
            return "gallery"
        if any(k in whole for k in ("/note/", "/slide/", "/album/")):
            return "gallery"
        return "video"
    if platform == "kuaishou":
        if any(k in whole for k in ("atlas", "picture", "/photo/", "imageurl", "multi_photo")):
            return "gallery"
        return "video"
    if platform == "bilibili":
        if any(k in whole for k in ("/opus/", "/dynamic/", "t.bilibili.com", "/read/", "opus/")):
            return "gallery"
        return "video"
    if platform == "xiaohongshu":
        return "gallery" if not any(k in whole for k in ("/video/", "type=video")) else "video"
    if platform == "weibo":
        return "gallery" if any(k in whole for k in ("/large/", "pic_ids", "m.weibo.cn")) else "video"

    # 3. 通用网页里的图片线索
    if any(k in whole for k in ("/image/", "/photo/", "/picture/", "/images/", "imageview", "imagemogr")):
        return "image"
    return "video"


def detect_media(text: str, expand: bool = False, timeout: float = 8.0) -> list[dict]:
    """纯本地识别：返回每个链接的平台与媒体类型，不发网络请求（默认）。

    用于界面上「粘贴即识别」，结果包含中文标签，可直接渲染。
    """
    return [
        {
            "raw": link.raw,
            "url": link.url,
            "platform": link.platform,
            "platform_name": link.platform_name,
            "kind": link.kind,
            "kind_name": link.kind_name,
            "shortlink": link.shortlink,
        }
        for link in prepare(text, expand=expand, timeout=timeout)
    ]


def _js_redirect(html: str, base: str) -> str:
    """有些短链页面不发 30x，而是用 JS 或 meta refresh 跳转。"""
    patterns = (
        r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+(?:url|URL)\s*=\s*["\']?([^"\'>\s;]+)',
        r'location\.(?:href|replace)\s*(?:=\s*|\(\s*)["\']([^"\']+)["\']',
        r'window\.location\s*=\s*["\']([^"\']+)["\']',
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
    )
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            candidate = urljoin(base, m.group(1).strip())
            if candidate.rstrip("/") != base.rstrip("/") and urlparse(candidate).scheme in ("http", "https"):
                return candidate
    return ""


def expand_shortlink(url: str, timeout: float = 10.0, session: requests.Session | None = None) -> tuple[str, bool]:
    """展开短链，返回 (最终URL, 是否发生了展开)。失败时原样返回，交给解析器兜底。"""
    host = (urlparse(url).netloc or "").lower()
    if host not in _SHORT_HOSTS and not any(host.endswith("." + h) for h in _SHORT_HOSTS):
        return url, False
    sess = session or requests.Session()
    # Accept-Encoding: identity —— 短链服务器 / 代理偶发下发编码标记错乱的压缩体，
    # raw.read(decode_content=True) 会抛出 zlib.error（且不属于 requests.RequestException，
    # 会穿透整个解析链路，前端表现为 "Error -3 while decompressing data"）。
    # 直接声明不接受压缩，从源头规避解压这一步。
    headers = {"User-Agent": MOBILE_UA, "Accept-Language": "zh-CN,zh;q=0.9",
               "Accept-Encoding": "identity"}
    try:
        resp = sess.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
        final = resp.url
        html = ""
        if "text/html" in resp.headers.get("Content-Type", ""):
            try:
                raw = resp.raw.read(262144, decode_content=True) if hasattr(resp.raw, "read") else b""
                html = raw.decode("utf-8", errors="ignore")
            except Exception:                          # noqa: BLE001 - 解压失败只丢 HTML，不丢短链
                html = ""
        resp.close()
        if html:
            target = _js_redirect(html, final)
            if target:
                final = target
        final = _clean_url(final) or url
        return final, final != url
    except Exception:                                  # noqa: BLE001 - 任何失败都原样返回，解析器自行展开
        return url, False


def prepare(text: str, expand: bool = True, timeout: float = 10.0) -> list[Link]:
    """把任意输入文本整理成 Link 列表（自动去重）。"""
    session = requests.Session() if expand else None
    links: list[Link] = []
    seen: set[str] = set()
    for raw in extract_urls(text):
        url, changed = expand_shortlink(raw, timeout=timeout, session=session) if expand else (raw, False)
        if url in seen:
            continue
        seen.add(url)
        platform, name = detect_platform(url)
        links.append(Link(
            raw=raw,
            url=url,
            platform=platform,
            platform_name=name,
            kind=guess_kind(url, platform),
            shortlink=changed,
        ))
    if session:
        session.close()
    return links


def split_lines(text: str) -> Iterable[str]:
    for line in (text or "").splitlines():
        if line.strip():
            yield line.strip()


def safe_filename(name: str, max_len: int = 90) -> str:
    """生成 Windows / macOS / Linux 都能用的文件名。"""
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = "".join(ch for ch in name if ord(ch) >= 32)
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "untitled"
