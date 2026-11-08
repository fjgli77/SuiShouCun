"""构建 Windows 桌面端 EXE：python build_exe.py

产物：dist/随手存-电脑端.exe（同时保留 ASCII 名的 dist/SuiShouCun-PC.exe）
需要：pip install pyinstaller（构建机专用，运行 EXE 的电脑不需要 Python）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "exe"
ASCII_NAME = "SuiShouCun-PC"
CN_NAME = "随手存-电脑端"


def main() -> int:
    Dist = DIST
    Dist.mkdir(exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    sep = os.pathsep
    web = ROOT / "media_dl" / "web"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole",
        "--name", ASCII_NAME,
        "--distpath", str(Dist),
        "--workpath", str(BUILD / "work"),
        "--specpath", str(BUILD),
        "--add-data", f"{web}{sep}media_dl/web",
        "--collect-all", "yt_dlp",
        "--hidden-import", "yt_dlp",
        "--hidden-import", "media_dl.server",
        "--hidden-import", "media_dl.delivery",
        "--hidden-import", "media_dl.nocookie",
        "--hidden-import", "media_dl.qr",
        str(ROOT / "desktop_app.py"),
    ]
    print("构建命令：\n  " + " ".join(cmd) + "\n", flush=True)
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("PyInstaller 构建失败", flush=True)
        return result.returncode
    src = Dist / f"{ASCII_NAME}.exe"
    if not src.exists():
        print(f"未找到产物 {src}", flush=True)
        return 1
    dst = Dist / f"{CN_NAME}.exe"
    shutil.copy2(src, dst)
    print(f"\n完成：{dst}  ({dst.stat().st_size / 1024 / 1024:.1f} MB)", flush=True)
    print(f"日志位置：%LOCALAPPDATA%\\SuiShouCun\\app.log", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
