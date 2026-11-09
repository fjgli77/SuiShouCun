"""离线自检：python selftest.py

验证链接清洗、平台/媒体类型识别、免 Cookie 解析、二维码、下载链路与 Web API，
全部使用本机临时 HTTP 服务，不访问外网、不需要真实平台账号或登录态。
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from media_dl import DownloadOptions, download_text, parse_text, prepare
from media_dl.extractor import html_image_urls, parse_link
from media_dl.links import Link, detect_media, detect_platform, extract_urls, guess_kind, safe_filename
from media_dl.nocookie import parse_nocookie
from media_dl.options import has_ffmpeg, ytdlp_opts
from media_dl.qr import make_matrix, qr_svg

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [OK]   {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name} {detail}")


def make_png(width: int = 64, height: int = 64) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + os.urandom(width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1))
            + chunk(b"IEND", b""))


PNG = make_png()
CLIP = os.urandom(8192)


def start_local_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                              # noqa: N802
            port = self.server.server_port
            base = f"http://127.0.0.1:{port}"
            if self.path.startswith("/dead.mp4"):
                self.send_error(500)
            elif self.path.startswith("/feed"):
                # 模拟抖音移动端推荐流：首个条目是干扰项，验证必须按 aweme_id 精确匹配
                query = parse_qs(urlparse(self.path).query)
                aweme_id = (query.get("aweme_id") or [""])[0]
                if aweme_id == "8123456789":
                    item = {
                        "aweme_id": aweme_id, "desc": "移动端通道测试图集",
                        "author": {"nickname": "测试作者"},
                        "images": [
                            {"url_list": [f"{base}/photo.png?wm=1", f"{base}/photo.png?orig=1"],
                             "download_url_list": [f"{base}/photo.png?bad=1"]},
                            {"url_list": [f"{base}/photo2.png"]},
                        ],
                        "video": {},
                    }
                else:
                    item = {
                        "aweme_id": aweme_id, "desc": "移动端通道测试视频 #话题",
                        "author": {"nickname": "测试作者"}, "duration": 74100,
                        "video": {
                            "origin_cover": {"url_list": [f"{base}/photo.png"]},
                            "play_addr": {"url_list": [f"{base}/clip.mp4?node=1",
                                                       f"{base}/clip.mp4?node=2"]},
                            "download_addr": {"url_list": [f"{base}/play/clip.mp4?hd=1"]},
                            "bit_rate": [
                                {"bit_rate": 890402, "is_h265": 1,
                                 "play_addr": {"url_list": [f"{base}/clip.mp4?h265=1"]}},
                                {"bit_rate": 521214, "is_h265": 0,
                                 "play_addr": {"url_list": [f"{base}/clip.mp4?h264=1"]}},
                            ],
                        },
                    }
                payload = {"status_code": 0, "aweme_list": [
                    {"aweme_id": "9999999999", "desc": "干扰推荐作品",
                     "video": {"play_addr": {"url_list": [f"{base}/clip.mp4?wrong=1"]}}},
                    item,
                ]}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/photo"):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(PNG)))
                self.end_headers()
                self.wfile.write(PNG)
            elif self.path.startswith(("/clip.mp4", "/play/clip.mp4", "/playwm/clip.mp4")):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(CLIP)))
                self.end_headers()
                self.wfile.write(CLIP)
            elif self.path.startswith("/video.html"):
                body = f"""<html><head><title>快手视频 - 快手</title></head><body>
<script>window.__APOLLO_STATE__={{"photo":{{"playUrl":"http:\\/\\/127.0.0.1:{port}\\/clip.mp4?x=1"}}}};</script>
<video src="http://127.0.0.1:{port}/clip.mp4"></video>
</body></html>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/douyin.html"):
                body = f"""<html><head><title>抖音视频</title></head><body>
<script>window._ROUTER_DATA = {{"loaderData":{{"video_(7123456789)/page":{{"videoInfoRes":{{"item_list":[
{{"aweme_id":"7123456789","desc":"测试抖音视频 #话题","author":{{"nickname":"测试作者"}},
"video":{{"play_addr":{{"url_list":["{base}/playwm/clip.mp4?x=1"]}},
"cover":{{"url_list":["{base}/photo.png"]}}}}}}]}}}}}}}};</script></body></html>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/douyin_gallery.html"):
                body = f"""<html><head><title>抖音图集</title></head><body>
<script>window._ROUTER_DATA = {{"loaderData":{{"note_(7123456789)/page":{{"videoInfoRes":{{"item_list":[
{{"desc":"测试抖音图文","images":[{{"url_list":["{base}/photo.png"]}},{{"url_list":["{base}/photo2.png"]}}]}}
]}}}}}}}};</script></body></html>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/kuaishou.html"):
                body = f"""<html><head><title>快手视频</title></head><body>
<script>window.__APOLLO_STATE__={{"defaultClient":{{"visionVideoDetailPhoto":{{"photo":
{{"caption":"快手测试视频","playUrl":"{base}/clip.mp4?x=2","coverUrls":["{base}/photo.png"]}}
}}}}}};</script></body></html>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/kuaishou_gallery.html"):
                body = f"""<html><head><title>快手图集</title></head><body>
<script>window.__APOLLO_STATE__={{"defaultClient":{{"photo":{{"caption":"快手测试图集",
"imageUrls":["{base}/photo.png","{base}/photo2.png"]}}}}}};</script></body></html>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/page"):
                body = f"""<html><head><title>Test Gallery - 抖音</title>
<meta property="og:image" content="http://127.0.0.1:{port}/photo.png">
</head><body>
<img data-src="http://127.0.0.1:{port}/photo2.png" />
<img src="http://127.0.0.1:{port}/logo.png" />
</body></html>""".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, *args):                # noqa: A002
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_port


