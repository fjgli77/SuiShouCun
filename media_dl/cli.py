"""命令行入口：python -m media_dl [选项] <链接或分享口令...>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .extractor import parse_text
from .models import Progress
from .options import DownloadOptions, has_ffmpeg


def _setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="media_dl",
        description="按分享链接下载视频 / 图片 / 图集（抖音、B站、快手、小红书、微博等）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python -m media_dl \"8.87 复制打开抖音... https://v.douyin.com/xxx/\"\n"
            "  python -m media_dl -o D:/media -q 1080 https://www.bilibili.com/video/BV1xx411c7mD\n"
            "  python -m media_dl --audio-only -f links.txt\n"
            "  python -m media_dl --parse-only --json https://v.kuaishou.com/xxxx\n"
            "  python -m media_dl --web             # 本机界面（PC / Android 同一套）\n"
            "  python -m media_dl --web --app       # 以独立桌面窗口打开\n"
        ),
    )
    p.add_argument("links", nargs="*", help="一个或多个链接，也可直接粘贴整段分享口令")
    p.add_argument("-o", "--outdir", default="downloads", help="保存目录（默认 ./downloads）")
    p.add_argument("-q", "--quality", default="best",
                   help="最高画质高度：best/2160/1080/720/480/360（默认 best）")
    p.add_argument("-a", "--audio-only", action="store_true", help="只下载音频")
    p.add_argument("--audio-format", default="mp3", help="音频格式（默认 mp3，需 ffmpeg）")
    p.add_argument("--thumbnail", action="store_true", help="同时保存视频封面")
    p.add_argument("--max-images", type=int, default=30, help="单个图集最多下载张数（默认 30）")
    p.add_argument("--no-page-images", action="store_true", help="解析失败时不再从网页抓图片")
    p.add_argument("--cookies-from-browser", default="",
                   help="从浏览器读取 Cookie：chrome/edge/firefox/brave/opera（部分内容需登录）")
    p.add_argument("--cookie-file", default="", help="Netscape 格式 cookies.txt 路径")
    p.add_argument("--proxy", default="", help="代理，如 http://127.0.0.1:7890")
    p.add_argument("--timeout", type=float, default=20.0, help="网络超时秒数（默认 20）")
    p.add_argument("-f", "--file", default="", help="从文本文件逐行读取链接")
    p.add_argument("--stdin", action="store_true", help="从标准输入读取分享文本")
    p.add_argument("--parse-only", action="store_true", help="只解析，不下载")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p.add_argument("--web", action="store_true", help="启动本机界面（PC / Android 同一套）")
    p.add_argument("--port", type=int, default=8765, help="网页界面端口（默认 8765）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机）")
    p.add_argument("--open", action="store_true", help="启动后用默认浏览器打开界面")
    p.add_argument("--app", action="store_true",
                   help="以 Chrome/Edge 独立窗口打开（PC 端近似桌面应用）")
    p.add_argument("--no-expand", action="store_true", help="不预展开短链")
    p.add_argument("-V", "--version", action="version", version=f"media_dl {__version__}")
    return p


def _collect_text(args: argparse.Namespace) -> str:
    chunks = list(args.links)
    if args.file:
        path = Path(args.file).expanduser()
        if not path.exists():
            raise SystemExit(f"找不到链接文件: {path}")
        chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
    if args.stdin or (not chunks and not sys.stdin.isatty()):
        chunks.append(sys.stdin.read())
    return "\n".join(chunks).strip()


def _options_from_args(args: argparse.Namespace) -> DownloadOptions:
    return DownloadOptions(
        outdir=Path(args.outdir),
        quality=args.quality,
        audio_only=args.audio_only,
        audio_format=args.audio_format,
        write_thumbnail=args.thumbnail,
        max_images=args.max_images,
        page_images=not args.no_page_images,
        cookies_browser=args.cookies_from_browser,
        cookie_file=args.cookie_file,
        proxy=args.proxy,
        timeout=args.timeout,
        expand_shortlinks=not args.no_expand,
    ).normalized()


def _print_progress(p: Progress) -> None:
    if p.status == "downloading":
        bar_len = 24
        filled = int(p.percent / 100 * bar_len)
        bar = "#" * filled + "-" * (bar_len - filled)
        sys.stderr.write(f"\r  [{bar}] {p.percent:5.1f}%  {p.message[:40]:<40}")
        sys.stderr.flush()
    elif p.status == "finished":
        sys.stderr.write("\r" + " " * 90 + "\r")
        print(f"  [完成] {p.title} -> {len(p.files)} 个文件  {p.message}")
    elif p.status == "error":
        sys.stderr.write("\r" + " " * 90 + "\r")
        print(f"  [失败] {p.title}: {p.message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    args = build_parser().parse_args(argv)

    if args.web:
        from .server import run_server
        run_server(
            host=args.host, port=args.port, outdir=Path(args.outdir),
            open_browser=args.open or args.app, app_mode=args.app,
        )
        return 0

    text = _collect_text(args)
    if not text:
        print("请提供链接，或粘贴分享口令；使用 --web 打开图形界面，--help 查看全部参数。")
        return 1

    opts = _options_from_args(args)
    if not has_ffmpeg() and not args.json:
        print("提示: 未检测到 ffmpeg，视频将下载为自带音轨的单一文件；"
              "安装 ffmpeg 后可自动合并更高画质并支持音频提取。", file=sys.stderr)

    if args.parse_only:
        results = parse_text(text, opts)
        if args.json:
            print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
        else:
            for r in results:
                print(f"\n{r.link.platform_name}  {r.link.url}")
                if r.error:
                    print(f"  [错误] {r.error}")
                for e in r.entries:
                    extra = f" ({len(e.images)} 张)" if e.images else ""
                    print(f"  - [{e.kind}{extra}] {e.title}  by {e.uploader or '-'}")
                for w in r.warnings:
                    print(f"  [提示] {w}")
        return 0 if any(r.entries for r in results) else 2

    from .downloader import download_text
    result = download_text(text, opts, on_progress=None if args.json else _print_progress)
    for r in result.parsed:
        if r.error and not args.json:
            print(f"[解析失败] {r.link.url}: {r.error}", file=sys.stderr)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"\n保存到: {opts.outdir}")
        print(f"文件 {len(result.files)} 个，失败 {len(result.errors)} 项")
        for f in result.files[:20]:
            print("  " + f)
        for e in result.errors:
            print("  [错误] " + e, file=sys.stderr)
    return 0 if result.files else 2


if __name__ == "__main__":
    raise SystemExit(main())
