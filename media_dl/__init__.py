"""media_dl - 按链接下载视频 / 图片 / 图集。

最简用法::

    from media_dl import download_text, DownloadOptions

    result = download_text("https://v.douyin.com/xxxx/",
                           DownloadOptions(outdir="downloads", quality="1080"))
    print(result.files, result.errors)

只解析不下载::

    from media_dl import parse_text
    for item in parse_text("8.87 复制打开抖音... https://v.douyin.com/xxx/"):
        print(item.to_dict())
"""

from .downloader import download_entry, download_parsed, download_text, iter_files
from .extractor import extract_images_from_page, parse_link, parse_text
from .links import Link, detect_media, detect_platform, extract_urls, prepare
from .models import DownloadResult, MediaEntry, ParseResult, Progress
from .nocookie import parse_nocookie
from .options import DownloadOptions, has_ffmpeg
from .qr import qr_svg

__version__ = "6.1.0"

__all__ = [
    "download_text", "download_parsed", "download_entry", "iter_files",
    "parse_text", "parse_link", "parse_nocookie", "extract_images_from_page",
    "prepare", "extract_urls", "detect_platform", "detect_media", "Link",
    "DownloadOptions", "DownloadResult", "MediaEntry", "ParseResult", "Progress",
    "has_ffmpeg", "qr_svg", "__version__",
]