def test_links() -> None:
    print("\n[1] 链接清洗与平台识别")
    share = ("8.87 复制打开抖音，看看【某某的作品】超好看！ https://v.douyin.com/iABCdef/ "
             "复制此链接，打开Dou音搜索，直接观看视频！")
    urls = extract_urls(share)
    check("从分享口令提取 URL", urls == ["https://v.douyin.com/iABCdef"], repr(urls))

    multi = "https://b23.tv/abc123\n【快手】https://v.kuaishou.com/xyz/ 长按复制\nhttps://example.com/a.jpg"
    check("多行批量提取 3 条", len(extract_urls(multi)) == 3, repr(extract_urls(multi)))

    cases = {
        "https://www.douyin.com/video/712345": ("douyin", "抖音"),
        "https://v.douyin.com/iABCdef": ("douyin", "抖音"),
        "https://www.iesdouyin.com/share/video/712345": ("douyin", "抖音"),
        "https://www.bilibili.com/video/BV1xx411c7mD": ("bilibili", "哔哩哔哩"),
        "https://v.kuaishou.com/abcdef": ("kuaishou", "快手"),
        "https://www.kuaishou.com/short-video/abc": ("kuaishou", "快手"),
        "https://www.xiaohongshu.com/explore/abc": ("xiaohongshu", "小红书"),
        "https://www.youtube.com/watch?v=abc": ("youtube", "YouTube"),
        "https://example.com/x": ("generic", "通用网页"),
    }
    for url, expect in cases.items():
        check(f"识别 {expect[1]}", detect_platform(url) == expect, f"{url} -> {detect_platform(url)}")

    links = prepare("https://v.douyin.com/iABC/ https://example.com/pic.jpg", expand=False)
    check("不展开短链时保留原地址", links[0].url.endswith("/iABC") and not links[0].shortlink)
    check("图片直链判为 image", links[1].kind == "image", links[1].kind)
    check("非法文件名字符被清理", "/" not in safe_filename('a/b:c*?d"|e'))


def test_recognition(client) -> None:
    print("\n[2] 平台 / 媒体类型自动识别")
    kinds = {
        "https://www.douyin.com/video/7123456789": ("douyin", "video"),
        "https://www.douyin.com/note/7123456789": ("douyin", "gallery"),
        "https://v.kuaishou.com/abc123": ("kuaishou", "video"),
        "https://www.kuaishou.com/short-video/abc?atlas=1": ("kuaishou", "gallery"),
        "https://example.com/pic.jpg": ("generic", "image"),
        "https://example.com/clip.mp4": ("generic", "video"),
        "https://www.bilibili.com/video/BV1xx411c7mD": ("bilibili", "video"),
        "https://www.bilibili.com/opus/123456": ("bilibili", "gallery"),
    }
    for url, (platform, kind) in kinds.items():
        p, _ = detect_platform(url)
        got = guess_kind(url, p)
        check(f"识别 {platform}/{kind}: {url.split('/')[-1][:20]}",
              p == platform and got == kind, f"got={p}/{got}")

    found = detect_media("看看这个 https://v.douyin.com/iABC/ 还有 https://example.com/a.jpg")
    check("批量本地识别返回中文标签",
          len(found) == 2 and found[0]["platform"] == "douyin"
          and found[0]["kind_name"] == "视频" and found[1]["kind_name"] == "图片",
          repr(found))

    res = client.post("/api/detect", json={
        "text": "https://v.douyin.com/abc/\nhttps://v.kuaishou.com/xyz/\nhttps://example.com/a.jpg"
    })
    data = res.get_json()
    check("识别接口可用且不需口令", res.status_code == 200
          and len(data["results"]) == 3, repr(data)[:300])
    check("识别接口区分抖音/快手/图片",
          [r["platform"] for r in data["results"]] == ["douyin", "kuaishou", "generic"],
          repr(data["results"]))
    check("识别接口返回类型中文名",
          all(r["kind_name"] for r in data["results"]), repr(data["results"]))

    parsed = client.post("/api/parse", json={
        "text": "https://example.com/a.jpg", "options": {"expand_shortlinks": False}
    }).get_json()["results"][0]
    check("解析结果回写确认类型", parsed.get("kind") == "image"
          and parsed.get("kind_name") == "图片", repr(parsed)[:250])


def test_extractor() -> None:
    print("\n[3] 页面图片提取与 yt-dlp 配置")
    html = """<html><head>
    <meta property="og:image" content="https://cdn.example.com/cover.jpg?x=1">
    <meta name="twitter:image" content="https://cdn.example.com/twitter.png">
    <script type="application/ld+json">{"image":"https://cdn.example.com/ld.webp"}</script>
    </head><body>
    <img data-src="https://p3-pc-sign.douyinpic.com/tos-cn-i-abc~tplv-obj:1.jpeg?x=1" />
    <img src="https://cdn.example.com/logo.png" />
    <img src="https://cdn.example.com/photo%20one.jpg" />
    <p>inline: https://cdn.example.com/inline.png</p>
    </body></html>"""
    urls = html_image_urls(html, "https://example.com/post/1")
    check("og:image 优先", urls and "cover.jpg" in urls[0], repr(urls[:2]))
    check("过滤 logo 图标", not any("logo.png" in u for u in urls), repr(urls))
    check("提取 JSON-LD / 懒加载 / 正文内联图",
          any("ld.webp" in u for u in urls) and any("douyinpic" in u for u in urls)
          and any("inline.png" in u for u in urls), repr(urls))

    from unittest.mock import patch
    from media_dl.extractor import video_urls_from_html
    kuaishou_html = r'''<script>window.__APOLLO_STATE__={"photo":{"playUrl":"https:\/\/cdn.kwai.com\/v\/abc.mp4?x=1"}};</script>'''
    vids = video_urls_from_html(kuaishou_html, "https://www.kuaishou.com/short-video/abc")
    check("快手内嵌 JSON 的 mp4 直链可提取",
          bool(vids) and "cdn.kwai.com" in vids[0] and "abc.mp4" in vids[0], repr(vids))

    opts = DownloadOptions(outdir=tempfile.gettempdir(), quality="720", expand_shortlinks=False)
    with patch("media_dl.options.has_ffmpeg", return_value=True):
        cfg = ytdlp_opts(opts, "douyin", extract_only=True)
    check("按画质生成 format", "720" in cfg["format"], cfg["format"])
    check("解析模式下跳过下载", cfg.get("skip_download") is True)
    check("移动端 UA 适配抖音", "iPhone" in cfg["http_headers"]["User-Agent"])
    check("解析错误日志已静默", cfg.get("logger") is not None)
    check("默认不带 Cookie / 代理",
          not cfg.get("cookiefile") and not cfg.get("cookiesfrombrowser") and not cfg.get("proxy"),
          repr({k: cfg.get(k) for k in ("cookiefile", "cookiesfrombrowser", "proxy")}))
    fallback = ytdlp_opts(opts, "douyin", extract_only=True)["format"]
    if has_ffmpeg():
        check("有 ffmpeg 时使用分离流合并", "+ba" in fallback, fallback)
    else:
        # 无 ffmpeg：必须逐级回退（带音轨单文件 -> 视频轨 -> 任意），
        # 写死 "b" 会让 B 站等只有分离轨的站点直接报 "Requested format is not available"
        check("无 ffmpeg 时自动降级单文件 b[acodec!=none]/bv*[acodec!=none]/b/bv*/best",
              "b[acodec!=none]" in fallback and "/bv*" in fallback and fallback.endswith("/best"),
              fallback)


