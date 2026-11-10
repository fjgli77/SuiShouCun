"""统一解析器：yt-dlp 为主，页面图片提取为补充兜底。

支持三类输入：
1. 视频页面链接（抖音 / B站 / 快手 / 小红书 / 微博 / YouTube ...）
2. 图集、动态、笔记（多张图片）
3. 图片直链、普通网页里的图片
"""

from __future__ import annotations

import html as html_lib
import json
import re
import threading
import time
from http.cookiejar import MozillaCookieJar
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests

from .links import DESKTOP_UA, MOBILE_UA, Link, prepare, safe_filename
from .models import MediaEntry, ParseResult
from .nocookie import parse_nocookie
from .options import DownloadOptions, ytdlp_opts

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".avif", ".tif", ".tiff")
_IMAGE_EXT_NAMES = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "heic", "avif", "tif", "tiff"}
# 明显不是内容图的地址，直接过滤
_IMAGE_SKIP = re.compile(
    r"(logo|icon|avatar|placeholder|qrcode|sprite|emoji|blank|loading|default_cover|"
    r"favicon|1x1|blank\.gif|share_icon|btn_|banner_tip)", re.I
)


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def parse_text(text: str, options: DownloadOptions | None = None) -> list[ParseResult]:
    """解析任意文本（可包含整段分享口令、多行链接、短链）。"""
    opts = (options or DownloadOptions()).normalized()
    links = prepare(text, expand=opts.expand_shortlinks, timeout=opts.timeout)
    results: list[ParseResult] = []
    for link in links:
        try:
            results.append(parse_link(link, opts))
        except Exception as exc:                    # noqa: BLE001 - 单条失败不拖垮整批解析
            bad = ParseResult(link=link)
            bad.error = _friendly(exc)              # 用户可见文案：不含英文异常类名
            bad.warnings.append(f"技术细节：{_brief(exc)}")   # 原始异常留在 warning，供折叠区排查
            results.append(bad)
    return results


def parse_link(link: Link, options: DownloadOptions | None = None) -> ParseResult:
    """解析单个链接；解析完成后把确认的媒体类型回写到 Link，供界面展示。"""
    result = _parse_link_impl(link, options)
    if result.entries:
        link.kind = result.media_type
    return result


