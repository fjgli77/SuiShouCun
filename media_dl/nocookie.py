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
import time
from typing import Any, Iterator
from urllib.parse import quote, unquote, urljoin, urlparse

import requests

from .links import Link, safe_filename, DESKTOP_UA
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
# 播放地址网关：aweme/v1/play/?video_id=<uri>&ratio=<档位> 会 302 到真实 mp4。
# 免 Cookie 的 feed 接口对多数作品只下发 720P/540P，而该网关接受 ratio=1080p
# 等档位参数并按请求档位返回对应清晰度，是补齐 1080P 的关键（见 _feed_high_quality_variants）。
PLAY_ENDPOINTS = (
    "https://aweme.snssdk.com/aweme/v1/play/",
    "https://api5-normal-c-hl.amemv.com/aweme/v1/play/",
)
# 需要探测的档位（高到低）。仅当 feed 下发的最高档低于此值时才会按需探测，
# 避免为了一个本来就 720P 的源白白多打好几次请求。
RATIO_LADDER = ("1080p", "720p", "540p")
# 图文作品（/note/）在免登录 feed 接口里不下发，但抖音给搜索引擎的 SEO 页面
# 会带上无水印原图。用该 UA 抓取即可解析出图集，全程免 Cookie。
_SPIDER_UA = "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)"
# SEO 页面里原图的 CDN 形态：p3-pc-sign.douyinpic.com/tos-cn-i-xxxx/<hash>~tplv-...
# 兼容 p3-pc-sign / p6-sign / p9-sign 等前缀与带签名参数的变体。
_SEO_IMAGE_RE = re.compile(
    r"https?://p\d+(?:-pc)?-sign\.douyinpic\.com/tos-cn-i-[\w~-]+/"
    r"[\w~:.,!\-]+\.(?:webp|jpeg|jpg|png)(?:\?[^\"'\s<>\\]*)?", re.I,
)
# SEO 页面里带水印的 desc / 头像 / 抖音 logo，需要排除，只留原图
_SEO_IMAGE_SKIP = re.compile(r"(avatar|logo|icon|watermark|douyin_static|/obj/douyin)", re.I)
# 抖音 SEO 页（搜索引擎抓取视角）
_SEO_PAGE_URLS = (
    "https://www.iesdouyin.com/share/note/{mid}/",
    "https://www.douyin.com/note/{mid}",
    "https://m.douyin.com/share/note/{mid}",
)
# 探测具体档位时的 Range 大小：只需覆盖 moov 头即可解析分辨率，128KB 足够且省流量
_PROBE_RANGE = "bytes=0-131071"
# 档位探测结果缓存：url -> (height, ts)。同一作品的档位短时间内不变，
# 缓存可显著减少移动端弱网下重复解析的打请求次数（解析卡顿主因之一）。
_TIER_CACHE: dict[str, tuple[int, float]] = {}
_TIER_CACHE_TTL = 1800.0
# 图文 / 图集作品的链接特征（PC 端与移动端复制的形态不同，这里做并集）
_NOTE_HINT = re.compile(
    r"/(?:note|slides|slide|album|gallery)/|share/note|noteId=|/note\?|type=image",
    re.I,
)
_APP_UA = (
    "com.ss.android.ugc.aweme/110101 (Linux; U; Android 13; en; Pixel 7; "
    "Build/TQ3A.230901.001; Cronet/TTNetVersion:8f1c5b2e)"
)


def parse_nocookie(link: Link, opts: DownloadOptions, warnings: list[str] | None = None) -> list[MediaEntry]:
    """返回免登录解析结果；无法解析时返回空列表，由上层继续兜底。

    `warnings` 用于回填诊断信息（如「免登录接口未下发该图文作品」），便于界面给出
    准确的失败原因，而不是笼统的「风控」提示。
    """
    if link.platform == "douyin":
        entries, note = _douyin(link, opts)
        if note and warnings is not None:
            warnings.append(note)
        return entries
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