def test_mobile_feed(base: str, outdir: Path) -> None:
    print("\n[4a] 抖音移动端 Feed 免 Cookie 主通道")
    import media_dl.nocookie as nocookie
    from media_dl import download_entry
    from media_dl.extractor import _friendly

    opts = DownloadOptions(outdir=outdir, expand_shortlinks=False, timeout=8)

    def link_for(media_id: str) -> Link:
        url = f"https://www.douyin.com/video/{media_id}"
        return Link(raw=url, url=url, platform="douyin", platform_name="抖音", kind="video")

    with patch.object(nocookie, "FEED_ENDPOINTS", (f"{base}/feed",)):
        video = parse_nocookie(link_for("7123456789"), opts)
        entry = video[0] if video else None
        check("Feed 解析出视频条目", len(video) == 1 and entry.kind == "video",
              repr([e.to_dict() for e in video])[:300])
        check("按 aweme_id 精确匹配，未误取推荐流首条",
              bool(entry) and "移动端通道测试视频" in entry.title, repr(entry.title if entry else ""))
        check("标记为移动端直连 / 免 Cookie 来源",
              bool(entry) and entry.raw.get("channel") == "mobile-feed"
              and entry.source == "nocookie", repr(entry.raw if entry else ""))
        candidates = (entry.raw.get("video_urls") or []) if entry else []
        check("生成多个 CDN / 画质候选直链", len(candidates) >= 4, repr(candidates[:4]))
        check("优先 H.264 码率流且不含水印端点",
              bool(candidates) and "h264=1" in candidates[0], repr(candidates[:2]))
        check("时长毫秒转秒", bool(entry) and abs(entry.duration - 74.1) < 0.01,
              repr(entry.duration if entry else ""))
        check("作者与封面一并解析",
              bool(entry) and entry.uploader == "测试作者" and "photo.png" in entry.thumbnail,
              repr((entry.uploader, entry.thumbnail) if entry else ""))

        gallery = parse_nocookie(link_for("8123456789"), opts)
        gent = gallery[0] if gallery else None
        check("Feed 图集自动识别为图集",
              bool(gent) and gent.kind == "gallery" and len(gent.images) == 2,
              repr([e.to_dict() for e in gallery])[:300])
        check("图集取无水印原图、跳过 download_url_list",
              bool(gent) and "orig=1" in gent.images[0] and "bad=1" not in "".join(gent.images),
              repr(gent.images if gent else ""))

        result = parse_link(link_for("7123456789"), opts)
        check("解析链路提示使用移动端直连通道",
              len(result.entries) == 1 and any("移动端直连通道" in w for w in result.warnings),
              repr(result.warnings))

        files = download_entry(entry, opts)
        check("Feed 视频端到端下载落盘",
              len(files) == 1 and Path(files[0]).stat().st_size >= 1024, repr(files))

        # 模拟首个 CDN 节点 500，验证自动切换备用直链
        from dataclasses import replace
        bad = replace(entry, id="retry_test_0001",
                      raw={**entry.raw,
                           "direct_video_url": f"{base}/dead.mp4",
                           "video_urls": [f"{base}/dead.mp4", f"{base}/clip.mp4"]})
        retry_files = download_entry(bad, opts)
        check("直链失败自动切换备用节点",
              len(retry_files) == 1 and Path(retry_files[0]).stat().st_size >= 1024,
              repr(retry_files))

    friendly = _friendly(Exception("ERROR: [Douyin] 123: Fresh cookies (not necessarily logged in) are needed"))
    check("yt-dlp 风控报错转中文提示", "风控" in friendly and "登录态" in friendly, friendly)

    session = nocookie._api_session(opts)
    check("Feed 会话不带 Cookie / 不强制代理",
          not session.cookies and not session.proxies,
          repr(dict(session.proxies)))
    session.close()


