"""统一数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .links import KIND_NAMES, Link


@dataclass
class MediaEntry:
    """一个可下载单元：一条视频、一张图片，或一组图集。"""
    id: str
    title: str
    kind: str                        # video | image | gallery
    url: str                         # 交给 yt-dlp 的页面地址，或图片直链
    platform: str = "generic"
    platform_name: str = "通用网页"
    thumbnail: str = ""
    uploader: str = ""
    duration: float | None = None
    images: list[str] = field(default_factory=list)   # 图集 / 页面图片
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return max(1, len(self.images)) if self.kind in ("image", "gallery") else 1

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, "待识别")

    @property
    def source(self) -> str:
        """解析来源：nocookie（免登录页面解析）/ ytdlp / page 等。"""
        return str(self.raw.get("source") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "kind_name": self.kind_name,
            "url": self.url,
            "platform": self.platform,
            "platform_name": self.platform_name,
            "thumbnail": self.thumbnail,
            "uploader": self.uploader,
            "duration": self.duration,
            "images": self.images,
            "count": self.count,
            "source": self.source,
            "watermark_free": bool(self.raw.get("watermark_free")),
            "channel": str(self.raw.get("channel") or ""),
        }


@dataclass
class ParseResult:
    link: Link
    entries: list[MediaEntry] = field(default_factory=list)
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    # 合集 / 系列信息：同一链接解析出多条视频时由解析器填充（如 B 站合集、番剧系列）
    collection: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.entries) and not self.error

    @property
    def media_type(self) -> str:
        """解析后确认的媒体类型：多条不一致时按第一条，图集优先展示。"""
        if not self.entries:
            return self.link.kind
        kinds = [e.kind for e in self.entries]
        if "gallery" in kinds:
            return "gallery"
        if "image" in kinds and "video" not in kinds:
            return "image"
        return kinds[0]

    @property
    def media_type_name(self) -> str:
        return KIND_NAMES.get(self.media_type, "待识别")

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.link.raw,
            "url": self.link.url,
            "platform": self.link.platform,
            "platform_name": self.link.platform_name,
            "kind": self.media_type,
            "kind_name": self.media_type_name,
            "shortlink": self.link.shortlink,
            "entries": [e.to_dict() for e in self.entries],
            "error": self.error,
            "warnings": self.warnings,
            "collection": self.collection,
        }


@dataclass
class Progress:
    """下载进度，供 CLI / Web 界面消费。"""
    entry_id: str
    status: str                      # pending | downloading | finished | error | skipped
    title: str = ""
    percent: float = 0.0
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    eta: int | None = None
    message: str = ""
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "status": self.status,
            "title": self.title,
            "percent": round(self.percent, 1),
            "downloaded": self.downloaded,
            "total": self.total,
            "speed": self.speed,
            "eta": self.eta,
            "message": self.message,
            "files": self.files,
        }


@dataclass
class DownloadResult:
    parsed: list[ParseResult] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.files) and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "parsed": [p.to_dict() for p in self.parsed],
            "files": self.files,
            "errors": self.errors,
            "ok": self.ok,
        }