def _decode_jsonish(value: Any) -> Any:
    """把「可能被转义过的 JSON 字符串」还原成对象；不是 JSON 就原样返回。

    快手分享页把某些子结构塞成字符串（ext_params 常见），且字符串里还会带
    ``\\u002F`` 之类的转义，需要先 unescape 再 loads。
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text.startswith(("{", "[")):
        return value
    for candidate in (text, html_lib.unescape(text)):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return value


def _atlas_urls(atlas: Any) -> list[str]:
    """解析快手图集（atlas）结构，返回真实图片 URL 列表。

    快手图集长这样（ext_params.atlas / __APOLLO_STATE__ 里的 atlas 同构）::

        {"cdn": ["p23.a.yximgs.com", "p2.a.yximgs.com"],
         "list": ["/ufile/atlas/xxx_0.webp", "/ufile/atlas/xxx_1.webp", ...],
         "size": [{"w": 1672, "h": 942}, ...], "volume": 1}

    ``list`` 里是**纯路径字符串**（没有 scheme），必须与 ``cdn`` 主机前缀拼成
    完整 URL。早期实现把 list 直接丢给 _url_list()，纯路径不含 scheme 被整段
    丢弃，于是整条图集分支失效、回退抓到 ``music.imageUrls``（音乐播放器配图）
    —— 这就是「解析出的图片和源图不一致」的根因。
    """
    atlas = _decode_jsonish(atlas)
    if isinstance(atlas, (list, tuple)):
        # 裸数组：通常已经是完整 URL 数组（无 cdn 前缀可拼）
        return _url_list(list(atlas))
    if not isinstance(atlas, dict):
        return []

    hosts = _cdn_hosts(atlas)
    paths = _paths_of(atlas, hosts)
    # 同一张图会在多个 CDN 主机上重复（拼出 N 份），按「路径」去重只保留首选主机，
    # 否则 8 张图会膨胀成 16 条、下载器会重复下载。
    out: list[str] = []
    seen: set[str] = set()
    for u in paths:
        key = re.sub(r"^https?://[^/]+", "", u)
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def _cdn_hosts(atlas: dict[str, Any]) -> list[str]:
    """收集 atlas 里的 CDN 主机名（兼容 cdn / cdnList / cdn_list / hosts 多形态）。"""
    raw: list[str] = []

    def take(value: Any) -> None:
        if isinstance(value, str):
            raw.append(value)
        elif isinstance(value, dict):
            for k in ("cdn", "host", "domain", "url"):
                take(value.get(k))
        elif isinstance(value, (list, tuple)):
            for item in value:
                take(item)

    for key in ("cdnList", "cdn", "cdn_list", "hosts", "host", "domain"):
        if key in atlas:
            take(atlas[key])

    out: list[str] = []
    for h in raw:
        h = html_lib.unescape(str(h).strip())
        if not h:
            continue
        # 主机名可能被写成 //host 或 https://host，统一成裸主机名
        h = re.sub(r"^https?://", "", h).strip("/")
        if "/" in h:                       # 若已是完整 URL，取 netloc 之外原样保留
            out.append(h)
        elif h:
            out.append(h)
    return list(dict.fromkeys(out))


def _paths_of(atlas: dict[str, Any], hosts: list[str]) -> list[str]:
    """把 atlas 里的图片路径（list/images/urls…）拼成可下载 URL。"""
    raw: list[str] = []

    def take(value: Any) -> None:
        if isinstance(value, str):
            raw.append(value)
        elif isinstance(value, dict):
            for k in ("url", "src", "cdn_url", "cdnUrl", "path", "download_url"):
                if isinstance(value.get(k), str):
                    raw.append(value[k])
        elif isinstance(value, (list, tuple)):
            for item in value:
                take(item)

    for key in ("list", "images", "urls", "url_list", "url", "src", "atlas"):
        if key in atlas:
            take(atlas[key])

    out: list[str] = []
    for p in raw:
        p = html_lib.unescape(str(p).strip()).replace("\\/", "/")
        if not p:
            continue
        if _HTTP.match(p):
            out.append(p)
        elif p.startswith("//"):
            out.append("https:" + p)
        else:
            path = p if p.startswith("/") else "/" + p
            out.extend("https://" + h + path for h in hosts)
    return out


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
    """从抖音 bit_rate.gear_name 解析分辨率高度。

    抖音下发的档位名有两种形态：
    - 纯数字档位：'1080p' / '540p' / '蓝光 1080P'
    - 复合标识：'720_1_1' / '540_2_1'（第一位是档位高度，后两位是编码/清晰度变体）
    """
    if not gear_name:
        return None
    text = str(gear_name)
    m = re.match(r"\s*(\d{3,4})(?:_|$|\D)", text)
    if not m:
        m = re.search(r"(\d{3,4})", text)
    return int(m.group(1)) if m else None


def _quality_label(gear_name: Any, height: int | None) -> str:
    """把档位标识转成用户看得懂的展示名。

    '蓝光' / '原画' 之类保留原文；'720_1_1' 这类复合标识归一为 '720P'；
    纯数字档 '540p' 归一为 '540P'。这样下拉框就不会再显示 '720_1_1'。
    """
    text = str(gear_name or "").strip()
    # 明确的中文档位名（蓝光 / 原画 / 2K / 4K…）原样保留
    if text and re.search(r"[^\x00-\x7f]", text):
        return text
    if height:
        return f"{height}P"
    return text or "默认"


def _tier_from_play_addr(pa: Any) -> int | None:
    """从 play_addr 的 width/height 推断「清晰度档位」。

    竖屏视频 play_addr.height 是屏幕高（720P 竖屏下发 1280），直接当档位会得到
    「1280P」这种不存在的档位，导致前端「画质上限」永远匹配不上。取长短边的较小值
    才是业界口径的档位（1280×720 -> 720，1920×1080 -> 1080）。
    """
    if not isinstance(pa, dict):
        return None
    h, w = pa.get("height"), pa.get("width")
    hh = int(h) if str(h or "").isdigit() else 0
    ww = int(w) if str(w or "").isdigit() else 0
    if hh and ww:
        return min(hh, ww)
    return hh or ww or None


def _feed_quality_variants(video: dict[str, Any], remove_watermark: bool = True) -> list[dict[str, Any]]:
    """按画质降序返回候选直链 [{height, url, watermark_free, label}]，供「画质上限」筛选。

    档位（height）优先取 bit_rate.gear_name（抖音下发的真实档位标识，如 '720_1_1'
    / '1080p' / '蓝光 1080P'），解析不出时再用 play_addr 的短边兜底。
    注意不要直接用 play_addr.height：竖屏作品下发的是屏幕高（1280），会得到
    「1280P」这种不存在的档位。展示标签统一走 _quality_label（'720_1_1' -> '720P'）。
    仅收集平台下发的 /play/ 无水印源（去水印关闭时收集带水印的 download_addr）。
    """
    seen: set[str] = set()
    variants: list[dict[str, Any]] = []
    bitrates = [b for b in (video.get("bit_rate") or []) if isinstance(b, dict)]
    bitrates.sort(key=lambda b: int(b.get("bit_rate") or 0), reverse=True)
    for br in bitrates:
        pa = br.get("play_addr")
        url = _pick(_url_list(pa)) if isinstance(pa, dict) else None
        if not url:
            continue
        height = _parse_height(br.get("gear_name"))
        if height is None:
            height = _tier_from_play_addr(pa)
        if height is None:
            continue
        height = int(height)
        label = _quality_label(br.get("gear_name"), height)
        u = _strip_watermark(url) if remove_watermark else url
        if u in seen:
            continue
        seen.add(u)
        variants.append({"height": height, "url": u,
                         "watermark_free": remove_watermark, "label": label})
    return variants


def _title(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"#\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return safe_filename(text, 70) or fallback


def _media_id(link: Link) -> str:
    url = link.url
    for pattern in (r"/(?:video|note|slide|slides|album|share/video|share/note|share/slides)/(\d{8,})",
                    r"(?:modal_id|item_ids|aweme_id|photoId|photo_id)=(\d{8,})",
                    r"/short-video/([A-Za-z0-9_-]{6,})",
                    r"/(?:fw|profile)/photo/(\d+)",
                    r"/([A-Za-z0-9]{10,})(?:/|\?|$)?"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return ""


# --------------------------------------------------------------------------- #
# MP4 分辨率探测（用于确认播放网关按 ratio 返回的真实档位）
# --------------------------------------------------------------------------- #
def _mp4_dimensions(data: bytes) -> tuple[int, int] | None:
    """从 MP4 头部字节解析视频轨分辨率（宽, 高）。

    只依赖 tkhd box：它的最后 8 字节是两个 16.16 定点数（宽、高），音频轨这两项为 0，
    因此取面积最大的一轨即视频分辨率。只需 Range 取文件头部若干 KB 即可，无需整包下载。
    """
    if not data or len(data) < 16:
        return None
    best: tuple[int, int] | None = None
    cursor = 0
    while True:
        idx = data.find(b"tkhd", cursor)
        if idx < 4:
            break
        box_start = idx - 4
        size = int.from_bytes(data[box_start:box_start + 4], "big")
        box_end = box_start + size
        if size >= 8 and box_end <= len(data):
            wh = data[box_end - 8:box_end]
            if len(wh) == 8:
                w = int.from_bytes(wh[0:4], "big") >> 16
                h = int.from_bytes(wh[4:8], "big") >> 16
                if w > 0 and h > 0 and (best is None or w * h > best[0] * best[1]):
                    best = (w, h)
        cursor = idx + 4
    return best


def _probe_tier_height(url: str, opts: DownloadOptions, want: int) -> int | None:
    """对播放网关返回的直链做一次 Range 探测，返回真实档位（min(宽, 高)）。

    探测到的短边优先采信；解析不出尺寸时，退而采信请求档位 want（网关按 ratio 302，
    能返回文件即说明该档位存在），避免因 moov 未落在探测窗口内而漏掉一个真实可用的档位。
    结果做进程内缓存：同一作品的档位在短时间内不会变，重复解析不必再打请求
    （移动端弱网下这是「解析卡顿」的主要来源之一）。
    """
    cached = _TIER_CACHE.get(url)
    if cached is not None:
        if time.time() - cached[1] < _TIER_CACHE_TTL:
            return cached[0] or None
        _TIER_CACHE.pop(url, None)
    session = _api_session(opts)
    try:
        resp = session.get(url, headers={"Range": _PROBE_RANGE}, stream=True,
                           timeout=min(opts.timeout, 6), allow_redirects=True)
        if resp.status_code >= 400:
            resp.close()
            return None
        data = b""
        for chunk in resp.iter_content(chunk_size=32768):
            data += chunk
            if len(data) >= 131072:
                break
        resp.close()
    except Exception:                                  # noqa: BLE001 - 探测失败不影响主流程
        return None
    finally:
        session.close()
    dims = _mp4_dimensions(data)
    h = min(dims[0], dims[1]) if dims else (want or None)
    if h:
        _TIER_CACHE[url] = (int(h), time.time())
    return h


def _video_uri(video: dict[str, Any]) -> str:
    """取作品的 video_id / uri，用于拼接 aweme/v1/play 播放网关。"""
    pa = video.get("play_addr")
    if isinstance(pa, dict) and pa.get("uri"):
        return str(pa["uri"])
    groups = [video.get("play_addr"), video.get("play_addr_h264"),
              video.get("play_addr_265"), video.get("play_addr_lowbr")]
    groups += [b.get("play_addr") for b in (video.get("bit_rate") or []) if isinstance(b, dict)]
    for group in groups:
        for u in _url_list(group):
            m = re.search(r"[?&]video_id=([^&]+)", u)
            if m:
                return unquote(m.group(1))
    return ""


def _feed_high_quality_variants(video: dict[str, Any], opts: DownloadOptions | None,
                                have_tiers: list[int]) -> list[dict[str, Any]]:
    """用播放网关按 ratio 档位补齐 feed 未下发的高清晰度。

    免 Cookie 的 feed 接口对多数作品最高只给 720P，而 `aweme/v1/play/?video_id=<uri>
    &ratio=1080p` 会按请求档位 302 到对应清晰度的真实 mp4（实测 1080p -> 1080×1920）。
    这里从高到低探测 RATIO_LADDER，Range 取 moov 解析真实分辨率；仅当 feed 下发的最高档
    低于 1080 时才尝试，避免为一个本就 720P 的源白白多打请求。
    """
    if opts is None:
        return []
    uri = _video_uri(video)
    if not uri:
        return []
    top = max(have_tiers) if have_tiers else 0
    if top >= 1080:
        return []
    out: list[dict[str, Any]] = []
    seen: set[int] = set(have_tiers)
    # 声明档位（未验证）：网关按 ratio 参数返回对应清晰度，档位存在性由源视频决定。
    # PC 端网络充裕时探测成功率高；移动端弱网 / 探测超时会让高档位整档丢失，
    # 出现「PC 有 1080P、手机只有 720P」。这里把声明档位作为兜底保留，
    # 探测成功时用实测高度替代，探测失败时仍保留，保证两端档位一致。
    declared: list[dict[str, Any]] = []
    for endpoint in PLAY_ENDPOINTS:
        for ratio in RATIO_LADDER:
            want = int(ratio[:-1]) if ratio.endswith("p") else 0
            if want and want <= top:
                continue
            url = (f"{endpoint}?video_id={quote(uri, safe='')}"
                   f"&ratio={ratio}&line=0&aid=1128")
            h = _probe_tier_height(url, opts, want)
            if h and h not in seen:
                seen.add(h)
                out.append({"height": h, "url": url, "watermark_free": True,
                            "label": f"{h}P"})
            elif want and want not in seen:
                # 探测未成功：仍登记该档位（下载时再用真实返回校验）
                declared.append({"height": want, "url": url, "watermark_free": True,
                                 "label": f"{want}P", "declared": True})
        if out:
            break
    if not out and declared:
        # 探测全部失败（离线 / 弱网 / 网关限流）——保留声明档位，避免手机端少一档
        dedup: dict[int, dict[str, Any]] = {}
        for v in declared:
            dedup.setdefault(int(v["height"]), v)
        out = list(dedup.values())
    out.sort(key=lambda v: v["height"], reverse=True)
    return out


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
        # 不接受压缩响应：抖音节点 / 中间代理偶发下发编码标记错乱的压缩体，
        # 解析端读取时会抛出裸 zlib.error 并穿透整条链路（前端显示 "Error -3 ..."）
        "Accept-Encoding": "identity",
    })
    # 用户手动粘贴的抖音登录态 Cookie：仅加在请求头，发往抖音自身域名，
    # 用于免 Cookie 通道取不到的图文 / 登录作品；不上传到任何服务器
    if getattr(opts, "cookie", ""):
        session.headers["Cookie"] = opts.cookie
    if opts.proxy:
        session.proxies.update({"http": opts.proxy, "https": opts.proxy})
    return session


def _douyin_feed(media_id: str, opts: DownloadOptions) -> tuple[dict[str, Any] | None, bool]:
    """请求移动端推荐流，返回 aweme_id 精确匹配的作品对象。

    返回 (item, got_any)：
    - item  : 精确匹配到的作品，找不到为 None；
    - got_any: 是否有任一节点返回了非空的 aweme_list（用于诊断「平台返回了推荐流但
              没有目标作品」——典型是图文 / 需登录态的作品，免登录接口不再下发）。

    feed 接口会顺带返回若干推荐作品，必须按 ID 精确匹配，不能取 aweme_list[0]
    就往下下，否则可能下载到别人的视频。
    """
    if not media_id or not media_id.isdigit():
        return None, False
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
            items = [a for a in (data.get("aweme_list") or []) if isinstance(a, dict)]
            for item in items:
                if str(item.get("aweme_id") or "") == media_id:
                    return item, bool(items)
            # 节点可用但没收录该作品：换下一个节点，不再重复直连
            if items:
                return None, True
            break
    return None, False


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


def _looks_like_note(link: Link) -> bool:
    """链接是否指向抖音图文 / 图集作品。

    图文作品的 URL 形态在各端不一致（PC 复制的是 /note/，移动端可能是 /share/note/
    或带 noteId=、/slides/），但都指向同一个数字 aweme_id，这里统一识别。
    """
    return bool(_NOTE_HINT.search(link.url or ""))


def _is_douyin_online(link: Link) -> bool:
    """是否指向真实的抖音域名。

    SEO 图文通道只在真实抖音域名下启用：离线自检与本地模拟服务用的是
    http://127.0.0.1:<port>/... 这类地址，必须跳过，避免自检误打外网。
    """
    host = (urlparse(link.url or "").hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in
               ("douyin.com", "iesdouyin.com", "amemv.com", "snssdk.com"))


def _seo_image_key(url: str) -> str:
    """图集去重键：同一张原图会在不同 CDN 节点 / 不同会话签名下多次出现，
    以 /tos-cn-i-*/ 之后的文件名（去掉 ~tplv 模板参数与签名 query）作为唯一键。
    """
    m = re.search(r"/tos-cn-i-[\w]+/([^~?\s]+)", url)
    return m.group(1) if m else url.split("?")[0]


def _seo_images_from_html(body: str) -> list[str]:
    """从 SEO 页面里按出现顺序提取图文原图，去重并滤除头像 / logo / 水印图。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _SEO_IMAGE_RE.finditer(body or ""):
        url = html_lib.unescape(m.group(0)).replace("\\/", "/").replace("\\u0026", "&")
        if not url.startswith("https://") or _SEO_IMAGE_SKIP.search(url):
            continue
        key = _seo_image_key(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def _seo_meta(body: str) -> tuple[str, str]:
    """从 SEO 页面里取标题与作者，用于结果展示（取不到返回空串）。"""
    title = ""
    for pattern in (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
                    r"<title[^>]*>([^<]+)</title>"):
        m = re.search(pattern, body or "", re.I)
        if m:
            title = html_lib.unescape(m.group(1)).strip()
            if title:
                break
    author = ""
    # JSON-LD：{"author":{"@type":"Person","name":"知夏AIGC"}}
    m = re.search(r'"author"\s*:\s*\{[^{}]*?"name"\s*:\s*"([^"]{1,40})"', body or "")
    if not m:
        m = re.search(r'"nickname"\s*:\s*"([^"]{1,40})"', body or "")
    if m:
        author = html_lib.unescape(m.group(1)).strip()
    return title, author


def _douyin_seo_gallery(media_id: str, opts: DownloadOptions) -> dict[str, Any]:
    """抓取抖音图文作品的搜索引擎版页面，提取无水印原图（全程免 Cookie）。

    免登录的 feed 接口对图文（/note/）作品不下发，但抖音对搜索引擎开放的 SEO 页面会
    内联无水印原图 CDN。PC 端与移动端复制的图文链接最终都归一到同一个数字 aweme_id，
    因此本通道天然同时支持两端链接。返回 {"images": [...], "title": ..., "author": ...}。
    """
    empty: dict[str, Any] = {"images": [], "title": "", "author": ""}
    if not media_id or not media_id.isdigit():
        return empty
    session = requests.Session()
    session.headers.update({
        "User-Agent": _SPIDER_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        # 不接受压缩：避免中间层下发编码标记错乱的压缩体触发 zlib 解压报错
        "Accept-Encoding": "identity",
    })
    if opts.proxy:
        session.proxies.update({"http": opts.proxy, "https": opts.proxy})
    attempts = (False, True) if not opts.proxy else (False,)
    try:
        for template in _SEO_PAGE_URLS:
            page_url = template.format(mid=media_id)
            for direct in attempts:
                try:
                    session.trust_env = not direct
                    resp = session.get(page_url, timeout=min(opts.timeout, 12),
                                       allow_redirects=True)
                    if resp.status_code >= 400:
                        continue
                    images = _seo_images_from_html(resp.text)
                    if images:
                        title, author = _seo_meta(resp.text)
                        return {"images": images, "title": title, "author": author}
                except Exception:                  # noqa: BLE001 - 换页面 / 换直连重试
                    continue
                finally:
                    session.trust_env = True
    finally:
        session.close()
    return empty


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

    # 画质档位：feed 下发的 bit_rate 档位 +（去水印时）播放网关按需探测出的更高档位。
    # 播放网关返回的始终是无水印源，因此只在「去除水印」模式下并入，避免用户
    # 选择「保留原始文件」时被悄悄换成无水印流。
    variants = _feed_quality_variants(video, strip)
    if strip:
        variants += _feed_high_quality_variants(video, opts, [v["height"] for v in variants])
        if variants:
            variants.sort(key=lambda v: v["height"], reverse=True)
            best = variants[0]["url"]
            if best:
                video_urls = [best] + [u for u in video_urls if u != best]

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
        # 画质增强：按档位标注的每档分辨率直链，供下载时按上限筛选
        "quality_variants": variants,
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
def _resolve_douyin_id(link: Link, opts: DownloadOptions) -> str:
    """当链接里没有数字 aweme_id 时（例如 v.douyin.com 短链落地在非 ID 页面），
    用桌面 UA 抓取规范化地址，再从 URL 路径 / 内嵌 JSON 中提取 aweme_id。
    """
    url = link.url
    mid = _media_id(link)
    if mid:
        return mid
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": DESKTOP_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
        })
        if opts.proxy:
            session.proxies.update({"http": opts.proxy, "https": opts.proxy})
        for direct in (False, True):
            try:
                if direct:
                    session.trust_env = False
                resp = session.get(url, timeout=min(opts.timeout, 15), allow_redirects=True)
                final = resp.url
                mid = _media_id(Link(final, final, "douyin", "抖音"))
                if mid:
                    return mid
                # 内嵌 JSON / og:url / canonical 里常带 aweme_id
                m = re.search(
                    r'(?:aweme_id|noteId|awemeId|note_id|item_id)["\']?\s*[:=]\s*["\']?(\d{8,})',
                    resp.text,
                )
                if m:
                    return m.group(1)
            except Exception:                      # noqa: BLE001 - 换直连重试
                continue
            finally:
                if direct:
                    session.trust_env = True
    except Exception:                              # noqa: BLE001
        pass
    return ""