def test_nocookie(base: str, outdir: Path) -> None:
    print("\n[4] 免 Cookie 解析（抖音 / 快手）")
    opts = DownloadOptions(outdir=outdir, expand_shortlinks=False, timeout=8)

    def link_for(path: str, platform: str, name: str) -> Link:
        url = f"{base}/{path}"
        return Link(raw=url, url=url, platform=platform, platform_name=name,
                    kind=guess_kind(url, platform))

    douyin = parse_nocookie(link_for("douyin.html", "douyin", "抖音"), opts)
    check("抖音 _ROUTER_DATA 解析出视频",
          len(douyin) == 1 and douyin[0].kind == "video", repr([e.to_dict() for e in douyin])[:300])
    check("抖音视频标记为免 Cookie 来源",
          bool(douyin) and douyin[0].source == "nocookie", repr(douyin[0].raw if douyin else ""))
    check("抖音 play_addr 去水印",
          bool(douyin) and "/play/" in douyin[0].raw.get("direct_video_url", ""),
          repr(douyin[0].raw if douyin else ""))

    dy_gallery = parse_nocookie(link_for("douyin_gallery.html", "douyin", "抖音"), opts)
    check("抖音图文自动识别为图集",
          len(dy_gallery) == 1 and dy_gallery[0].kind == "gallery"
          and len(dy_gallery[0].images) == 2, repr([e.to_dict() for e in dy_gallery])[:300])

    ks = parse_nocookie(link_for("kuaishou.html", "kuaishou", "快手"), opts)
    check("快手 __APOLLO_STATE__ 解析出视频",
          len(ks) == 1 and ks[0].kind == "video" and "clip.mp4" in ks[0].raw.get("direct_video_url", ""),
          repr([e.to_dict() for e in ks])[:300])

    ks_gallery = parse_nocookie(link_for("kuaishou_gallery.html", "kuaishou", "快手"), opts)
    check("快手图集自动识别为图集",
          len(ks_gallery) == 1 and ks_gallery[0].kind == "gallery"
          and len(ks_gallery[0].images) == 2, repr([e.to_dict() for e in ks_gallery])[:300])

    # 集成：parse_link 优先走免 Cookie 分支，并把确认的类型回写 Link
    link = link_for("douyin.html", "douyin", "抖音")
    result = parse_link(link, opts)
    check("解析链路优先免 Cookie 且回写类型",
          len(result.entries) == 1 and link.kind == "video"
          and result.to_dict()["kind_name"] == "视频", repr(result.to_dict())[:300])

    from media_dl import download_entry
    files = download_entry(douyin[0], opts)
    check("抖音免 Cookie 视频端到端下载落盘",
          len(files) == 1 and Path(files[0]).stat().st_size >= 1024,
          repr(files))
    gallery_files = download_entry(dy_gallery[0], opts)
    check("抖音免 Cookie 图集端到端下载落盘",
          len(gallery_files) == 2, repr(gallery_files))
    ks_files = download_entry(ks[0], opts)
    check("快手免 Cookie 视频端到端下载落盘",
          len(ks_files) == 1 and Path(ks_files[0]).stat().st_size >= 1024,
          repr(ks_files))


def test_download(base: str, outdir: Path) -> None:
    print("\n[5] 端到端下载（本地 HTTP 服务）")
    opts = DownloadOptions(outdir=outdir, expand_shortlinks=False, page_images=True, timeout=8)

    direct = download_text(f"{base}/photo.png", opts)
    check("图片直链下载成功", len(direct.files) == 1 and Path(direct.files[0]).stat().st_size > 128,
          repr(direct))
    check("按平台分目录保存", direct.files and "other" in direct.files[0], repr(direct.files))

    page = parse_text(f"{base}/page.html", opts)[0]
    check("普通网页兜底提取图片", len(page.entries) == 1 and len(page.entries[0].images) >= 2,
          page.to_dict())
    gallery = download_text(f"{base}/page.html", opts)
    check("图集下载落盘", len(gallery.files) >= 2, repr(gallery.files))

    video_page = parse_text(f"{base}/video.html", opts)[0]
    check("页面内嵌 mp4 识别为视频",
          len(video_page.entries) == 1 and video_page.entries[0].kind == "video",
          video_page.to_dict())
    video = download_text(f"{base}/video.html", opts)
    check("直链视频下载成功（快手兜底链路）",
          len(video.files) == 1 and Path(video.files[0]).stat().st_size >= 1024,
          repr(video.files) + repr(video.errors))

    invalid_opts = DownloadOptions(outdir=outdir, expand_shortlinks=False,
                                   page_images=False, timeout=3, retries=1)
    invalid = download_text("http://127.0.0.1:1/no-such-video", invalid_opts)
    check("无效链接返回错误而非崩溃", bool(invalid.errors), repr(invalid.errors))


def test_web_api(base: str, outdir: Path) -> list[dict]:
    print("\n[6] Web API")
    from media_dl.server import app
    client = app.test_client()

    cfg = client.get("/api/config").get_json()
    check("配置接口可用", "outdir" in cfg and "ffmpeg" in cfg)
    check("配置声明 v6 单应用运行时与免 Cookie",
          cfg.get("version") in ("6.0", "6.1") and cfg.get("nocookie") is True
          and cfg.get("runtime") in ("pc", "android")
          and not any(k in cfg for k in ("access_token", "apk_key", "qr_url", "lan_urls")),
          repr(cfg)[:250])
    check("首页可访问", client.get("/").status_code == 200)
    check("静态资源可访问", client.get("/static/app.js").status_code == 200)
    check("PC / 移动 / 兼容入口返回同一个页面",
          client.get("/").data == client.get("/pc").data == client.get("/m").data)

    parsed = client.post("/api/parse", json={
        "text": f"{base}/photo.png",
        "options": {"outdir": str(outdir), "expand_shortlinks": False}
    }).get_json()
    check("解析接口返回条目", bool(parsed.get("results") and parsed["results"][0]["entries"]), repr(parsed)[:300])

    started = client.post("/api/jobs", json={
        "text": f"{base}/photo2.png",
        "options": {"outdir": str(outdir), "expand_shortlinks": False}
    })
    jid = started.get_json()["id"]
    status = ""
    for _ in range(50):
        status = client.get(f"/api/jobs/{jid}").get_json().get("status")
        if status in ("done", "error", "cancelled"):
            break
        time.sleep(0.15)
    job = client.get(f"/api/jobs/{jid}").get_json()
    check("后台任务正常结束", status == "done", f"status={status} errors={job.get('errors')}")
    check("任务产出文件", len(job.get("files") or []) >= 1, repr(job.get("files")))
    files = client.get("/api/files").get_json()
    check("文件列表接口可用", "files" in files)
    return files.get("files") or []