def _parse_link_impl(link: Link, options: DownloadOptions | None = None) -> ParseResult:
    opts = (options or DownloadOptions()).normalized()
    result = ParseResult(link=link)

    # 1. 图片直链，无需动用解析器
    if link.kind == "image" or _path_is_image(link.url):
        result.entries = [MediaEntry(
            id=_stable_id(link.url),
            title=safe_filename(_filename_from_url(link.url) or "image"),
            kind="image",
            url=link.url,
            platform=link.platform,
            platform_name=link.platform_name,
            images=[link.url],
            raw={"source": "direct"},
        )]
        return result

    # 2. 免 Cookie 解析（抖音 / 快手优先，yt-dlp 对这两家支持有限）
    if link.platform in ("douyin", "kuaishou"):
        try:
            entries = parse_nocookie(link, opts, warnings=result.warnings)
        except Exception as exc:                       # noqa: BLE001
            entries = []
            result.warnings.append(f"免登录解析未成功：{_brief(exc)}")
        if entries:
            result.entries = entries
            channel = (entries[0].raw or {}).get("channel")
            result.warnings.append(
                "移动端直连通道解析成功（免 Cookie / 免签名 / 免代理）"
                if channel == "mobile-feed"
                else "页面公开数据解析成功（免 Cookie / 免代理）"
            )
            return result

    # 3. B站番剧 / 剧集：yt-dlp 的 Web 提取器常因大会员限制只返回封面，
    #    这里先走公开的 PGC 接口拿 ep_id -> cid -> 免 Cookie 标清直链
    if link.platform == "bilibili" and "/bangumi/play/" in link.url:
        bangumi, why = _bilibili_bangumi(link, opts)
        if bangumi:
            result.entries = [bangumi]
            result.warnings.append("B站番剧公开接口解析成功（免 Cookie）")
            return result
        if why:
            result.warnings.append(why)

    # 3b. B站普通视频：走 durl 渐进式接口直接拿「含音轨的单文件 MP4」，
    #     不依赖 ffmpeg 合并（yt-dlp 在无 ffmpeg 时对 B站 会直接失败）
    if link.platform == "bilibili" and "/video/" in link.url:
        bili, why = _bilibili_video(link, opts)
        if bili:
            result.entries = [bili]
            result.warnings.append("B站视频公开接口解析成功（免 Cookie 直链）")
            return result
        if why:
            result.warnings.append(why)

    # 4. yt-dlp 主引擎（默认不带任何 Cookie / 代理）
    try:
        entries = _parse_with_ytdlp(link, opts)
        if entries:
            result.entries = entries
            return result
    except Exception as exc:                       # noqa: BLE001 - 需要兜底到页面提取
        result.warnings.append(f"备用解析器未识别：{_friendly(exc)}")

    # 5. 明确的视频页面（/video/、/bangumi/、/short-video/ 等）解析失败时，
    #    绝不能把网页封面 / og:image 当成「图片」结果返回——这正是番剧链接
    #    被误判成图片的原因。此时直接给出失败原因。
    definite_video = _is_definite_video_page(link)

    # 6. B站动态 / 图文专用接口（best effort）
    imgs, title = _bilibili_dynamic(link, opts)
    if imgs and not definite_video:
        result.entries = [_gallery_entry(link, imgs, title, opts)]
        return result

    # 7. 页面里的视频直链（快手兜底链路，其他站点同样适用）
    direct = _video_from_page(link, opts)
    if direct:
        result.entries = [direct]
        result.warnings.append("通过页面公开地址解析（免 Cookie）")
        return result

    # 8. 通用网页图片提取（仅对非视频页面启用）
    if opts.page_images and not definite_video:
        try:
            imgs, title = extract_images_from_page(link.url, link, opts)
            if imgs:
                result.entries = [_gallery_entry(link, imgs, title, opts)]
                return result
            result.warnings.append("页面中未找到可下载的图片")
        except Exception as exc:                   # noqa: BLE001
            result.warnings.append(f"页面解析失败：{_brief(exc)}")
    elif definite_video:
        result.warnings.append("该链接是视频页面，已跳过网页图片兜底，避免把封面误当成图片")

    if not result.error:
        # 挑选用户可读的 warning 作理由：跳过「技术细节：」这类给排查用的原始异常行
        readable = [w for w in result.warnings if not w.startswith("技术细节：")]
        if link.platform in ("douyin", "kuaishou"):
            result.error = (
                "该作品未能通过免 Cookie 通道获取（平台风控或移动端通道暂未收录）。"
                "可稍后重试，或改用 App 里复制的完整分享短链；全程无需登录 Cookie 与代理。"
            )
        elif definite_video:
            reason = readable[-1] if readable else "视频流未返回"
            result.error = (
                f"该视频页面未能解析出可下载的视频：{reason}。"
                "常见原因是内容需要登录 / 大会员、地区限制或平台风控；"
                "本项目全程免 Cookie、免代理，可换用公开视频链接或稍后重试。"
            )
        else:
            reason = readable[0] if readable else "无法识别该链接"
            result.error = reason + "；请确认链接可公开访问、未过期，或稍后重试（全流程免 Cookie、免代理）"
    return result


_DEFINITE_VIDEO = re.compile(
    r"(?:bilibili\.com/(?:video/|bangumi/play/|festival/)|"
    r"douyin\.com/(?:video/|slide/)|kuaishou\.com/short-video/|"
    r"youtube\.com/watch|youtu\.be/|ixigua\.com/\d+)", re.I
)


def _is_definite_video_page(link: Link) -> bool:
    """链接形态已经明确是视频详情页时，封面图不能被当作解析结果。"""
    if link.kind == "image" or _path_is_image(link.url):
        return False
    if _DEFINITE_VIDEO.search(link.url):
        return True
    return link.platform == "bilibili" and link.kind == "video"