def _douyin_web_detail(media_id: str, opts: DownloadOptions) -> "dict[str, Any] | None":
    """登录态增强：用用户提供的 Cookie 拉取抖音网页详情接口，返回完整的 aweme 对象。

    免 Cookie 的 feed 接口对视频通常只下发到 720P；而网页详情在登录态下会返回完整的
    bit_rate（含 1080P / 蓝光 / 原画）。该接口匿名访问会被风控 403，因此仅当配置了
    Cookie 时调用，失败则静默回退到 feed 的画质。
    """
    cookie = getattr(opts, "cookie", "")
    if not cookie or not media_id or not media_id.isdigit():
        return None
    session = requests.Session()
    session.headers.update({
        "User-Agent": DESKTOP_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.douyin.com/",
        "Cookie": cookie,
    })
    params = {
        "aweme_id": media_id,
        "aid": "6383",
        "device_platform": "webapp",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "190500",
        "version_name": "19.5.0",
        "cookie_enabled": "true",
    }
    if opts.proxy:
        session.proxies.update({"http": opts.proxy, "https": opts.proxy})
    for direct in (False, True):
        try:
            if direct:
                session.trust_env = False
            resp = session.get("https://www.douyin.com/aweme/v1/web/aweme/detail/",
                               params=params, timeout=min(opts.timeout, 15),
                               allow_redirects=True)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:                       # noqa: BLE001
                    continue
                detail = data.get("aweme_detail") or (data.get("item_list") or [None])[0]
                if isinstance(detail, dict) and detail.get("aweme_id"):
                    return detail
        except Exception:                              # noqa: BLE001 - 换直连重试
            continue
        finally:
            if direct:
                session.trust_env = True
    return None