def test_unified_ui(client) -> None:
    print("\n[7] 一个应用 · 一套响应式前端")
    from media_dl.server import _DEFAULT_OUTDIR, _opts_from_request

    page = client.get("/").data.decode("utf-8")
    js = client.get("/static/app.js").data.decode("utf-8")
    css = client.get("/static/style.css").data.decode("utf-8")
    web = Path("media_dl/web")

    check("只有一套前端文件", (web / "index.html").exists()
          and (web / "app.js").exists() and (web / "style.css").exists()
          and not any((web / f).exists() for f in ("mobile.html", "mobile.js", "mobile.css")))
    check("PC / 移动 / 兼容入口都返回同一个页面",
          client.get("/").data == client.get("/m").data == client.get("/pc").data)
    check("两端样式在同一份 CSS：移动端由 body.is-mobile 决定（不再按窗口宽度切换）",
          "--bg: #f4f6fa" in css and "--bg: #0d1222" in css
          and "body.is-mobile" in css
          and "env(safe-area-inset-bottom)" in css
          and "@media (max-width: 760px)" not in css)
    check("移动端大触控底部操作栏", "position: fixed" in css and "min-height: 50px" in css)
    check("按运行端裁剪功能：PC 专属选项与退出按钮（JS 不再按窗口宽度判断）",
          "pc-only" in page and "btn-exit" in page
          and "is-mobile .pc-only" in css and '!MOBILE' in js
          and "matchMedia" not in js)
    check("前端不再有扫码 / 口令 / 连接电脑",
          all(x not in page for x in ("qr-modal", "token-card", "btn-conn", "apk-key"))
          and all(x not in js for x in ("X-Access-Token", "apk_key", "X-MediaDL-Key",
                                        "tryAuth", "showQr", "media_dl_server")))
    check("前端保留粘贴识别、结果预览与失败原因",
          "detect-list" in page and "result-list" in page
          and "renderErrors" in js and "openPreview" in js)
    check("唯一分叉点：APK 原生下载 / 浏览器下载",
          "BRIDGE.download" in js and "onNativeDownload" in js
          and "triggerDownload" in js and "apiUrl(item.download_url)" in js)
    check("两端都不提供打开目录功能",
          client.post("/api/open-folder", json={}).status_code == 404
          and "open-folder" not in js)
    manifest = client.get("/manifest.webmanifest").data.decode("utf-8")
    check("PWA 启动与分享都指向统一入口",
          '"start_url": "/?pwa=1"' in manifest and '"action": "/"' in manifest
          and "192.168" not in manifest)
    check("服务端二维码不依赖第三方组件",
          "import qrcode" not in Path("media_dl/server.py").read_text(encoding="utf-8"))

    opts = _opts_from_request({"options": {
        "outdir": "/tmp/should-be-ignored", "cookies_browser": "chrome",
        "cookie_file": "cookies.txt", "proxy": "http://127.0.0.1:7890",
    }})
    check("忽略客户端指定的保存目录", str(opts.outdir) == str(_DEFAULT_OUTDIR.resolve()), str(opts.outdir))
    check("强制清空 Cookie 与代理",
          not opts.cookies_browser and not opts.cookie_file and not opts.proxy, repr(opts.to_dict()))

    preflight = client.options("/api/jobs", headers={
        "Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,range",
    })
    check("跨域预检返回 204", preflight.status_code == 204, str(preflight.status_code))
    check("CORS 不再放行旧口令请求头",
          "X-Access-Token" not in (preflight.headers.get("Access-Control-Allow-Headers") or ""),
          repr(dict(preflight.headers)))
    check("CORS 回显来源",
          "127.0.0.1:5173" in (preflight.headers.get("Access-Control-Allow-Origin") or ""),
          repr(preflight.headers.get("Access-Control-Allow-Origin")))


def test_qrcode(client) -> None:
    print("\n[8] 内置二维码")
    from urllib.parse import quote
    svg = qr_svg("https://example.com/share?text=hello")
    check("SVG 可直接渲染", svg.startswith("<svg") and "<path" in svg and "fill=" in svg, svg[:80])
    resp = client.get("/api/qrcode?data=" + quote("https://example.com/share?text=hello", safe=""))
    check("二维码接口返回 SVG", resp.status_code == 200 and b"<svg" in resp.data,
          f"status={resp.status_code}")
    check("二维码内容校验", client.get("/api/qrcode?data=abc").status_code == 400)

    try:
        import qrcode
        from qrcode.util import QRData, MODE_8BIT_BYTE
    except ImportError:
        check("与标准库矩阵一致（标准库未装，仅验证自洽）", len(make_matrix("TEST")) == 21)
        return
    ok = True
    for text in ("HELLO 123", "https://example.com/share?text=hello"):
        for mask in (0, 3, 7):
            q = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L,
                              box_size=1, border=0, mask_pattern=mask)
            q.add_data(QRData(text.encode("utf-8"), mode=MODE_8BIT_BYTE))
            q.make(fit=True)
            ref, mine = q.get_matrix(), make_matrix(text, mask=mask)
            if len(ref) != len(mine) or any(ref[r][c] != mine[r][c]
                                            for r in range(len(ref)) for c in range(len(ref))):
                ok = False
    check("逐模块与标准库一致（3 种掩码 × 2 组内容）", ok)


