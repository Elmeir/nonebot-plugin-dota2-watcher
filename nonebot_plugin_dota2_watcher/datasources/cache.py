"""带版本号的 JSON 缓存读写：按账号隔离，结构升级后旧缓存自动失效。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..utils import load_cache


def read(path: Path, version: int, steam_id) -> dict | None:
    """读取缓存；文件缺失、结构版本不符或账号不匹配时返回 None。"""
    data = load_cache(path)
    if not isinstance(data, dict) or data.get("cache_version") != version:
        return None
    if data.get("steam_id") != int(steam_id):
        return None
    return data


def write(path: Path, version: int, steam_id, **fields) -> None:
    """写入缓存：统一补充版本、账号与抓取时间，其余字段由调用方提供。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cache_version": version,
        "steam_id": int(steam_id),
        "fetched_at": time.time(),
        **fields,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
