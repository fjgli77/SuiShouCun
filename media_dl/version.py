"""版本号唯一真源（single source of truth）。

全项目任何地方需要版本号，都应从这里取，不要在别处硬编码：
- 服务端 / API / 界面顶栏：``__version_short__``（如 "6.1"）
- 完整语义化版本（打包、日志）：``__version__``（如 "6.1.0"）
- Android ``versionCode``：``__version_code__``（每次发布递增的整数）

``android/build_apk.py`` 会在构建时解析本文件，把版本号注入 AndroidManifest，
因此**改版本只需改这里一处**。
"""

from __future__ import annotations

__version__ = "6.1.0"
__version_short__ = "6.1"
__version_code__ = 7

__all__ = ["__version__", "__version_short__", "__version_code__"]