def test_browser_download(base: str, client, outdir: Path) -> None:
    print("\n[10] 浏览器接管下载（票据 / 流式 / zip / 服务端不落盘）")
    import io
    import zipfile

    from media_dl.server import _DEFAULT_OUTDIR

    cfg = client.get("/api/config").get_json()
    check("配置声明浏览器下载 / 预览 / 去水印 / v6.0",
          cfg.get("browser_download") is True and cfg.get("watermark_free") is True
          and cfg.get("version") in ("6.0", "6.1") and cfg.get("preview") is True,
          repr(cfg)[:220])

    before = {str(p) for p in _DEFAULT_OUTDIR.rglob("*") if p.is_file()}

    def parse_one(text: str) -> dict:
        resp = client.post("/api/parse", json={
            "text": text, "options": {"expand_shortlinks": False, "page_images": True}
        })
        check(f"解析成功 {text.rsplit('/', 1)[-1]}", resp.status_code == 200, str(resp.status_code))
        return resp.get_json()

    # 图片直链
    data = parse_one(f"{base}/photo.png")
    ticket = data.get("ticket") or ""
    items = data.get("items") or []
    check("解析返回下载票据与下载项", bool(ticket) and len(items) == 1, repr(data)[:250])
    check("下载项指向 /api/fetch 而非磁盘路径",
          bool(items) and items[0]["download_url"].startswith("/api/fetch?ticket="),
          repr(items[:1]))
    resp = client.get(items[0]["download_url"])
    disp = resp.headers.get("Content-Disposition") or ""
    check("图片以 attachment 交给浏览器",
          resp.status_code == 200 and "attachment" in disp and ".png" in disp,
          f"{resp.status_code} {disp[:120]}")
    check("图片内容与源站一致", resp.data == PNG, f"{len(resp.data)} bytes")
    check("响应类型为图片", "image/png" in (resp.headers.get("Content-Type") or ""))

    # 视频直链（页面内嵌 mp4）
    vdata = parse_one(f"{base}/video.html")
    vitems = vdata.get("items") or []
    vresp = client.get(vitems[0]["download_url"]) if vitems else None
    check("视频以 attachment 交给浏览器",
          vresp is not None and vresp.status_code == 200
          and ".mp4" in (vresp.headers.get("Content-Disposition") or ""),
          str(vresp.status_code if vresp else None))
    check("视频内容与源站一致", vresp is not None and vresp.data == CLIP,
          f"{len(vresp.data) if vresp else 0} bytes")

    # 图集打包 zip
    gdata = parse_one(f"{base}/page.html")
    gitems = gdata.get("items") or []
    check("图集识别为多文件并标记 zip",
          bool(gitems) and gitems[0]["count"] >= 2 and gitems[0]["mode"] == "zip",
          repr(gitems[:1]))
    zresp = client.get(gitems[0]["download_url"]) if gitems else None
    zip_ok = False
    names: list[str] = []
    if zresp is not None and zresp.status_code == 200:
        try:
            with zipfile.ZipFile(io.BytesIO(zresp.data)) as zf:
                names = zf.namelist()
                zip_ok = len(names) >= 2 and all(zf.read(n)[:4] == b"\x89PNG" for n in names)
        except zipfile.BadZipFile:
            pass
    check("图集一次打包为可打开的 zip", zip_ok, repr(names))
    check("zip 以附件形式下载",
          zresp is not None and "attachment" in (zresp.headers.get("Content-Disposition") or "")
          and ".zip" in (zresp.headers.get("Content-Disposition") or ""))
    one = client.get(gitems[0]["download_url"] + "&j=0&zip=0")
    check("图集支持单张另存", one.status_code == 200 and one.data[:4] == b"\x89PNG")

    # 票据安全：只认解析结果，不接受任意 URL
    check("伪造票据被拒绝", client.get("/api/fetch?ticket=forged&i=0").status_code == 404)
    check("越界序号被拒绝",
          client.get(f"/api/fetch?ticket={ticket}&i=99").status_code == 404)
    check("缺少票据返回 400", client.get("/api/fetch?i=0").status_code == 400)
    check("下载项不暴露任意外部直链",
          all("http" not in str(it.get("download_url")) for it in items))

    after = {str(p) for p in _DEFAULT_OUTDIR.rglob("*") if p.is_file()}
    check("浏览器下载不往服务端 downloads/ 写文件", after == before,
          f"新增 {sorted(after - before)[:3]}")


def test_watermark(base: str, outdir: Path) -> None:
    print("\n[11] 去水印直链选择")
    import media_dl.nocookie as nocookie
    from media_dl.nocookie import _feed_video_urls, _strip_watermark

    video = {
        "play_addr": {"url_list": [f"{base}/play/clip.mp4?src=play_addr"]},
        "download_addr": {"url_list": [f"{base}/download/clip.mp4?src=download_addr"]},
        "bit_rate": [
            {"bit_rate": 980000, "is_h265": 0,
             "play_addr": {"url_list": [f"{base}/play/clip.mp4?src=h264"]}},
            {"bit_rate": 1200000, "is_h265": 1,
             "play_addr": {"url_list": [f"{base}/play/clip.mp4?src=h265"]}},
        ],
    }
    urls = _feed_video_urls(video)
    check("H.264 无水印 /play/ 通道排在首位", bool(urls) and "h264" in urls[0], repr(urls))
    check("带水印的 download_addr 降级为最后兜底",
          bool(urls) and "download_addr" in urls[-1], repr(urls[-1:]))
    check("所有首选直链不含水印端点",
          all("playwm" not in u and "watermark=1" not in u for u in urls[:-1]))
    check("playwm 地址改写为无水印 /play/",
          "/play/" in _strip_watermark(f"{base}/playwm/clip.mp4"))

    opts = DownloadOptions(outdir=outdir, expand_shortlinks=False, timeout=8)
    url = "https://www.douyin.com/video/7123456789"
    link = Link(raw=url, url=url, platform="douyin", platform_name="抖音", kind="video")
    with patch.object(nocookie, "FEED_ENDPOINTS", (f"{base}/feed",)):
        entries = parse_nocookie(link, opts)
    entry = entries[0] if entries else None
    check("Feed 结果标记无水印",
          bool(entry) and entry.raw.get("watermark_free") is True
          and entry.to_dict().get("watermark_free") is True,
          repr(entry.raw if entry else ""))
    candidates = (entry.raw.get("video_urls") or []) if entry else []
    check("Feed 候选里带水印的下载地址排最后",
          bool(candidates) and "hd=1" in candidates[-1], repr(candidates[-2:]))
    check("图集排除带 watermark 的图片地址",
          "watermark" not in "".join((entry.images if entry else [])))