def _bilibili_video(link: Link, opts: DownloadOptions) -> tuple[MediaEntry | None, str]:
    """B站普通视频（/video/BVxxx）：web-interface/view 拿 cid，playurl 走
    fnval=1 的 durl 通道，直接返回**含音轨的单文件 MP4**，无需 ffmpeg 合并。

    这是「本机未安装 ffmpeg」时唯一可用的高画质方案：
      - dash（fnval=16）只给分离的视频轨 + 音频轨，没有 ffmpeg 无法合并；
      - durl（fnval=1）给渐进式单文件（含音轨），可直接下载播放。
    匿名只下发 720P/360P；带登录 Cookie 时可达 1080P（本函数自动复用 opts.cookie）。

    返回 (条目, 失败原因)。失败原因用于上层给出准确提示。
    """
    m = re.search(r"/video/(BV[0-9A-Za-z]+)", link.url)
    if not m:
        return None, ""
    bvid = m.group(1)
    headers = {"User-Agent": DESKTOP_UA, "Referer": "https://www.bilibili.com/",
               "Accept": "application/json"}
    proxies = {"http": opts.proxy, "https": opts.proxy} if opts.proxy else None
    cookie = getattr(opts, "cookie", "") or ""
    if cookie:
        headers["Cookie"] = cookie
    try:
        view = requests.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid}, headers=headers, timeout=min(opts.timeout, 15),
            proxies=proxies,
        ).json()
    except Exception as exc:                        # noqa: BLE001
        return None, f"B站视频信息接口不可用：{_brief(exc)}"
    if str(view.get("code") or 0) not in ("0", ""):
        return None, (f"B站视频接口返回 {view.get('code')}："
                      f"{str(view.get('message') or '无权限')[:60]}")
    data = view.get("data") or {}
    cid = str(data.get("cid") or "")
    if not cid:
        return None, "B站视频信息里没有 cid（可能是合集 / 已失效）"
    title = safe_filename(str(data.get("title") or "") or "bilibili_video", 90)
    cover = str(data.get("pic") or "")
    duration = data.get("duration") or None

    # 画质阶梯：优先 1080P（80）→ 720P（64）→ 480P（32）→ 360P（16）
    urls: list[str] = []
    quality = 0
    last_code: Any = ""
    for qn in (80, 64, 32, 16):
        try:
            play = requests.get(
                "https://api.bilibili.com/x/player/playurl",
                params={"bvid": bvid, "cid": cid, "qn": qn, "fnval": 1, "fnver": 0,
                        "fourk": 1, "otype": "json"},
                headers={**headers, "Referer": link.url},
                timeout=min(opts.timeout, 15), proxies=proxies,
            ).json()
        except Exception as exc:                    # noqa: BLE001
            last_code = _brief(exc)
            continue
        last_code = play.get("code")
        pdata = play.get("data") or {}
        durls = pdata.get("durl") or []
        for d in durls:
            u = str(d.get("url") or "")
            if u.startswith("http"):
                urls.append(u)
            backup = d.get("backup_url")
            if isinstance(backup, str) and backup.startswith("http"):
                urls.append(backup)
            elif isinstance(backup, list):
                urls.extend(str(x) for x in backup if str(x).startswith("http"))
        if urls:
            quality = int(pdata.get("quality") or qn)
            break
    urls = [u for u in urls if u.startswith("http")]
    if not urls:
        hint = "可能需要登录 / 大会员或地区限制" if not cookie else "该视频暂无可下载的清晰度"
        return None, (f"B站播放地址未返回视频流（code={last_code}，{hint}）；"
                      "可在「下载设置 → 抖音登录态 Cookie」填入已登录 B站的 Cookie 后重试获取更高画质")

    qlabel = {120: "4K", 116: "1080P60", 112: "1080P+", 80: "1080P",
              64: "720P", 32: "480P", 16: "360P"}.get(quality, f"{quality}")
    entry = MediaEntry(
        id=_stable_id(f"{link.url}#{cid}"),
        title=title,
        kind="video",
        url=link.url,
        platform=link.platform,
        platform_name=link.platform_name,
        thumbnail=cover,
        duration=duration,
        raw={
            "source": "bilibili-durl",
            "direct_video_url": urls[0],
            "video_urls": urls[:4],
            "ytdlp_headers": {"Referer": "https://www.bilibili.com/",
                              "User-Agent": DESKTOP_UA},
            "referer": "https://www.bilibili.com/",
            "watermark_free": True,
            "channel": "bili-durl",
            "quality": qlabel,
            "quality_variants": [{"height": quality, "label": qlabel,
                                  "url": urls[0], "ext": "mp4"}],
        },
    )
    return entry, ""


