"""随手存 · 本机敏感数据加密存储（Cookie 等登录态）。

设计目标：**不引入第三方加密依赖**（APK 内嵌 CPython 不便再加 C 扩展），
改用各操作系统自带的用户级加密能力：

- Windows（PC EXE）：DPAPI（`CryptProtectData` / `CryptUnprotectData`），
  密文绑定当前 Windows 用户账户，复制到别的电脑 / 别的用户下无法解密。
- Android：无 DPAPI。应用私有目录本身已由 Android 沙箱（per-app UID）隔离，
  但备份 / 导出 / root 仍可能读到明文；此处退化为「沙箱隔离 + 明文」并如实标记，
  不谎称已加密。
- 其它平台：同上，密文头标记为 `plain`。

存储格式（首行为魔数，便于识别与降级）：
    MDLENC1:dpapi\n<base64 密文>
    MDLENC1:plain\n<原文>

读取时：任何不认识的格式 / 解密失败，都按「旧版明文文件」处理，保证向后兼容，
绝不让已有用户因为升级而丢失 Cookie。
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

MAGIC = "MDLENC1"

# DPAPI 仅 Windows 可用；在 conda / 精简 Python 下也可能缺 ctypes.windll。
_IS_WINDOWS = sys.platform.startswith("win")


def _encode(kind: str, payload: bytes) -> str:
    return f"{MAGIC}:{kind}\n" + base64.b64encode(payload).decode("ascii")


def _decode(text: str) -> tuple[str, str] | None:
    """返回 (kind, base64体)；不是本模块格式时返回 None。"""
    if not text.startswith(MAGIC + ":"):
        return None
    try:
        head, _, body = text.partition("\n")
        kind = head.split(":", 1)[1].strip()
        return kind, body.strip()
    except Exception:                                  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Windows DPAPI
# --------------------------------------------------------------------------- #
def _dpapi_available() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        import ctypes                                   # noqa: F401
        return bool(ctypes.windll.crypt32)
    except Exception:                                  # noqa: BLE001
        return False


def _dpapi(protect: bool, data: bytes) -> bytes:
    """调用 DPAPI 加/解密；失败抛异常，由调用方决定降级。"""
    import ctypes
    import ctypes.wintypes as wt

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _in(b: bytes) -> _Blob:
        buf = ctypes.create_string_buffer(b, len(b))
        return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _out(bl: _Blob) -> bytes:
        return ctypes.string_at(bl.pbData, bl.cbData)

    src = _in(data)
    dst = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect \
        else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst))
    if not ok:
        raise OSError("DPAPI 调用失败")
    try:
        return _out(dst)
    finally:
        try:
            ctypes.windll.kernel32.LocalFree(dst.pbData)
        except Exception:                              # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 对外：读写单个敏感字符串
# --------------------------------------------------------------------------- #
def encrypt(text: str) -> str:
    """把明文加密成可落盘的单行文本（含魔数头）。"""
    raw = (text or "").encode("utf-8")
    if _dpapi_available():
        try:
            return _encode("dpapi", _dpapi(True, raw))
        except Exception:                              # noqa: BLE001
            pass
    return _encode("plain", raw)


def decrypt(stored: str) -> str:
    """还原密文；旧版纯明文文件原样返回，保证向后兼容。"""
    parsed = _decode(stored or "")
    if parsed is None:
        return (stored or "").strip()
    kind, body = parsed
    try:
        raw = base64.b64decode(body.encode("ascii"))
    except Exception:                                  # noqa: BLE001
        return ""
    if kind == "dpapi":
        try:
            return _dpapi(False, raw).decode("utf-8", "replace").strip()
        except Exception:                              # noqa: BLE001
            return ""
    return raw.decode("utf-8", "replace").strip()


# --------------------------------------------------------------------------- #
# 文件级读写
# --------------------------------------------------------------------------- #
def write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = encrypt(text)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    try:                                               # 收紧其它用户读权限（POSIX）
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_secret(path: Path) -> str:
    try:
        if not path.exists():
            return ""
        return decrypt(path.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        return ""


def backend() -> str:
    """当前生效的加密后端，供界面如实展示（dpapi / plain）。"""
    return "dpapi" if _dpapi_available() else "plain"