def test_local_only(client, files: list[dict]) -> None:
    print("\n[9] 只在本机运行 · 无连接 / 无口令 / 无发现")
    from urllib.parse import quote
    from media_dl import server as srv

    remote = {"REMOTE_ADDR": "192.168.1.99"}
    check("服务端没有局域网发现 / 口令 / 客户端密钥实现",
          not any(hasattr(srv, n) for n in ("start_discovery", "rotate_token",
                                            "ensure_token", "local_ips", "lan_urls",
                                            "_is_apk_request", "_auth_ok")))
    check("配置里不含任何连接层字段",
          all(k not in client.get("/api/config").get_json()
              for k in ("access_token", "apk_key", "qr_url", "lan_urls",
                        "discovery_port", "token_ttl_seconds", "lan")))
    check("非本机请求一律拒绝",
          client.get("/api/config", environ_base=remote).status_code == 403
          and client.post("/api/parse", json={"text": "x"},
                          environ_base=remote).status_code == 403)
    check("本机请求无需任何口令",
          client.get("/api/files").status_code == 200
          and client.post("/api/auth", json={}).status_code == 200)
    rules = {r.rule for r in srv.app.url_map.iter_rules()}
    check("旧的刷新口令接口已移除", "/api/token/refresh" not in rules)
    check("保留 /m /pc 作为兼容别名", "/m" in rules and "/pc" in rules)
    launcher = Path("desktop_app.py").read_text(encoding="utf-8")
    check("PC 启动器只监听 127.0.0.1",
          '"127.0.0.1"' in launcher and "lan=True" not in launcher
          and 'runtime="pc"' in launcher)
    for path in ("/manifest.webmanifest", "/sw.js", "/static/icon.svg", "/pc", "/m"):
        check("统一入口资源 " + path, client.get(path).status_code == 200)
    if files:
        q = "?path=" + quote(files[0]["path"], safe="")
        dl = client.get("/api/download" + q)
        check("旧 CLI 文件下载接口仍可用",
              dl.status_code == 200
              and "attachment" in (dl.headers.get("Content-Disposition") or ""),
              str(dl.status_code))
        check("旧 CLI 文件预览接口仍可用", client.get("/api/file" + q).status_code == 200)
    else:
        check("旧 CLI 文件下载接口仍可用", False, "无测试文件")
        check("旧 CLI 文件预览接口仍可用", False, "无测试文件")