def _bilibili_bangumi(link: Link, opts: DownloadOptions) -> tuple[MediaEntry | None, str]:
    """B站番剧 ep 链接：PGC season 接口拿 cid，playurl 接口拿免登录标清流。

    返回 (条目, 失败原因)。失败时给出具体原因，绝不回落去抓页面封面图。
    """
    m = re.search(r"/bangumi/play/ep(\d+)", link.url)
    if not m:
        return None, ""
    ep_id = m.group(1)
    headers = {"User-Agent": DESKTOP_UA, "Referer": "https://www.bilibili.com/",
               "Accept": "application/json"}
    proxies = {"http": opts.proxy, "https": opts.proxy} if opts.proxy else None
    try:
        season = requests.get(
            "https://api.bilibili.com/pgc/view/web/season",
            params={"ep_id": ep_id}, headers=headers, timeout=min(opts.timeout, 15),
        ).json()
    except Exception as exc:                        # noqa: BLE001
        return None, f"B站番剧信息接口不可用：{_brief(exc)}"

    if str(season.get("code") or 0) not in ("0", ""):
        return None, (f"B站番剧接口返回 {season.get('code')}："
                      f"{str(season.get('message') or '无权限')[:60]}")
    payload = season.get("result") or {}
    episodes: list[dict[str, Any]] = list(payload.get("episodes") or [])
    for section in payload.get("section") or []:
        episodes.extend(section.get("episodes") or [])
    target = next((e for e in episodes if str(e.get("ep_id") or "") == ep_id), None)
    if not target and episodes:
        target = episodes[0]
    if not target:
        return None, "B站番剧接口没有返回该集信息，可能是下架或地区限制"
    cid = str(target.get("cid") or "")
    bvid = str(target.get("bvid") or "")
    aid = str(target.get("aid") or "")
    if not cid:
        return None, "该集没有可用的 cid（可能是预告 / 未上线）"
    title = safe_filename(" ".join(filter(None, [
        str(payload.get("title") or ""), str(target.get("long_title") or ""),
        str(target.get("title") or ""),
    ])) or "bilibili_bangumi", 90)

    params = {"cid": cid, "qn": 64, "fnval": 0, "fnver": 0, "fourk": 0,
              "otype": "json"}     # 不加 platform=html5，番剧带上它会返回 -404
    if bvid:
        params["bvid"] = bvid
    if aid:
        params["avid"] = aid
    play: dict[str, Any] = {}
    for api in ("https://api.bilibili.com/x/player/playurl",
                "https://api.bilibili.com/pgc/player/web/playurl"):
        query = dict(params)
        if api.endswith("pgc/player/web/playurl"):
            query["ep_id"] = ep_id
        try:
            play = requests.get(
                api, params=query, headers={**headers, "Referer": link.url},
                timeout=min(opts.timeout, 15), proxies=proxies,
            ).json()
        except Exception:                               # noqa: BLE001
            continue
        if str(play.get("code") or 0) in ("0", ""):
            break
    pdata = play.get("data") or play.get("result") or {}
    durls = pdata.get("durl") or []
    urls = [str(d.get("url") or "") for d in durls]
    for d in durls:
        backup = d.get("backup_url")
        if isinstance(backup, str) and backup.startswith("http"):
            urls.append(backup)
        elif isinstance(backup, list):
            urls.extend(str(u) for u in backup)
    urls = [u for u in urls if u.startswith("http")]
    if not urls:
        return None, (f"B站播放地址未返回视频流（code={play.get('code')}，"
                      f"{str(play.get('message') or '可能需要大会员 / 登录')[:40]}）；"
                      "该集可能受地区或会员限制")
    cover = str(target.get("cover") or payload.get("cover") or "")
    length_ms = pdata.get("timelength") or 0
    try:
        duration = float(length_ms) / 1000 or None
    except (TypeError, ValueError):
        duration = None
    entry = MediaEntry(
        id=_stable_id(f"{link.url}#{ep_id}"),
        title=title,
        kind="video",
        url=link.url,
        platform=link.platform,
        platform_name=link.platform_name,
        thumbnail=cover,
        duration=duration,
        raw={
            "source": "bilibili-pgc",
            "direct_video_url": urls[0],
            "video_urls": urls[:4],
            "ytdlp_headers": {"Referer": "https://www.bilibili.com/",
                              "User-Agent": DESKTOP_UA},
            "referer": "https://www.bilibili.com/",
            "watermark_free": True,
            "channel": "pgc-public",
            "quality": "B站公开标清流（durl 含音轨单文件）",
        },
    )
    return entry, ""


