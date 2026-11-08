"""免 Cookie 解析器：抖音 / 快手公开数据。

抖音：
1. 主通道是移动端 App 推荐流 feed 接口（amemv / snssdk），不需要登录态、
   a_bogus 签名、SecSDK 指纹或任何代理，直接返回作品的播放直链与图集原图。
   2026 年起 www.douyin.com/video/ 改成纯 CSR 空壳，分享页 _ROUTER_DATA 也常
   只返回 serverToken 而无作品数据；yt-dlp 的 PC Web 详情接口则被 Argus 网关
   403 拦截（报 "Fresh cookies ... are needed"），所以不能依赖这两条路。
2. 分享页 SSR（_ROUTER_DATA / RENDER_DATA / 页面内嵌 mp4）作为降级。
快手：分享页 __APOLLO_STATE__ / 页面内嵌 mp4。

所有请求都不加载 cookies.txt、不读取浏览器登录态；系统代理不可用时会
自动直连重试，用户无需配置任何 Cookie 或代理。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
from typing import Any, Iterator
from urllib.parse import unquote, urljoin, urlparse

import requests

from .links import Link, safe_filename
from .models import MediaEntry
from .options import DownloadOptions

# 只认 https 的可下载资源
_HTTP = re.compile(r"^https?://", re.I)
_WATERMARK = re.compile(r"playwm|/playwm/|watermark=1", re.I)

# 抖音移动端 App 推荐流协议：不需要登录 Cookie、不需要 a_bogus / SecSDK 签名，
# 也不经过 PC Web 的 Argus 风控网关。2026 年抖音把 www.douyin.com/video/ 改成了
# 纯 CSR 空壳，分享页 _ROUTER_DATA 也只对部分请求下发作品数据，所以这条是主通道。
# 模块级变量，便于自检时指向本地模拟服务，避免离线测试访问外网
FEED_ENDPOINTS = (
    "https://api5-normal-c-hl.amemv.com/aweme/v1/feed/",
    "https://aweme.snssdk.com/aweme/v1/feed/",
)
_APP_UA = (
    "com.ss.android.ugc.aweme/110101 (Linux; U; Android 13; en; Pixel 7; "
    "Build/TQ3A.230901.001; Cronet/TTNetVersion:8f1c5b2e)"
)
_SPIDER_UA = "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)"


def parse_nocookie(link: Link, opts: DownloadOptions) -> list[MediaEntry]:
    """返回免登录解析结果；无法解析时返回空列表，由上层继续兜底。"""
    if link.platform == "douyin":
        return _douyin(link, opts)
    if link.platform == "kuaishou":
        return _kuaishou(link, opts)
    return []


# --------------------------------------------------------------------------- #
# 页面 / 内嵌 JSON
# --------------------------------------------------------------------------- #
def _pages(link: Link, opts: DownloadOptions, extra_urls: list[str]) -> Iterator[tuple[str, str, str]]:
    """依次尝试若干候选页面，产出 (html, final_url, title)。"""
    from .extractor import fetch_page                 # 延迟导入，避免模块循环
    seen: set[str] = set()
    for url in [link.url, *extra_urls]:
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            yield fetch_page(url, link, opts)
        except Exception:                             # noqa: BLE001 - 单个候选失败继续下一个
            continue


def _embedded_json(body: str) -> list[Any]:
    """从页面里抽出所有内嵌 JSON 对象（含 URL 编码的 RENDER_DATA）。"""
    out: list[Any] = []
    decoder = json.JSONDecoder()
    keys = ("_ROUTER_DATA", "__APOLLO_STATE__", "__INITIAL_STATE__", "__NUXT__",
            "__NEXT_DATA__", "videoInfoRes", "pageProps", "INIT_STATE")
    for key in keys:
        start = 0
        while True:
            idx = body.find(key, start)
            if idx < 0:
                break
            brace = body.find("{", idx)
            if brace >= 0:
                try:
                    obj, _ = decoder.raw_decode(body[brace:])
                    out.append(obj)
                except (ValueError, RecursionError):
                    pass
            start = idx + len(key)
    # 抖音老版页面把整个 JSON 做 percent-encoding 后放进 RENDER_DATA
    m = re.search(r'id=["\']RENDER_DATA["\'][^>]*>([^<]+)</script>', body, re.I)
    if m:
        try:
            out.append(json.loads(unquote(m.group(1).strip())))
        except (ValueError, TypeError):
            pass
    return out


def _iter_dicts(obj: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    if depth > 30:
        return
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_dicts(value, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _iter_dicts(value, depth + 1)


def _str_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = html_lib.unescape(value.strip()).replace("\\/", "/").replace("\\u0026", "&")
    if value.startswith("//"):
        value = "https:" + value
    return value if _HTTP.match(value) else ""


def _url_list(value: Any) -> list[str]:
    """处理 url_list / 单字符串 / [{url:...}] 三种形态。"""
    if isinstance(value, str):
        u = _str_url(value)
        return [u] if u else []
    if isinstance(value, dict):
        for key in ("url_list", "urls", "url", "src", "download_url"):
            if key in value:
                return _url_list(value[key])
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_url_list(item))
        return out
    return []


def _pick(urls: list[str]) -> str:
    """优先 https、优先无水印地址。"""
    clean = [u for u in dict.fromkeys(urls) if u]
    clean.sort(key=lambda u: (not u.startswith("https://"), bool(_WATERMARK.search(u))))
    return clean[0] if clean else ""


def _strip_watermark(url: str) -> str:
    """抖音 play_addr 里的 playwm 换成 play 即为无水印源。"""
    return url.replace("/playwm/", "/play/").replace("playwm", "play")


def _stable_id(value: str) -> str:
    return hashlib.md5(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _parse_height(gear_name: Any) -> int | None:
    """从抖音 bit_rate.gear_name（如 '1080p' / '540p'）解析分辨率高度。"""
    if not gear_name:
        return None
    m = re.search(r"(\d{3,4})", str(gear_name))
    return int(m.group(1)) if m else None


def _feed_quality_variants(video: dict[str, Any], remove_watermark: bool = True) -> list[dict[str, Any]]:
    """按画质降序返回候选直链 [{height, url, watermark_free}]，供「画质上限」筛选。

    仅收集平台下发的 /play/ 无水印源（去水印关闭时收集带水印的 download_addr），
    分辨率从 bit_rate.gear_name（如 '1080p'）解析；解析不出高度的地址不进入画质表，
    但仍会出现在 video_urls 里作为兜底。
    """
    seen: set[str] = set()
    variants: list[dict[str, Any]] = []
    bitrates = [b for b in (video.get("bit_rate") or []) if isinstance(b, dict)]
    bitrates.sort(key=lambda b: int(b.get("bit_rate") or 0), reverse=True)
    for br in bitrates:
        height = _parse_height(br.get("gear_name"))
        if height is None:
            continue
        for group in (br.get("play_addr"), video.get("play_addr_h264"), video.get("play_addr")):
            url = _pick(_url_list(group))
            if not url:
                continue
            u = _strip_watermark(url) if remove_watermark else url
            if u in seen:
                break
            seen.add(u)
            variants.append({"height": height, "url": u, "watermark_free": remove_watermark})
            break
    return variants


def _title(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"#\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return safe_filename(text, 70) or fallback


def _media_id(link: Link) -> str:
    url = link.url
    for pattern in (r"/(?:video|note|slide|album|share/video)/(\d{8,})",
                    r"(?:modal_id|item_ids|aweme_id|photoId|photo_id)=(\d{8,})",
                    r"/short-video/([A-Za-z0-9_-]{6,})",
                    r"/(?:fw|profile)/photo/(\d+)",
                    r"/([A-Za-z0-9]{10,})(?:/|\?|$)?"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return ""


def _video_entry(link: Link, url: str, title: str, uploader: str = "",
                 cover: str = "", watermark_free: bool = False,
                 strip: bool = True) -> MediaEntry:
    """strip=False 时保留平台原始地址（可能带水印），用于「不去除水印」选项。"""
    return MediaEntry(
        id=_stable_id(link.url),
        title=_title(title, "video"),
        kind="video",
        url=link.url,
        platform=link.platform,
        platform_name=link.platform_name,
        thumbnail=cover,
        uploader=uploader,
        raw={
            "source": "nocookie",
            "direct_video_url": _strip_watermark(url) if strip else url,
            "referer": link.url,
            "watermark_free": watermark_free,
        },
    )


def _gallery_entry(link: Link, images: list[str], title: str, uploader: str = "") -> MediaEntry:
    unique = list(dict.fromkeys(u for u in images if u))
    if not unique:
        raise ValueError("没有可用图片地址")
    return MediaEntry(
        id=_stable_id(link.url),
        title=_title(title, "images"),
        kind="gallery" if len(unique) > 1 else "image",
        url=link.url,
        platform=link.platform,
        platform_name=link.platform_name,
        thumbnail=unique[0],
        uploader=uploader,
        images=unique,
        raw={"source": "nocookie", "watermark_free": True},
    )


# --------------------------------------------------------------------------- #
# 抖音：移动端 Feed 主通道（免 Cookie / 免签名 / 免代理）
# --------------------------------------------------------------------------- #
def _api_session(opts: DownloadOptions, direct: bool = False) -> requests.Session:
    """direct=True 时忽略系统代理环境变量，用于代理不可用时的直连重试。"""
    session = requests.Session()
    if direct:
        session.trust_env = False
    session.headers.update({
        "User-Agent": _APP_UA,
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if opts.proxy:
        session.proxies.update({"http": opts.proxy, "https": opts.proxy})
    return session


def _douyin_feed(media_id: str, opts: DownloadOptions) -> dict[str, Any] | None:
    """请求移动端推荐流，返回 aweme_id 精确匹配的作品对象。

    feed 接口会顺带返回若干推荐作品，必须按 ID 精确匹配，不能取 aweme_list[0]
    就往下下，否则可能下载到别人的视频。
    """
    if not media_id or not media_id.isdigit():
        return None
    for endpoint in FEED_ENDPOINTS:
        for direct in (False, True):
            session = _api_session(opts, direct=direct)
            try:
                resp = session.get(
                    endpoint, params={"aweme_id": media_id, "aid": 1128},
                    timeout=min(opts.timeout, 15),
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:                          # noqa: BLE001 - 换节点 / 换直连重试
                continue
            finally:
                session.close()
            for item in data.get("aweme_list") or []:
                if isinstance(item, dict) and str(item.get("aweme_id") or "") == media_id:
                    return item
            # 节点可用但没收录该作品：换下一个节点，不再重复直连
            break
    return None


def _feed_video_urls(video: dict[str, Any], remove_watermark: bool = True) -> list[str]:
    """按画质优先级收集播放直链，首个失败时下载器会自动切换下一个 CDN 节点。

    remove_watermark=True  : /play/ 无水印源优先，带水印的 download_addr 排最后；
    remove_watermark=False : 保留平台原始地址，带水印的官方保存地址优先（用户选择不去水印）。
    """
    bitrates = [b for b in (video.get("bit_rate") or []) if isinstance(b, dict)]
    h264: list[dict[str, Any]] = []
    h265: list[dict[str, Any]] = []
    for br in bitrates:
        codec = str(br.get("codec_type") or "").lower()
        (h265 if br.get("is_h265") or codec in ("h265", "hevc") else h264).append(br)
    by_rate = lambda b: int(b.get("bit_rate") or 0)   # noqa: E731
    h264.sort(key=by_rate, reverse=True)
    h265.sort(key=by_rate, reverse=True)

    # play_addr / bit_rate 走 /play/ 通道，是平台下发的无水印源；
    # download_addr 对应「保存到本地」按钮，通常叠加了平台水印，只作最后兜底。
    primary = [b.get("play_addr") for b in h264]
    primary += [video.get(key) for key in ("play_addr_h264", "play_addr")]
    primary += [b.get("play_addr") for b in h265]
    primary += [video.get("play_addr_265"), video.get("play_addr_lowbr")]
    fallback = [video.get("download_addr")]

    clean: list[str] = []
    fallback_urls: list[str] = []

    def collect(groups: list[Any], sink: list[str], strip: bool) -> None:
        for group in groups:
            for raw in _url_list(group):
                url = _strip_watermark(raw) if strip else raw
                if url and url not in clean and url not in fallback_urls:
                    sink.append(url)

    collect(primary, clean, remove_watermark)
    collect(fallback, fallback_urls, False)   # download_addr 保持平台原始地址
    if remove_watermark:
        # 无水印直链优先，带水印的保存地址只在无水印源全部失效时才被使用
        return clean + [u for u in fallback_urls if u not in clean]
    # 用户取消「去除水印」：直接用平台下发的原始保存地址
    return fallback_urls + [u for u in clean if u not in fallback_urls]


def _feed_images(item: dict[str, Any]) -> list[str]:
    """图集：只取 url_list（末项为无水印原图），跳过带水印的 download_url_list。"""
    images = item.get("images")
    if not images:
        post = item.get("image_post_info") or {}
        images = (post.get("images") or post.get("image_list")) if isinstance(post, dict) else None
    urls: list[str] = []
    for image in images or []:
        # 同一个 url_list 里是同一张图的多个 CDN 节点，只取末项（无水印原图），
        # 否则图集会出现大量重复图片
        if isinstance(image, dict):
            cand = _url_list(image.get("url_list") or image.get("url"))
        else:
            cand = _url_list(image)
        if cand:
            urls.append(cand[-1])
    return [u for u in dict.fromkeys(urls)
            if u and not re.search(r"(avatar|logo|icon|watermark)", u, re.I)]


def _feed_entry(link: Link, item: dict[str, Any],
                opts: DownloadOptions | None = None) -> MediaEntry | None:
    desc = item.get("desc") or ""
    author = ((item.get("author") or {}).get("nickname") or "")
    video = item.get("video") or {}
    cover = _pick(_url_list(video.get("origin_cover")) or _url_list(video.get("cover")))
    duration_ms = item.get("duration") or video.get("duration") or 0
    strip = bool(getattr(opts, "remove_watermark", True))

    images = _feed_images(item)
    video_urls = _feed_video_urls(video, strip)
    if images and not video_urls:
        entry = _gallery_entry(link, images, desc, author)
        entry.raw["channel"] = "mobile-feed"
        entry.raw["watermark_free"] = strip
        return entry
    if not video_urls:
        return None

    entry = _video_entry(link, video_urls[0], desc, author, cover,
                         watermark_free=strip, strip=strip)
    try:
        entry.duration = float(duration_ms) / 1000 or None
    except (TypeError, ValueError):
        pass
    entry.raw.update({
        "channel": "mobile-feed",
        "direct_video_url": video_urls[0],
        "video_urls": video_urls[:8],
        # 画质增强：按 bit_rate.gear_name 标注的每档分辨率直链，供下载时按上限筛选
        "quality_variants": _feed_quality_variants(video, strip),
        # 交付层按「是否去除水印」开关在两组候选里选择
        "watermarked_video_urls": _feed_video_urls(video, False)[:8],
        "original_video_urls": _feed_video_urls(video, True)[:8],
        "watermark_free": strip,
        "watermark_option": "remove" if strip else "keep",
        "nowatermark_reason": ("移动端 /play/ 无水印源，已排除带水印的下载地址"
                               if strip else "按设置保留平台原始文件（可能带水印）"),
        "quality": f"{len(video_urls)} 条候选直链（优先 H.264 / 无水印）",
    })
    return entry


# --------------------------------------------------------------------------- #
# 抖音：分享页 SSR 兜底
# --------------------------------------------------------------------------- #
def _douyin(link: Link, opts: DownloadOptions) -> list[MediaEntry]:
    media_id = _media_id(link)

    # 1. 移动端 Feed：当前抖音主站与分享页大多只返回空壳，这条才是主通道
    item = _douyin_feed(media_id, opts)
    if item:
        try:
            entry = _feed_entry(link, item, opts)
            if entry:
                return [entry]
        except (ValueError, TypeError, AttributeError):
            pass

    # 2. 分享页内嵌 JSON / mp4 直链（对部分作品与旧版页面仍然有效）
    candidates: list[str] = []
    if media_id and media_id.isdigit():
        candidates = [
            f"https://www.iesdouyin.com/share/video/{media_id}/",
            f"https://www.iesdouyin.com/share/note/{media_id}/",
            f"https://www.douyin.com/video/{media_id}",
        ]

    for body, final_url, page_title in _pages(link, opts, candidates):
        objs = _embedded_json(body)
        for item in _douyin_items(objs):
            try:
                entry = _douyin_build(link, item, page_title, opts)
            except (ValueError, TypeError, AttributeError):
                continue
            if entry:
                return [entry]

        # 内嵌 JSON 没拿到，退一步找页面里的 mp4 直链
        from .extractor import video_urls_from_html        # 延迟导入
        videos = [u for u in video_urls_from_html(body, final_url)
                  if not re.search(r"\.(js|css)(\?|$)", u, re.I)]
        if videos:
            return [_video_entry(link, videos[0], page_title)]
    return []


def _douyin_items(objs: list[Any]) -> Iterator[dict[str, Any]]:
    seen: set[int] = set()
    for obj in objs:
        for d in _iter_dicts(obj):
            il = d.get("item_list")
            if isinstance(il, list) and il and isinstance(il[0], dict):
                if id(il[0]) not in seen:
                    seen.add(id(il[0]))
                    yield il[0]
            aw = d.get("aweme_detail")
            if isinstance(aw, dict) and id(aw) not in seen:
                seen.add(id(aw))
                yield aw
        for d in _iter_dicts(obj):
            if ("play_addr" in d or "images" in d) and (
                "desc" in d or "aweme_id" in d or "share_info" in d
            ) and id(d) not in seen:
                seen.add(id(d))
                yield d


def _douyin_build(link: Link, item: dict[str, Any], page_title: str,
                  opts: DownloadOptions | None = None) -> MediaEntry | None:
    strip = bool(getattr(opts, "remove_watermark", True))
    desc = item.get("desc") or (item.get("share_info") or {}).get("share_desc") or page_title
    author = ((item.get("author") or {}).get("nickname")
              or (item.get("author_info") or {}).get("nickname") or "")
    cover = _pick(_url_list((item.get("video") or {}).get("cover"))
                  or _url_list(item.get("cover")))

    # 图集优先：抖音图文作品 images[] 每项带 url_list
    images = item.get("images")
    if isinstance(images, list) and images:
        urls: list[str] = []
        for im in images:
            urls.extend(_url_list(im.get("url_list") if isinstance(im, dict) else im))
        urls = [u for u in urls if not re.search(r"(avatar|logo|icon)", u, re.I)]
        if urls:
            return _gallery_entry(link, urls, desc, author)

    video = item.get("video") or {}
    # 不去水印：优先平台官方「保存到本地」的原始文件（叠加了平台水印）
    if not strip:
        raw_url = _pick(_url_list(video.get("download_addr")))
        if raw_url:
            entry = _video_entry(link, raw_url, desc, author, cover,
                                 watermark_free=False, strip=False)
            entry.raw.update({
                "video_urls": _feed_video_urls(video, False)[:8],
                "watermarked_video_urls": _feed_video_urls(video, False)[:8],
                "original_video_urls": _feed_video_urls(video, True)[:8],
                "quality_variants": _feed_quality_variants(video, False),
                "watermark_option": "keep",
            })
            return entry
    # play* 是 /play/ 无水印通道；download_addr 带平台水印，只作最后兜底
    for key in ("play_addr", "play_addr_h264", "play_addr_265"):
        url = _pick(_url_list(video.get(key)))
        if url:
            entry = _video_entry(link, url, desc, author, cover,
                                 watermark_free=strip, strip=strip)
            entry.raw["quality_variants"] = _feed_quality_variants(video, strip)
            return entry
    for br in video.get("bit_rate") or []:
        if isinstance(br, dict):
            url = _pick(_url_list(br.get("play_addr")))
            if url:
                entry = _video_entry(link, url, desc, author, cover,
                                     watermark_free=strip, strip=strip)
                entry.raw["quality_variants"] = _feed_quality_variants(video, strip)
                return entry
    url = _pick(_url_list(video.get("download_addr")))
    if url:
        entry = _video_entry(link, url, desc, author, cover,
                            watermark_free=False, strip=strip)
        entry.raw["quality_variants"] = _feed_quality_variants(video, False)
        return entry
    return None


# --------------------------------------------------------------------------- #
# 快手
# --------------------------------------------------------------------------- #
def _kuaishou(link: Link, opts: DownloadOptions) -> list[MediaEntry]:
    for body, final_url, page_title in _pages(link, opts, []):
        objs = _embedded_json(body)
        for photo in _kuaishou_photos(objs):
            try:
                entry = _kuaishou_build(link, photo, page_title, opts)
            except (ValueError, TypeError, AttributeError):
                continue
            if entry:
                return [entry]

        from .extractor import video_urls_from_html        # 延迟导入
        videos = [u for u in video_urls_from_html(body, final_url)
                  if not re.search(r"\.(js|css)(\?|$)", u, re.I)]
        if videos:
            return [_video_entry(link, videos[0], page_title)]
    return []


def _kuaishou_photos(objs: list[Any]) -> Iterator[dict[str, Any]]:
    wanted = ("playUrl", "mainMvUrls", "imageUrls", "atlas", "photoUrl", "caption")
    seen: set[int] = set()
    for obj in objs:
        for d in _iter_dicts(obj):
            if any(k in d for k in wanted) and id(d) not in seen:
                seen.add(id(d))
                yield d


def _kuaishou_build(link: Link, photo: dict[str, Any], page_title: str,
                    opts: DownloadOptions | None = None) -> MediaEntry | None:
    strip = bool(getattr(opts, "remove_watermark", True))
    title = photo.get("caption") or photo.get("photoCaption") or page_title
    author = ((photo.get("user") or {}).get("name")
              or (photo.get("author") or {}).get("userName") or "")
    cover = _pick(_url_list(photo.get("coverUrls"))
                  or _url_list(photo.get("coverUrl"))
                  or _url_list(photo.get("photoUrl")))

    # 图集：imageUrls 或 ext_params.atlas（后者常是一段 JSON 字符串）
    image_urls: list[str] = []
    image_urls.extend(_url_list(photo.get("imageUrls")))
    ext = photo.get("ext_params") or {}
    if isinstance(ext, dict):
        ext = ext.get("atlas") or {}
    for atlas in (photo.get("atlas"), ext):
        if isinstance(atlas, str) and atlas.strip().startswith(("{", "[")):
            try:
                parsed = json.loads(atlas)
                image_urls.extend(_url_list(parsed.get("list") if isinstance(parsed, dict) else parsed))
                if isinstance(parsed, dict):
                    for key in ("images", "cdn", "urls", "atlas"):
                        image_urls.extend(_url_list(parsed.get(key)))
            except ValueError:
                pass
        elif isinstance(atlas, (list, dict)):
            image_urls.extend(_url_list(atlas.get("list") if isinstance(atlas, dict) else atlas))
    image_urls = [u for u in dict.fromkeys(image_urls)
                  if u and not re.search(r"(avatar|logo|icon)", u, re.I)]
    if image_urls:
        return _gallery_entry(link, image_urls, title, author)

    for key in ("playUrl", "mainMvUrls", "mainMvUrl", "videoUrl", "downloadUrl"):
        pool = _url_list(photo.get(key))
        if strip:
            pool = [u for u in pool if not _WATERMARK.search(u)]
        url = _pick(pool)
        if url:
            entry = _video_entry(link, url, title, author, cover,
                                 watermark_free=strip and key not in ("downloadUrl", "mainMvUrl"),
                                 strip=strip)
            if not strip:
                entry.raw.update({
                    "video_urls": [u for u in _url_list(photo.get(key)) if u][:8],
                    "watermarked_video_urls": [u for u in _url_list(photo.get(key)) if u][:8],
                    "watermark_option": "keep",
                })
            return entry
    return None
