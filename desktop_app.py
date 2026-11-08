"""随手存 · Windows 桌面端 EXE 入口。

打包方式见 build_exe.py / build_exe.bat。双击生成的 EXE 后：
1. 把日志写到 %LOCALAPPDATA%\\SuiShouCun\\app.log（无控制台窗口）；
2. 若本机服务已在运行，直接打开同一个界面，不再启动第二个实例；
3. 否则在本机 127.0.0.1 启动 Flask 服务，用 Edge / Chrome 的 --app 模式
   打开独立桌面窗口。

与 Android APK 是同一个应用：共用 media_dl 全部代码与 media_dl/web 前端，
各自在本机运行，互不连接。差异只在响应式样式与按端裁剪的功能。
"""

from __future__ import annotations

import os
import sys
import tempfile
import webbrowser
from pathlib import Path

PORT = 8765


def _redirect_output() -> None:
    """--noconsole 打包下 stdout/stderr 可能为 None，统一改写到日志文件。"""
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "SuiShouCun"
    try:
        base.mkdir(parents=True, exist_ok=True)
        fp = (base / "app.log").open("a", encoding="utf-8", errors="replace")
        sys.stdout = fp
        sys.stderr = fp
    except OSError:
        class _Null:
            def write(self, *args, **kwargs): return 0
            def flush(self): pass
        sys.stdout = sys.stderr = _Null()   # type: ignore[assignment]


def _open_existing() -> bool:
    from media_dl.server import _port_busy
    if not _port_busy(PORT):
        return False
    try:
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
    except Exception:                                  # noqa: BLE001
        pass
    print(f"端口 {PORT} 上已有一个随手存实例，已直接打开界面。", flush=True)
    return True


def main() -> int:
    _redirect_output()
    if _open_existing():
        return 0
    from media_dl.server import run_server
    print("随手存 PC 端启动（EXE / 本机模式）", flush=True)
    # 供无头验证 / 服务器场景使用：设置 MEDIADL_NO_UI=1 时不弹桌面窗口
    open_ui = os.environ.get("MEDIADL_NO_UI") != "1"
    run_server(
        host="127.0.0.1",
        port=PORT,
        open_browser=open_ui,
        app_mode=open_ui,
        runtime="pc",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
