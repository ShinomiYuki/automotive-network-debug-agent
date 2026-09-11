"""
文件用途：
- 提供插件运行时缓存目录的跨平台解析。
- 允许部署者通过 ANDA_CACHE_DIR 显式放置缓存，避免把个人路径写进工程。
"""

import os
from pathlib import Path


def default_cache_root() -> Path:
    """返回插件缓存根目录；只创建插件自己的子目录。"""
    configured = os.environ.get("ANDA_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData/Local"
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME", "").strip()
        base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return (base / "AutomotiveNetworkDebugAgent" / "cache").resolve()
