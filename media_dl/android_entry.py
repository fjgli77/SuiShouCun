"""Android APK 内嵌 CPython 的启动入口（与 desktop_app.py 是同一个应用）。

Java 侧通过 JNI 调用本模块的 main()；这里调用的是与 PC 完全相同的
media_dl.server.run_server，只是参数不同：不打开浏览器、runtime=android、
临时目录与下载目录放到应用私有目录（Android 上 /tmp 通常不可写）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    home = Path(os.environ.get("MEDIADL_HOME") or os.getcwd())

    # Android 没有控制台，把 Python 的 stdout/stderr 落到应用私有日志
    try:
        fp = (home / "app.log").open("a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = fp
        sys.stderr = fp
    except OSError:
        pass

    # 无直链站点会临时下载到系统临时目录再转发；Android 上必须换成可写目录
    tmp = home / "tmp"
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        os.environ["TMPDIR"] = str(tmp)
        import tempfile
        tempfile.tempdir = str(tmp)
    except OSError:
        pass

    outdir = home / "downloads"
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        outdir = home

    os.environ["MEDIADL_RUNTIME"] = "android"
    print("随手存 Android 端启动（内嵌 CPython · 本机服务）", flush=True)

    from .server import run_server
    run_server(
        host="127.0.0.1",
        port=8765,
        outdir=outdir,
        open_browser=False,
        app_mode=False,
        runtime="android",
    )


if __name__ == "__main__":
    main()