# --------------------------------------------------------------------------- #
# yt-dlp
# --------------------------------------------------------------------------- #
def _parse_with_ytdlp(link: Link, opts: DownloadOptions) -> list[MediaEntry]:
    import yt_dlp

    ydl_opts = ytdlp_opts(opts, link.platform, extract_only=True)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(link.url, download=False)
    if not info:
        return []

    raw_entries = _flatten_info(info)
    picture_urls: list[str] = []
    video_entries: list[MediaEntry] = []
    playlist_title = safe_filename(str(info.get("title") or ""))
    video_page = _is_definite_video_page(link)

    for item in raw_entries:
        # 视频详情页里，yt-dlp 只返回封面 / 缩略图时不能当成图片作品
        urls = [] if video_page else _image_urls_from_info(item)
        if urls:
            picture_urls.extend(urls)
            continue
        page_url = str(item.get("webpage_url") or item.get("original_url") or link.url)
        direct, headers = _direct_from_info(item)
        if video_page and not direct and not item.get("formats"):
            continue                    # 只有封面、没有视频流，视为解析失败
        video_entries.append(MediaEntry(
            id=str(item.get("id") or _stable_id(page_url or link.url)),
            title=safe_filename(str(item.get("title") or item.get("description") or playlist_title or "video")),
            kind="video",
            url=page_url,
            platform=link.platform,
            platform_name=link.platform_name,
            thumbnail=_best_thumbnail(item),
            uploader=str(item.get("uploader") or item.get("channel") or ""),
            duration=item.get("duration"),
            raw={
                "source": "ytdlp",
                "ytdlp_url": direct,
                "ytdlp_headers": headers,
                "referer": page_url,
                "watermark_free": False,
            },
        ))

    result: list[MediaEntry] = []
    if picture_urls:
        unique = _dedupe(picture_urls)[: max(1, opts.max_images)]
        title = playlist_title or safe_filename(str(raw_entries[0].get("title") or "gallery"))
        result.append(_gallery_entry(link, unique, title, opts))
    result.extend(video_entries)
    return result


