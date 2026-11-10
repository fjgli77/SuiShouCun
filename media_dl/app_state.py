"""随手存 · 跨重启、跨升级都持久的应用状态（首启标记等）。

为什么不能存在「包目录」里 —— 两种打包形态都会把它清掉：

- **PC EXE**：PyInstaller `--onefile` 每次运行都把资源解压到
  `%TEMP%\\_MEIxxxxxx`，进程退出即整目录删除；`__file__` 指向的就是那里，
  于是任何写在包目录旁的标记，下次启动必然是空的。
- **Android APK**：`MainActivity.prepareHome()` 用 `assets/py/VERSION` 做戳记，
  版本一变就 `deleteRecursively(filesDir/py)` 整目录重建，包目录内的状态同样丢。

结果就是「每次启动都当第一次」，首启引导永远关不掉。因此状态必须落在
一个独立、持久的目录：

- 显式 `MEDIADL_DATA`（自检 / 特殊部署用，可完全隔离）；
- Android：`MEDIADL_HOME` 的上一级 `data/`（即 `filesDir/data`，升级不丢）；
- Windows EXE：`%LOCALAPPDATA%\\SuiShouCun`（与日志同处，用户可预期）；
- 其它 / 开发态：包目录 `data/`。

写盘一律「临时文件 + 原子替换」，避免断电 / 强杀留下半截 JSON。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_CACHE: dict[str, Any] | None = None

_STATE_FILE = "state.json"


# --------------------------------------------------------------------------- #
# 目录解析
# --------------------------------------------------------------------------- #
def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def legacy_data_dir() -> Path:
    """旧版（v6.2 及以前）的数据目录：包目录下的 data/。

    仅用于**读取**迁移：老用户的 Cookie 存这里，升级后不能丢。
    Android/EXE 下这个目录本身不可靠，不作为写入目标。
    """
    return _package_dir() / "data"


def state_dir() -> Path:
    """当前形态下的持久状态目录（只负责解析，不创建）。"""
    explicit = (os.environ.get("MEDIADL_DATA") or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    home = (os.environ.get("MEDIADL_HOME") or "").strip()
    if home:
        # Android：MEDIADL_HOME=filesDir/py，data 放它上一级，规避版本变更时的整目录重建
        try:
            return Path(home).expanduser().parent / "data"
        except Exception:                              # noqa: BLE001
            pass

    if getattr(sys, "frozen", False):
        # PC EXE：%LOCALAPPDATA%\SuiShouCun（与 app.log 一致）
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = tempfile.gettempdir()
        return Path(base) / "SuiShouCun"

    # 开发态 / 其它平台：沿用包目录，行为与旧版一致
    return legacy_data_dir()


def _state_path() -> Path:
    return state_dir() / _STATE_FILE


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except Exception:                                  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load() -> dict[str, Any]:
    """读取状态（进程内缓存）。文件不存在时返回空字典，不抛异常。"""
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            data = _read_json(_state_path())
            if not data:
                # 迁移：旧版把 Cookie 存在包目录 data/，这里只搬标记，不动 Cookie 文件
                data = {}
            _CACHE = data
        return dict(_CACHE)


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入若干字段并落盘。"""
    global _CACHE
    with _LOCK:
        data = load()
        data.update(patch)
        try:
            _write_json(_state_path(), data)
        except OSError:
            pass                                       # 只读环境下静默降级，不影响主流程
        _CACHE = data
        return dict(data)


def reset_cache() -> None:
    """测试用：丢弃进程内缓存，强制下次从磁盘重读。"""
    global _CACHE
    with _LOCK:
        _CACHE = None


# --------------------------------------------------------------------------- #
# 首启标记
# --------------------------------------------------------------------------- #
def is_first_run() -> bool:
    """True = 从未完成过首启引导（需要显示「开始使用」）。"""
    return not bool(load().get("first_run_done"))


def first_run_done() -> bool:
    return bool(load().get("first_run_done"))


def mark_first_run_done() -> dict[str, Any]:
    import time as _t
    patch: dict[str, Any] = {"first_run_done": True}
    if not load().get("first_run_at"):
        patch["first_run_at"] = _t.strftime("%Y-%m-%d %H:%M:%S")
    return save(patch)
