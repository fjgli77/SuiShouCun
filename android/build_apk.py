"""构建 Android APK：python android/build_apk.py

v6：APK 内嵌 CPython 3.13，在手机本机跑与 PC EXE 完全相同的 media_dl 服务。
不使用 Gradle，直接调用本机 Android SDK 的 aapt2 / javac / d8 / zipalign /
apksigner 与 NDK clang；Python 运行时来自 Maven Central 的 Chaquopy target 包。

产物：dist/随手存-手机版.apk（同时保留 ASCII 名 dist/SuiShouCun-Mobile.apk）
环境：ANDROID_HOME 指向 Android SDK（本机 Z:\\SDK）、JDK 8+、NDK、curl。

首次构建会从 Maven Central 下载 ~9MB 的 Python 运行时并缓存到 android/vendor/，
之后离线也能构建。所有纯 Python 依赖从当前开发环境的 site-packages 复制。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST = ROOT / "dist"
BUILD = HERE / "build"
VENDOR = HERE / "vendor"
CN_NAME = "随手存-手机版"
ASCII_NAME = "SuiShouCun-Mobile"


def read_version() -> tuple[str, int]:
    """从 media_dl/version.py 读版本号（唯一真源），避免清单里再硬编码一份。"""
    src = (ROOT / "media_dl" / "version.py").read_text(encoding="utf-8")
    name = re.search(r'^__version__\s*=\s*"([^"]+)"', src, re.MULTILINE)
    code = re.search(r"^__version_code__\s*=\s*(\d+)", src, re.MULTILINE)
    if not name or not code:
        raise SystemExit("media_dl/version.py 缺少 __version__ / __version_code__")
    return name.group(1), int(code.group(1))


def render_manifest() -> Path:
    """把版本号注入 AndroidManifest.xml，输出到 build/ 供 aapt2 使用。"""
    name, code = read_version()
    src = (HERE / "AndroidManifest.xml").read_text(encoding="utf-8")
    src = re.sub(r'android:versionCode="[^"]*"', f'android:versionCode="{code}"', src, count=1)
    src = re.sub(r'android:versionName="[^"]*"', f'android:versionName="{name}"', src, count=1)
    BUILD.mkdir(parents=True, exist_ok=True)
    out = BUILD / "AndroidManifest.xml"
    out.write_text(src, encoding="utf-8")
    print(f"    版本号注入：versionName={name} versionCode={code}", flush=True)
    return out

BT_VERSION = "36.0.0"
PLATFORM = "android-34"
MIN_SDK = 24
TARGET_SDK = 34
ABI = "arm64-v8a"
NDK_VERSION = "28.2.13676358"
PY_TARGET = "3.13.0-1"
MAVEN_BASE = f"https://repo1.maven.org/maven2/com/chaquo/python/target/{PY_TARGET}"
RUNTIME_ZIP = f"target-{PY_TARGET}-{ABI}.zip"
STDLIB_ZIP = f"target-{PY_TARGET}-stdlib.zip"

# 需要随 APK 带上的纯 Python 依赖（与 requirements.txt 对应，含传递依赖）
SITE_PACKAGES = [
    "flask", "werkzeug", "jinja2", "markupsafe", "click", "itsdangerous",
    "blinker", "requests", "urllib3", "idna", "certifi", "charset_normalizer",
    "yt_dlp",
]
SKIP_SUFFIXES = {".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".a", ".lib"}
SKIP_DIRS = {"__pycache__", ".git", "tests", "test"}


# --------------------------------------------------------------------------- #
def run(cmd: list[str], label: str, check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n>>> {label}\n    {cmd[0]} ...", flush=True)
    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        print((result.stdout or "")[-3000:], flush=True)
        print((result.stderr or "")[-3000:], flush=True)
        raise SystemExit(f"{label} 失败（exit {result.returncode}）")
    tail = (result.stderr or result.stdout or "").strip()
    if tail:
        print("    " + tail.splitlines()[-1][:200], flush=True)
    return result


def png_chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return struct.pack(">I", len(data)) + body + \
        struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def make_icon(path: Path, size: int = 192) -> None:
    """纯标准库生成应用图标 PNG（渐变底 + 中心浅色方块）。"""
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")            # PNG filter type 0
        for x in range(size):
            t = (x + y) / (2.0 * size)
            r, g, b = int(25 + 25 * t), int(115 + 70 * t), int(230 - 25 * t)
            if size * .24 < x < size * .76 and size * .24 < y < size * .76:
                r, g, b = 245, 250, 255
                if size * .37 < x < size * .63 and size * .43 < y < size * .57:
                    r, g, b = 37, 99, 235
            row += bytes((r, g, b, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)
    data = (b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(raw, 6))
            + png_chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def find_sdk() -> Path:
    candidates = [
        os.environ.get("ANDROID_HOME", ""),
        os.environ.get("ANDROID_SDK_ROOT", ""),
        r"Z:\SDK",
        str(Path.home() / "AppData" / "Local" / "Android" / "Sdk"),
    ]
    for value in candidates:
        if value and Path(value).is_dir() and (Path(value) / "platforms").is_dir():
            return Path(value)
    raise SystemExit("找不到 Android SDK，请设置 ANDROID_HOME")


def find_clang(sdk: Path) -> Path:
    llvm = sdk / "ndk" / NDK_VERSION / "toolchains" / "llvm" / "prebuilt" / "windows-x86_64" / "bin"
    for name in ("clang.exe", "clang"):
        p = llvm / name
        if p.exists():
            return p
    # macOS / Linux 的 NDK 目录名不同，尝试通用查找
    for base in (sdk / "ndk").glob("*/toolchains/llvm/prebuilt/*/bin"):
        for name in ("clang", "clang.exe"):
            p = base / name
            if p.exists():
                return p
    raise SystemExit(f"找不到 NDK clang（期望 {llvm}）")


def fetch(url: str, dest: Path) -> None:
    """下载并缓存；已存在则直接复用，便于离线重复构建。"""
    if dest.exists() and dest.stat().st_size > 1000:
        print(f"    复用缓存 {dest.name}（{dest.stat().st_size / 1024 / 1024:.1f} MB）", flush=True)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    print(f"    下载 {url}", flush=True)
    result = subprocess.run(["curl", "-fL", "--retry", "3", "--max-time", "300",
                             "-o", str(tmp), url], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0 or not tmp.exists():
        raise SystemExit(f"下载失败：{url}\n{result.stderr[-1000:]}")
    tmp.replace(dest)


def prepare_runtime() -> Path:
    """下载并解开 Chaquopy 的 Android CPython 运行时。"""
    VENDOR.mkdir(parents=True, exist_ok=True)
    runtime_zip = VENDOR / RUNTIME_ZIP
    stdlib_zip = VENDOR / STDLIB_ZIP
    fetch(f"{MAVEN_BASE}/{RUNTIME_ZIP}", runtime_zip)
    fetch(f"{MAVEN_BASE}/{STDLIB_ZIP}", stdlib_zip)

    out = BUILD / "runtime"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    with zipfile.ZipFile(runtime_zip, "r") as z:
        z.extractall(out)
    for needed in (out / "jniLibs" / ABI / "libpython3.13.so",
                   out / "include" / "python3.13" / "Python.h",
                   out / "lib-dynload" / ABI):
        if not needed.exists():
            raise SystemExit(f"运行时缺文件：{needed}")
    return out


def build_jni(sdk: Path, runtime: Path) -> Path:
    """用 NDK clang 编译 JNI 启动器，链接 libpython3.13.so。"""
    clang = find_clang(sdk)
    out = BUILD / "jniLibs" / ABI
    out.mkdir(parents=True, exist_ok=True)
    lib = out / "libpystart.so"
    cmd = [
        str(clang), f"--target=aarch64-linux-android{MIN_SDK}",
        "-fPIC", "-shared", "-O2", "-Wall",
        "-I", str(runtime / "include" / "python3.13"),
        "-o", str(lib),
        str(HERE / "jni" / "pystart.c"),
        "-L", str(runtime / "jniLibs" / ABI),
        "-lpython3.13", "-llog",
    ]
    run(cmd, "NDK 编译 JNI 启动器")
    if not lib.exists() or lib.stat().st_size < 1000:
        raise SystemExit("libpystart.so 未生成")
    return lib


def site_packages_dir() -> Path:
    import sysconfig
    return Path(sysconfig.get_paths()["purelib"])


def copy_tree(src: Path, dst: Path) -> None:
    """复制纯 Python 包：排除原生扩展、缓存、测试与打包元数据。"""
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".dist-info")]
        rel = Path(root).relative_to(src)
        target = dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            if Path(name).suffix.lower() in SKIP_SUFFIXES:
                continue
            shutil.copy2(Path(root) / name, target / name)


def copy_metadata(purelib: Path, pkg: str) -> list[Path]:
    """收集某依赖需要复制的源：包目录 + 其同级 *.dist-info / *.egg-info / *.egg-link。

    关键：importlib.metadata（如 werkzeug 的 version() 调用）依赖 sys.path 下的
    *.dist-info 元数据。原实现只对包目录调用 copy_tree，把它们彻底漏掉了，导致
    `PackageNotFoundError: No package metadata was found for werkzeug`，服务起不来。"""
    sources: list[Path] = []
    pkg_dir = purelib / pkg
    if pkg_dir.is_dir():
        sources.append(pkg_dir)
    for item in purelib.iterdir():
        name = item.name
        low = name.lower()
        if item.is_dir() and (low.endswith(".dist-info") or low.endswith(".egg-info")):
            suffix = ".dist-info" if low.endswith(".dist-info") else ".egg-info"
            base = name[: len(name) - len(suffix)]
            proj = base.rsplit("-", 1)[0] if "-" in base else base
            if proj.lower() == pkg.lower():
                sources.append(item)
        elif low == pkg.lower() + ".egg-link":
            sources.append(item)
    if not sources:
        raise SystemExit(f"在 site-packages 中找不到 {pkg} 及其安装元数据")
    return sources


def prepare_assets(runtime: Path) -> Path:
    """组装 assets/py：标准库 zip + 项目源码 + 纯 Python 依赖 + 版本标记。"""
    assets = BUILD / "assets"
    if assets.exists():
        shutil.rmtree(assets, ignore_errors=True)
    py = assets / "py"
    (py / "app").mkdir(parents=True)
    (py / "site").mkdir(parents=True)

    shutil.copy2(VENDOR / STDLIB_ZIP, py / "stdlib.zip")

    # 与 PC EXE 完全相同的 media_dl 源码与前端
    src_pkg = ROOT / "media_dl"
    dst_pkg = py / "app" / "media_dl"
    copy_tree(src_pkg, dst_pkg)
    if not (dst_pkg / "__init__.py").exists() or not (dst_pkg / "web" / "index.html").exists():
        raise SystemExit("media_dl 源码 / 前端复制不完整")

    # 纯 Python 依赖（从开发环境的 site-packages 取，剔除 Windows 原生扩展）
    # 注意：必须连同同级 *.dist-info 一起复制，否则 importlib.metadata 取不到版本
    purelib = site_packages_dir()
    for pkg in SITE_PACKAGES:
        for src in copy_metadata(purelib, pkg):
            copy_tree(src, py / "site" / src.name)
    for needed in (py / "site" / "flask" / "__init__.py",
                   py / "site" / "yt_dlp" / "__init__.py",
                   py / "site" / "certifi" / "cacert.pem"):
        if not needed.exists():
            raise SystemExit(f"依赖复制不完整：{needed}")

    # 标准库 C 扩展（math/_socket/_ssl/_hashlib…）随 assets 打包，运行时由
    # MainActivity.prepareStdlib 解压到 lib/python3.13/lib-dynload。不依赖系统把
    # APK lib/<abi> 的 .so 提取到 nativeLibraryDir（雷电模拟器等环境不保证提取全部）。
    native_dir = py / "native" / ABI
    native_dir.mkdir(parents=True, exist_ok=True)
    dyn_src = runtime / "lib-dynload" / ABI
    if not dyn_src.is_dir():
        raise SystemExit(f"运行时缺少标准库扩展目录：{dyn_src}")
    for so in dyn_src.iterdir():
        if so.is_file() and so.suffix == ".so":
            shutil.copy2(so, native_dir / so.name)
    if not any(p.is_file() and p.suffix == ".so" for p in native_dir.iterdir()):
        raise SystemExit("标准库扩展复制不完整（缺少 math 等 C 扩展）")

    # 版本标记：内容变了就触发 Java 侧重新解压 assets
    digest = hashlib.sha256()
    for path in sorted(dst_pkg.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(dst_pkg)).encode("utf-8"))
            digest.update(str(path.stat().st_size).encode("ascii"))
    for path in sorted((py / "site").rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(py / "site")).encode("utf-8"))
            digest.update(str(path.stat().st_size).encode("ascii"))
    for path in sorted(native_dir.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(native_dir)).encode("utf-8"))
            digest.update(str(path.stat().st_size).encode("ascii"))
    (py / "VERSION").write_text(f"6.0-{PY_TARGET}-" + digest.hexdigest()[:16] + "\n",
                                encoding="utf-8")
    count = sum(1 for _ in assets.rglob("*") if _.is_file())
    print(f"\n>>> assets 就绪：{count} 个文件", flush=True)
    return assets


def collect_native_libs(runtime: Path) -> list[Path]:
    """APK lib/<abi>/ 只放 Java 侧需要直接 loadLibrary 的运行时库：CPython 解释器、
    JNI 启动器，以及 OpenSSL/SQLite 的 _python 与 _chaquopy 变体。

    标准库 C 扩展（math/_socket/_ssl…）不再放进 APK lib/，改由 prepare_assets
    打包进 assets/py/native/<ABI>/，运行时解压到 lib/python3.13/lib-dynload。
    原因：雷电模拟器等环境不会把 APK lib/<abi> 的全部 .so 提取到 nativeLibraryDir，
    依赖 nativeLibraryDir 会导致 import math 等报 ModuleNotFoundError。"""
    libs: list[Path] = []
    for folder in (runtime / "jniLibs" / ABI, BUILD / "jniLibs" / ABI):
        if folder.is_dir():
            libs.extend(p for p in folder.iterdir() if p.is_file() and p.suffix == ".so")
    required = ("libpython3.13.so", "libpystart.so")
    missing = [n for n in required if not any(p.name == n for p in libs)]
    if missing:
        raise SystemExit(f"APK lib/ 缺少必需的 JNI 运行时库：{missing}")
    return libs


def main() -> int:
    sdk = find_sdk()
    bt = sdk / "build-tools" / BT_VERSION
    android_jar = sdk / "platforms" / PLATFORM / "android.jar"
    aapt2 = bt / ("aapt2.exe" if os.name == "nt" else "aapt2")
    zipalign = bt / ("zipalign.exe" if os.name == "nt" else "zipalign")
    d8_jar = bt / "lib" / "d8.jar"
    apksigner_jar = bt / "lib" / "apksigner.jar"
    for path in (android_jar, aapt2, zipalign, d8_jar, apksigner_jar):
        if not path.exists():
            raise SystemExit(f"缺少 SDK 组件：{path}")
    java = shutil.which("java") or "java"
    javac = shutil.which("javac") or "javac"
    keytool = shutil.which("keytool") or "keytool"

    if BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    DIST.mkdir(exist_ok=True)

    runtime = prepare_runtime()
    build_jni(sdk, runtime)
    assets = prepare_assets(runtime)
    native_libs = collect_native_libs(runtime)
    print(f"    原生库：{len(native_libs)} 个 .so（{ABI}）", flush=True)

    # 1. 资源
    make_icon(HERE / "res" / "mipmap-hdpi" / "ic_launcher.png")
    compiled = BUILD / "compiled.flata"
    run([str(aapt2), "compile", "--dir", str(HERE / "res"), "-o", str(compiled)],
        "aapt2 编译资源")

    # 2. 基础包（含 assets）
    base_apk = BUILD / "base.apk"
    manifest = render_manifest()
    run([str(aapt2), "link", "-o", str(base_apk), "-I", str(android_jar),
         "--manifest", str(manifest),
         "-R", str(compiled), "--auto-add-overlay",
         "-A", str(assets),
         "--java", str(BUILD / "gen"),
         "--min-sdk-version", str(MIN_SDK),
         "--target-sdk-version", str(TARGET_SDK)],
        "aapt2 链接 APK（含 Python assets）")

    # 3. Java -> class -> dex
    classes = BUILD / "classes"
    classes.mkdir(parents=True)
    java_src = HERE / "java" / "com" / "suishoucun" / "app"
    java_files = [str(java_src / "MainActivity.java"), str(java_src / "PyService.java")]
    run([javac, "--release", "8", "-encoding", "UTF-8", "-Xlint:-options",
         "-cp", str(android_jar), "-d", str(classes)] + java_files,
        "javac 编译 Java 源码")
    class_files = [str(p) for p in classes.rglob("*.class")]
    if not class_files:
        raise SystemExit("javac 未产出 class 文件")
    dex_dir = BUILD / "dex"
    dex_dir.mkdir(exist_ok=True)
    run([java, "-cp", str(d8_jar), "com.android.tools.r8.D8",
         "--lib", str(android_jar), "--min-api", str(MIN_SDK),
         "--output", str(dex_dir)] + class_files,
        "d8 生成 classes.dex")
    dex_file = dex_dir / "classes.dex"
    if not dex_file.exists():
        raise SystemExit("d8 未产出 classes.dex")

    # 4. 组装：base + classes.dex + lib/<abi>/*.so（STORED，供系统解压后 dlopen）
    with_dex = BUILD / "with-dex.apk"
    with zipfile.ZipFile(base_apk, "r") as src, \
            zipfile.ZipFile(with_dex, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.write(dex_file, "classes.dex")
        for lib in native_libs:
            info = zipfile.ZipInfo(f"lib/{ABI}/{lib.name}")
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            dst.writestr(info, lib.read_bytes())
    print(f"\n>>> 已注入 classes.dex 与 {len(native_libs)} 个 .so", flush=True)

    # 5. 对齐（16KB 页对齐兼容 Android 15+；旧 build-tools 回退 4 字节）
    aligned = BUILD / "aligned.apk"
    result = run([str(zipalign), "-f", "-P", "16", "4", str(with_dex), str(aligned)],
                 "zipalign 16KB 页对齐", check=False)
    if result.returncode != 0:
        run([str(zipalign), "-f", "4", str(with_dex), str(aligned)], "zipalign 对齐")

    # 6. 签名
    keystore = HERE / "debug.keystore"
    if not keystore.exists():
        run([keytool, "-genkeypair", "-keystore", str(keystore),
             "-storepass", "android", "-keypass", "android",
             "-alias", "androiddebugkey", "-storetype", "PKCS12",
             "-dname", "CN=MediaDL,OU=Dev,O=MediaDL,L=Beijing,ST=Beijing,C=CN",
             "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000"],
            "生成调试签名证书")
    ascii_out = DIST / f"{ASCII_NAME}.apk"
    cn_out = DIST / f"{CN_NAME}.apk"
    run([java, "-jar", str(apksigner_jar), "sign",
         "--ks", str(keystore), "--ks-pass", "pass:android",
         "--key-pass", "pass:android", "--ks-key-alias", "androiddebugkey",
         "--v1-signing-enabled", "true", "--v2-signing-enabled", "true",
         "--out", str(ascii_out), str(aligned)],
        "apksigner 签名")
    run([java, "-jar", str(apksigner_jar), "verify", "--verbose", str(ascii_out)],
        "apksigner 校验")
    shutil.copy2(ascii_out, cn_out)

    print("\n" + "=" * 58, flush=True)
    print(f"  APK 构建完成：{cn_out}", flush=True)
    print(f"  大小：{cn_out.stat().st_size / 1024 / 1024:.2f} MB", flush=True)
    print(f"  架构：{ABI} · 内嵌 CPython 3.13 · 本机服务 127.0.0.1:8765", flush=True)
    print(f"  安装：adb install -r \"{cn_out}\"", flush=True)
    print("=" * 58, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