def test_v6(client, base: str, outdir: Path) -> None:
    print("\n[12] v6：一个应用 · APK 自包含 CPython · 去水印开关 · 预览 · 失败原因")
    import zipfile as _zf
    from media_dl.delivery import _video_candidates
    from media_dl.extractor import _is_definite_video_page
    from media_dl.models import MediaEntry
    from media_dl.server import app
    client = app.test_client()

    # ---- 1. 交付物与「两端各自本机运行」的架构 ----
    dist = Path("dist")
    check("PC 端 EXE 产物存在", (dist / "随手存-电脑端.exe").exists())
    check("移动端 APK 产物存在", (dist / "随手存-手机版.apk").exists())
    launcher = Path("desktop_app.py").read_text(encoding="utf-8")
    check("桌面启动器在本机启动同一服务",
          "run_server(" in launcher and '"127.0.0.1"' in launcher
          and 'runtime="pc"' in launcher)
    java = Path("android/java/com/suishoucun/app/MainActivity.java").read_text(encoding="utf-8")
    jni = Path("android/jni/pystart.c").read_text(encoding="utf-8")
    manifest = Path("android/AndroidManifest.xml").read_text(encoding="utf-8")
    entry = Path("media_dl/android_entry.py").read_text(encoding="utf-8")
    check("APK 内嵌 CPython 并启动同一个 Flask 服务",
          "PyService.nativeStart" in java and "Py_InitializeFromConfig" in jni
          and "run_server(" in entry and 'runtime="android"' in entry)
    check("APK 不再连接任何电脑",
          all(x not in java for x in ("DISCOVERY_PORT", "MEDIADL_DISCOVER",
                                      "X-MediaDL-Key", "apk_key", "connectBox"))
          and "CHANGE_WIFI_MULTICAST_STATE" not in manifest)
    check("APK 保留原生下载与本机明文 HTTP",
          "MediaStore.Downloads" in java and "addJavascriptInterface" in java
          and "usesCleartextTraffic" in manifest
          and 'extractNativeLibs="true"' in manifest)
    apk = dist / "随手存-手机版.apk"
    if apk.exists():
        with _zf.ZipFile(apk) as z:
            names = set(z.namelist())
        check("APK 内含标准库与同一份 media_dl 源码 / 前端",
              "assets/py/stdlib.zip" in names
              and "assets/py/app/media_dl/server.py" in names
              and "assets/py/app/media_dl/web/index.html" in names
              and "assets/py/app/media_dl/web/app.js" in names)
        check("APK 内含 CPython 与原生扩展模块",
              # 运行时 .so 随 lib/ 打包；标准库 C 扩展改由 assets/py/native/<ABI>/
              # 打包，运行时解压到 lib-dynload（见 android/build_apk.py）
              "lib/arm64-v8a/libpython3.13.so" in names
              and "lib/arm64-v8a/libpystart.so" in names
              and any(n.startswith("assets/py/native/arm64-v8a/_socket.") for n in names))
        check("APK 内含纯 Python 依赖",
              "assets/py/site/flask/__init__.py" in names
              and "assets/py/site/yt_dlp/__init__.py" in names
              and "assets/py/site/certifi/cacert.pem" in names)
    else:
        for label in ("APK 内含标准库与同一份 media_dl 源码 / 前端",
                      "APK 内含 CPython 与原生扩展模块", "APK 内含纯 Python 依赖"):
            check(label, False, "APK 不存在")

    # ---- 2. 去水印开关 ----
    off = client.post("/api/parse", json={
        "text": f"{base}/photo.png",
        "options": {"expand_shortlinks": False, "remove_watermark": False}
    }).get_json()
    on = client.post("/api/parse", json={
        "text": f"{base}/photo.png",
        "options": {"expand_shortlinks": False, "remove_watermark": True}
    }).get_json()
    check("下载设置可关闭去水印",
          off.get("remove_watermark") is False
          and off["items"][0]["watermark_option"] == "keep", repr(off)[:200])
    check("下载设置可开启去水印",
          on.get("remove_watermark") is True
          and on["items"][0]["watermark_option"] == "remove", repr(on)[:200])
    fake = MediaEntry(id="x", title="t", kind="video", url="u", raw={
        "video_urls": [f"{base}/play/a.mp4"],
        "watermarked_video_urls": [f"{base}/download/a.mp4"]})
    check("勾选去水印优先 /play/ 无水印源", "/play/" in _video_candidates(fake, True)[0])
    check("取消勾选保留平台原始地址", "/download/" in _video_candidates(fake, False)[0])

    # ---- 3. 解析结果预览与失败原因 ----
    item = on["items"][0]
    pv = client.get(item["preview_url"])
    check("结果栏预览以 inline 返回",
          pv.status_code == 200 and "inline" in (pv.headers.get("Content-Disposition") or ""),
          f"{pv.status_code} {pv.headers.get('Content-Disposition')}")
    rng = client.get(item["preview_url"], headers={"Range": "bytes=0-99"})
    check("视频预览支持 Range 请求", rng.status_code in (200, 206), str(rng.status_code))
    thumb = client.get(item["thumb_url"])
    check("结果栏封面缩略图可用",
          thumb.status_code == 200
          and (thumb.headers.get("Content-Type") or "").startswith("image/"),
          str(thumb.status_code))
    page = client.get("/").data.decode("utf-8")
    js = client.get("/static/app.js").data.decode("utf-8")
    check("统一页面含预览弹层与失败原因渲染",
          "preview-modal" in page and "renderErrors" in js and "result-errors" in page)
    check("统一页面显示解析选项与去水印开关",
          "opt-remove-watermark" in page and "settings-card" in page)
    check("两端共用同一套 API，无连接层残留",
          "native_download" in client.get("/api/config").get_json()
          and "media_dl_server" not in js)

    # ---- 3b. 移动端抖音登录态：系统内不自建登录，只读取移动端已有登录态 ----
    check("登录态只从移动端抖音读取（系统内不提供登录流程）",
          "DY_MOBILE_URL" in java and "m.douyin.com" in java
          and "验证码登录" not in java and "OPEN_LOGIN_JS" not in java)
    check("读取抖音前先 flush Cookie 落盘",
          "CookieManager.getInstance().flush()" in java)
    check("登录态判定只认真实会话键（排除匿名会话字段）",
          'new String[]{"sessionid_ss", "sessionid", "sid_tt", "sid_guard"}' in java)
    check("支持静默探测移动端登录态（免打扰）",
          "probeDouyinCookieSilently" in java and "probeDouyinCookie" in js)
    check("捕获到的登录态回写本机服务自动使用",
          "/api/set-cookie" in java and "/api/cookie" in js
          and "onCookieSaved" in js)

    # ---- 3b-2. JS 桥回调必须切回主线程（否则 new WebView throw IllegalStateException 闪退） ----
    # @JavascriptInterface 方法运行在 WebView 的 JavaBridge 线程；在其中直接创建 View
    # 会抛「Calling View methods on another thread than the UI thread」导致应用启动即闪退。
    _bridge = java.split("class JsBridge", 1)[-1].split("\n    }", 1)[0]
    check("JS 桥 probeDouyinCookie 切回主线程",
          "public void probeDouyinCookie()" in _bridge
          and "runOnUiThread" in _bridge.split("probeDouyinCookie()")[1][:220])
    check("JS 桥 openDouyinLogin 切回主线程",
          "public void openDouyinLogin()" in _bridge
          and "runOnUiThread" in _bridge.split("openDouyinLogin()")[1][:220])
    check("openDouyinWeb 内部主线程兜底（防任意调用方误用）",
          "Looper.myLooper() != android.os.Looper.getMainLooper()" in java)

    # ---- 3c. 移动端剪贴板：非安全上下文下走原生桥 ----
    check("移动端粘贴走原生剪贴板桥",
          "readClipboard" in java and "ClipboardManager" in java
          and "readClipboard" in js)

    # ---- 4. B站番剧不再被封面误判成图片 ----
    ep = "https://www.bilibili.com/bangumi/play/ep321808/?share_source=copy_web"
    link = Link(raw=ep, url=ep, platform="bilibili",
                platform_name="哔哩哔哩", kind="video")
    check("番剧链接识别为明确视频页面", _is_definite_video_page(link))
    with patch("media_dl.extractor._bilibili_bangumi",
               return_value=(None, "模拟：该集需要大会员")), \
         patch("media_dl.extractor._parse_with_ytdlp",
               side_effect=RuntimeError("模拟：yt-dlp 解析失败")), \
         patch("media_dl.extractor._video_from_page", return_value=None), \
         patch("media_dl.extractor.extract_images_from_page",
               return_value=([f"{base}/photo.png"], "封面")):
        result = parse_link(link, DownloadOptions(
            outdir=outdir, page_images=True, expand_shortlinks=False))
    check("番剧解析失败时不再回落成图片",
          not result.entries and result.media_type == "video",
          f"entries={len(result.entries)} kind={result.media_type}")
    check("解析结果栏能拿到明确失败原因",
          "视频" in str(result.error) and bool(result.to_dict().get("warnings")),
          str(result.error)[:160])


def main() -> int:
    server, port = start_local_server()
    base = f"http://127.0.0.1:{port}"
    outdir = Path(tempfile.mkdtemp(prefix="media_dl_selftest_"))
    try:
        from media_dl.server import app
        client = app.test_client()
        # 默认关闭外网 Feed 通道，保证自检完全离线；Feed 专项测试自行指向本地模拟服务
        with patch("media_dl.nocookie.FEED_ENDPOINTS", ()):
            test_links()
            test_recognition(client)
            test_extractor()
            test_mobile_feed(base, outdir)
            test_nocookie(base, outdir)
            test_download(base, outdir)
            files = test_web_api(base, outdir)
            test_unified_ui(client)
            test_qrcode(client)
            test_browser_download(base, client, outdir)
            test_watermark(base, outdir)
            test_local_only(client, files)
            test_v6(client, base, outdir)
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(outdir, ignore_errors=True)
    print(f"\n结果: {PASSED} 通过, {FAILED} 失败")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