def _direct_from_info(item: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """提取 yt-dlp 解析出的媒体直链，供浏览器下载直接流式转发。

    没有直链时返回空串，交付层会临时用 yt-dlp 下载后再转交给浏览器。
    """
    headers = {str(k): str(v) for k, v in (item.get("http_headers") or {}).items()}
    url = str(item.get("url") or "")
    if url.startswith(("http://", "https://")):
        return url, headers
    candidates = []
    for fmt in item.get("formats") or []:
        furl = str(fmt.get("url") or "")
        if not furl.startswith(("http://", "https://")):
            continue
        # 优先自带音轨的单文件，避免只拿到无声画面
        score = (2 if fmt.get("acodec") not in (None, "none") else 0) + \
                (1 if fmt.get("vcodec") not in (None, "none") else 0)
        candidates.append((score, float(fmt.get("tbr") or 0), furl,
                           {str(k): str(v) for k, v in (fmt.get("http_headers") or {}).items()}))
    if candidates:
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return candidates[0][2], {**headers, **candidates[0][3]}
    return "", headers


def _flatten_info(info: dict[str, Any], depth: int = 0) -> list[dict[str, Any]]:
    """展开 playlist / multi_video / url_transparent 等嵌套结构。"""
    if depth > 3 or not info:
        return []
    entries = info.get("entries")
    if entries:
        out: list[dict[str, Any]] = []
        for sub in entries:
            if isinstance(sub, dict):
                out.extend(_flatten_info(sub, depth + 1))
        return out
    return [info]


def _image_urls_from_info(info: dict[str, Any]) -> list[str]:
    """图集在 yt-dlp 里往往表现为 ext=jpg 的条目，或 formats 里只有图片。"""
    ext = str(info.get("ext") or "").lower()
    url = str(info.get("url") or "")
    candidates: list[str] = []
    if ext in _IMAGE_EXT_NAMES and url:
        candidates.append(url)
    for fmt in info.get("formats") or []:
        fext = str(fmt.get("ext") or "").lower()
        furl = str(fmt.get("url") or "")
        if fext in _IMAGE_EXT_NAMES and furl:
            candidates.append(furl)
    if _path_is_image(url):
        candidates.append(url)
    # 有些图集把图片放在 thumbnails 里，且没有其他可下载格式
    if not (info.get("formats") or info.get("url")):
        candidates.extend(t.get("url", "") for t in info.get("thumbnails") or [])
    return [u for u in candidates if u and not _IMAGE_SKIP.search(u)]


def _best_thumbnail(info: dict[str, Any]) -> str:
    thumbs = info.get("thumbnails") or []
    if thumbs:
        return str(thumbs[-1].get("url") or "")
    return str(info.get("thumbnail") or "")


# --------------------------------------------------------------------------- #
# 页面图片提取
# --------------------------------------------------------------------------- #
_PAGE_CACHE: dict[str, tuple[float, str, str, str]] = {}
_PAGE_LOCK = threading.Lock()


def fetch_page(url: str, link: Link, opts: DownloadOptions) -> tuple[str, str, str]:
    """抓取页面 HTML 并做短期缓存，视频直链提取与图片提取共用同一次请求。"""
    now = time.time()
    with _PAGE_LOCK:
        cached = _PAGE_CACHE.get(url)
        if cached and now - cached[0] < 120:
            return cached[1], cached[2], cached[3]

    session = requests.Session()
    session.headers.update({
        "User-Agent": MOBILE_UA if link.platform in ("douyin", "kuaishou", "xiaohongshu") else DESKTOP_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": url,
        # 不接受压缩响应，规避源站 / 代理下发编码错乱压缩体时的裸 zlib.error
        "Accept-Encoding": "identity",
    })
    if opts.cookie_file:
        try:
            jar = MozillaCookieJar(opts.cookie_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies = jar
        except Exception:                          # noqa: BLE001
            pass
    elif getattr(opts, "cookie", ""):
        session.headers["Cookie"] = opts.cookie
    proxies = {"http": opts.proxy, "https": opts.proxy} if opts.proxy else None
    try:
        resp = session.get(url, timeout=opts.timeout, proxies=proxies, allow_redirects=True)
        resp.raise_for_status()
        body, final_url = resp.text, resp.url
    except Exception:
        # 系统代理不通时不该让整条免 Cookie 链路失效，自动直连重试一次
        session.close()
        direct = requests.Session()
        direct.trust_env = False
        direct.headers.update(session.headers)
        try:
            resp = direct.get(url, timeout=opts.timeout, allow_redirects=True)
            resp.raise_for_status()
            body, final_url = resp.text, resp.url
        except Exception:                          # noqa: BLE001 - 直连也失败则返回空页，由上层兜底
            direct.close()
            return "", url, ""
        finally:
            direct.close()
    finally:
        session.close()

    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = safe_filename(html_lib.unescape(title_match.group(1)).strip()) if title_match else ""
    title = re.sub(r"\s*[-_|]\s*(抖音|哔哩哔哩|快手|小红书).*$", "", title).strip() or "media"

    with _PAGE_LOCK:
        if len(_PAGE_CACHE) > 100:
            _PAGE_CACHE.clear()
        _PAGE_CACHE[url] = (now, body, final_url, title)
    return body, final_url, title


def extract_images_from_page(
    url: str, link: Link, opts: DownloadOptions
) -> tuple[list[str], str]:
    """从 HTML 中提取内容图片，兼容抖音 RENDER_DATA、JSON-LD、og:image、懒加载属性。"""
    body, final_url, title = fetch_page(url, link, opts)
    urls = html_image_urls(body, final_url)
    return _dedupe(urls)[: max(1, opts.max_images)], title


def video_urls_from_html(body: str, base: str) -> list[str]:
    """从页面或内嵌 JSON（快手 __APOLLO_STATE__ 等）中提取 mp4 直链。"""
    variants = [body]
    decoded = body.replace("\\/", "/").replace("\\u002F", "/").replace("\\u0026", "&")
    if decoded != body:
        variants.append(decoded)
    try:
        unquoted = unquote(body)
        if unquoted != body:
            variants.append(unquoted.replace("\\/", "/"))
    except Exception:                              # noqa: BLE001
        pass

    found: list[str] = []

    def add(value: str) -> None:
        value = html_lib.unescape(value.strip()).replace("\\/", "/").replace("\\u0026", "&")
        if value.startswith("//"):
            value = "https:" + value
        elif not value.startswith("http"):
            value = urljoin(base, value)
        if value.startswith("http") and value not in found:
            found.append(value)

    patterns = (
        r"<meta[^>]+(?:property|name)\s*=\s*[\"']og:video(?::url|:secure_url)?[\"'][^>]*content\s*=\s*[\"']([^\"']+)",
        r"<video[^>]+src\s*=\s*[\"']([^\"']+)[\"']",
        r"<source[^>]+src\s*=\s*[\"']([^\"']+\.mp4[^\"']*)[\"']",
        r"[\"'](?:playUrl|photoUrl|videoUrl|downloadUrl|mainMvUrl|mp4Url|url)[\"']\s*:\s*[\"'](https?:[^\"']+\.mp4[^\"']*)[\"']",
        r"https?://[^\s\"'<>\\\)]{10,600}\.mp4(?:\?[^\s\"'<>\\\)]{0,300})?",
    )
    for text in variants:
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.I):
                add(m.group(1) if m.lastindex else m.group(0))
    return [u for u in found if not re.search(r"\.(js|css|json)(\?|$)", u, re.I)]


def _video_from_page(link: Link, opts: DownloadOptions) -> MediaEntry | None:
    """yt-dlp 不认识该站点时（典型如快手），尝试直接取页面里的 mp4。"""
    try:
        body, final_url, title = fetch_page(link.url, link, opts)
    except Exception:                              # noqa: BLE001
        return None
    videos = video_urls_from_html(body, final_url)
    if not videos:
        return None
    return MediaEntry(
        id=_stable_id(link.url),
        title=safe_filename(title or "video"),
        kind="video",
        url=link.url,
        platform=link.platform,
        platform_name=link.platform_name,
        raw={"source": "page", "direct_video_url": videos[0], "referer": link.url},
    )


def html_image_urls(body: str, base: str) -> list[str]:
    """按优先级抓取页面里的图片地址。"""
    # 平台内嵌 JSON 常把 / 转义成 \/ 或 \u002F，展开后再匹配
    variants = [body]
    decoded = body.replace("\\/", "/").replace("\\u002F", "/").replace("\\u0026", "&")
    if decoded != body:
        variants.append(decoded)
    try:
        unquoted = unquote(body)
        if unquoted != body:
            variants.append(unquoted.replace("\\/", "/"))
    except Exception:                              # noqa: BLE001
        pass

    ordered: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        if not candidate or candidate.startswith("data:"):
            return
        candidate = html_lib.unescape(candidate.strip()).replace("\\/", "/")
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        elif not candidate.startswith("http"):
            candidate = urljoin(base, candidate)
        if not candidate.startswith("http") or _IMAGE_SKIP.search(candidate):
            return
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)

    for text in variants:
        # meta 标签（两种属性顺序都覆盖）
        for m in re.finditer(
            r"<meta[^>]+(?:property|name)\s*=\s*[\"'](?:og:image(?::url)?|twitter:image)[\"'][^>]*>",
            text, re.I,
        ):
            c = re.search(r"content\s*=\s*[\"']([^\"']+)[\"']", m.group(0), re.I)
            if c:
                add(c.group(1))
        for m in re.finditer(
            r"<meta[^>]+content\s*=\s*[\"']([^\"']+)[\"'][^>]*(?:property|name)\s*=\s*[\"'](?:og:image(?::url)?|twitter:image)[\"']",
            text, re.I,
        ):
            add(m.group(1))
        # JSON-LD
        for m in re.finditer(r"\"image\"\s*:\s*\"(https?[^\"\\]+)\"", text, re.I):
            add(m.group(1))
        # img 标签
        for tag in re.findall(r"<img\b[^>]*>", text, re.I):
            for attr in ("data-original", "data-src", "data-lazy-src", "src", "data-url"):
                m = re.search(rf"{attr}\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
                if m:
                    add(m.group(1))
                    break
            m = re.search(r"srcset\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
            if m:
                parts = [p.strip().split()[0] for p in m.group(1).split(",") if p.strip()]
                if parts:
                    add(parts[-1])
        # 兜底：正文里出现的所有图片 URL（含 CDN 无扩展名带参数的地址）
        for m in re.finditer(
            r"https?://[^\s\"'<>\\\)]{8,500}?\.(?:jpe?g|png|webp|heic|avif|bmp)(?:\?[^\s\"'<>\\\)]{0,200})?",
            text, re.I,
        ):
            add(m.group(0))
        # 图片 CDN 常用无扩展名地址（抖音 ~tplv / 微博 imageView 等）
        for m in re.finditer(
            r"https?://[A-Za-z0-9.\-]*(?:douyinpic|hdslb|sinaimg|xhscdn|byteimg|pstatp|kwai|qhimg|alicdn)"
            r"[A-Za-z0-9.\-]*/[^\s\"'<>\\\)]{10,400}",
            text, re.I,
        ):
            add(m.group(0))
    return ordered


# --------------------------------------------------------------------------- #
# B站图文动态
# --------------------------------------------------------------------------- #
def _bilibili_dynamic(link: Link, opts: DownloadOptions) -> tuple[list[str], str]:
    if link.platform != "bilibili":
        return [], ""
    m = re.search(r"/(?:opus|dynamic)/(\d+)", link.url)
    if not m:
        return [], ""
    dynamic_id = m.group(1)
    api = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?timezone_offset=-480&id={dynamic_id}"
    headers = {
        "User-Agent": DESKTOP_UA,
        "Referer": "https://www.bilibili.com/",
    }
    proxies = {"http": opts.proxy, "https": opts.proxy} if opts.proxy else None
    try:
        resp = requests.get(api, headers=headers, timeout=opts.timeout, proxies=proxies)
        data = resp.json()
        item = (data.get("data") or {}).get("item") or {}
        modules = item.get("modules") or {}
        title = safe_filename((((modules.get("module_dynamic") or {}).get("desc") or {}).get("text") or "")[:60]) or "bilibili_dynamic"
        major = (modules.get("module_dynamic") or {}).get("major") or {}
        draw = major.get("draw") or major.get("archive") or {}
        urls = [i.get("src", "") for i in (draw.get("items") or []) if i.get("src")]
        # 专栏 / 图文也可能嵌在 opus 的 paragraphs 里
        if not urls:
            for p in ((major.get("opus") or {}).get("paragraphs") or []):
                for pic in ((p.get("pic") or {}).get("pics") or []):
                    if pic.get("url"):
                        urls.append(pic["url"])
        return _dedupe(urls)[: max(1, opts.max_images)], title
    except Exception:                              # noqa: BLE001
        return [], ""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _gallery_entry(link: Link, images: list[str], title: str, opts: DownloadOptions) -> MediaEntry:
    return MediaEntry(
        id=_stable_id(link.url),
        title=safe_filename(title or "gallery"),
        kind="gallery" if len(images) > 1 else "image",
        url=link.url,
        platform=link.platform,
        platform_name=link.platform_name,
        thumbnail=images[0] if images else "",
        images=list(images[: max(1, opts.max_images)]),
    )


def _path_is_image(url: str) -> bool:
    return urlparse(url or "").path.lower().endswith(_IMAGE_EXTS)


def _filename_from_url(url: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return unquote(name).rsplit(".", 1)[0] if name else ""


def _stable_id(value: str) -> str:
    import hashlib
    return hashlib.md5(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


_FRIENDLY_ERRORS = (
    ("fresh cookies", "平台要求登录后访问，已触发风控拦截"),
    ("argussecurityplugin", "平台风控网关拒绝了匿名请求"),
    ("signature not found", "平台签名校验未通过"),
    ("http error 403", "平台拒绝匿名访问（403 风控）"),
    ("http error 401", "需要登录才能访问该内容"),
    ("http error 404", "作品不存在或已删除"),
    ("http error 429", "请求过于频繁，被平台限流，请稍后再试"),
    ("http error 5", "平台服务器暂时异常，请稍后重试"),
    ("unsupported url", "该链接格式暂不被解析器支持"),
    ("no video formats", "未找到可下载的媒体格式"),
    ("video unavailable", "视频不可用（可能已删除或仅限特定地区）"),
    ("private video", "该作品为私密内容，无法访问"),
    ("login required", "该内容需要登录后才能访问"),
    ("not found", "内容不存在或链接已失效"),
    ("timed out", "网络请求超时，请检查网络后重试"),
    ("timeout", "网络请求超时，请检查网络后重试"),
    ("connection", "网络连接失败，请检查网络后重试"),
    ("ssl", "加密连接异常，请检查网络环境后重试"),
    ("urlopen error", "网络连接失败，请检查网络后重试"),
    ("json", "平台返回数据异常（可能是风控拦截），请稍后重试"),
    ("keyerror", "平台返回数据结构有变化，请稍后重试"),
    ("typeerror", "平台返回数据结构有变化，请稍后重试"),
)


def _friendly(exc: Exception) -> str:
    """把英文异常 / 风控报错转成用户能看懂的中文提示（不含英文异常类名）。"""
    low = f"{type(exc).__name__} {_brief(exc)}".lower()
    for needle, hint in _FRIENDLY_ERRORS:
        if needle in low:
            return hint
    return "解析失败，请确认链接可公开访问、未过期，或稍后重试"


def _brief(exc: Exception, limit: int = 140) -> str:
    msg = str(exc).replace("\n", " ").strip()
    msg = re.sub(r"\s+", " ", msg)
    return msg[:limit] + ("..." if len(msg) > limit else "")


def json_default(obj: Any) -> str:
    return str(obj)