def _douyin(link: Link, opts: DownloadOptions) -> tuple[list[MediaEntry], str | None]:
    """返回 (条目列表, 诊断信息)。诊断信息非空时说明已尝试但平台未下发该作品。"""
    media_id = _media_id(link)
    if not media_id:
        media_id = _resolve_douyin_id(link, opts)

    # 1. 移动端 Feed：当前抖音主通道，按 aweme_id 精确匹配
    item, got_any = _douyin_feed(media_id, opts)
    entry: MediaEntry | None = None
    if item:
        try:
            entry = _feed_entry(link, item, opts)
        except (ValueError, TypeError, AttributeError):
            entry = None

    # 1.5 登录态增强：用 Cookie 拉取网页详情，解锁 1080P / 蓝光等更高画质
    cookie = getattr(opts, "cookie", "")
    if cookie and media_id and media_id.isdigit():
        web_aweme = _douyin_web_detail(media_id, opts)
        if web_aweme:
            try:
                web_entry = _douyin_build(link, web_aweme, "", opts)
            except (ValueError, TypeError, AttributeError):
                web_entry = None
            if web_entry:
                if not entry:
                    entry = web_entry
                else:
                    wheights = [int(v["height"]) for v in
                                web_entry.raw.get("quality_variants", []) if v.get("height")]
                    eheights = [int(v["height"]) for v in
                                entry.raw.get("quality_variants", []) if v.get("height")]
                    if (max(wheights) if wheights else 0) > (max(eheights) if eheights else 0):
                        entry = web_entry

    # 1.6 图文作品 SEO 通道：免登录 feed 不下发 /note/ 作品，但搜索引擎版页面会内联
    #     无水印原图。只在链接确实是抖音域名时启用，避免离线自检 / 本地模拟服务误打真站。
    if _is_douyin_online(link) and media_id and media_id.isdigit() and (
            not entry or (_looks_like_note(link) and entry.kind == "video")):
        seo = _douyin_seo_gallery(media_id, opts)
        if seo.get("images"):
            seo_title = seo.get("title") or (entry.title if entry else "")
            seo_entry = _gallery_entry(link, seo["images"], seo_title,
                                       seo.get("author") or "")
            seo_entry.raw["channel"] = "seo-note"
            seo_entry.raw["watermark_free"] = True
            return [seo_entry], None

    # feed / 网页详情已经拿到作品，直接返回（不再走 SSR 兜底，避免覆盖）
    if entry:
        return [entry], None

    # 2. 分享页 / 笔记页 SSR 兜底（对部分作品与旧版页面仍然有效）
    candidates: list[str] = []
    if media_id and media_id.isdigit():
        candidates = [
            f"https://www.iesdouyin.com/share/video/{media_id}/",
            f"https://www.iesdouyin.com/share/note/{media_id}/",
            f"https://www.douyin.com/note/{media_id}",
            f"https://www.douyin.com/video/{media_id}",
        ]

    for body, final_url, page_title in _pages(link, opts, candidates):
        objs = _embedded_json(body)
        for entry in _douyin_items(objs):
            try:
                built = _douyin_build(link, entry, page_title, opts)
            except (ValueError, TypeError, AttributeError):
                continue
            if built:
                return [built], None

        # 内嵌 JSON 没拿到，退一步找页面里的 mp4 直链
        from .extractor import video_urls_from_html        # 延迟导入
        videos = [u for u in video_urls_from_html(body, final_url)
                  if not re.search(r"\.(js|css)(\?|$)", u, re.I)]
        if videos:
            return [_video_entry(link, videos[0], page_title)], None

    # 3. 诊断：区分「平台返回了推荐流但没有目标作品」与「完全无法识别」
    if got_any:
        cookie_hint = "" if getattr(opts, "cookie", "") else (
            "若作品确为图文 / 需登录态，可在「下载设置 → 抖音登录态 Cookie」填入你"
            "已登录抖音的 Cookie 后重试（仅发往抖音，本机保存、不上传）。"
        )
        return [], (
            "已尝试免登录接口，但该作品未被返回——抖音对图文 / 需登录态的作品加强了风控，"
            "免 Cookie 通道不再下发数据。" + cookie_hint
        )
    if not media_id:
        # 短链被抖音 302 到首页根路径是常见情形：口令不完整 / 已失效 / 需 App 内打开
        root = (link.url or "").rstrip("/")
        if root in ("https://www.douyin.com", "https://www.iesdouyin.com",
                    "http://www.douyin.com", "http://www.iesdouyin.com"):
            return [], (
                "短链被抖音重定向到了首页，未能定位到具体作品——通常是分享口令复制不完整"
                "或链接已失效。请在抖音 App 里对该作品重新点「分享 → 复制链接」，"
                "把完整口令重新粘贴后再试。"
            )
        return [], "无法从链接中识别作品 ID，请粘贴抖音分享里的完整视频 / 图文链接或口令。"
    return [], None


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
    wanted = ("playUrl", "mainMvUrls", "imageUrls", "atlas", "photoUrl", "caption",
              "ext_params", "extParams")
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

    # 图集（多图作品）。优先级：ext_params.atlas（真图）> photo.atlas > imageUrls。
    # 注意不能用 music.imageUrls 之类兜底 —— 那是播放器配图，会产出错误图片。
    atlas_pool: list[str] = []
    ext_raw = photo.get("ext_params") or photo.get("extParams")
    ext = _decode_jsonish(ext_raw)
    for atlas in (
        (ext.get("atlas") if isinstance(ext, dict) else None),
        photo.get("atlas"),
        photo.get("atlasInfo"),
    ):
        atlas_pool.extend(_atlas_urls(atlas))
    atlas_pool = [u for u in dict.fromkeys(atlas_pool)
                  if u and not re.search(r"(avatar|logo|icon)", u, re.I)]
    if len(atlas_pool) > 1:
        return _gallery_entry(link, atlas_pool, title, author)

    # 单图 / 其它形态的 imageUrls
    image_urls: list[str] = list(atlas_pool)
    image_urls.extend(_url_list(photo.get("imageUrls")))
    image_urls.extend(_url_list(photo.get("images")))
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
