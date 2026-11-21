"""DOTA2 官方新闻监听：按 gid 去重后广播，并维护可持久化的已见基线。"""

import json
from pathlib import Path

from nonebot.log import logger

from ..config import DATA_DIR
from ..datasources.request_match import request_news
from ..utils import load_cache
from . import store
from .broadcast import broadcast_text

# 已见 gid 持久化：重启后按 gid 补播；首次运行只建基线，不播历史新闻。
_STATE_FILE: Path = DATA_DIR / "news_state.json"
_MAX_SEEN = 50  # 最多保留的 gid 数，防止状态文件无限增长


def sync_baseline(key: str) -> None:
    """退订后删除新闻基线；重新订阅时按首次运行重建，不补播停订期间的新闻。"""
    if key != "news" or store.any_group_subscribed("subscribe_news"):
        return
    try:
        _STATE_FILE.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"新闻基线删除失败：{e}")


def _load_state() -> dict:
    """读取去重状态 {'seen_gids': [...]}；文件缺失/损坏返回空表（视为首次运行）。"""
    data = load_cache(_STATE_FILE)
    return data if isinstance(data, dict) else {}


def _save_state(seen: list[str]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(
        json.dumps({"version": 1, "seen_gids": seen[-_MAX_SEEN:]}, ensure_ascii=False),
        encoding="utf-8",
    )


async def poll_news() -> None:
    """拉取最新 5 条新闻，把未播报过的按旧→新合并广播。

    无群订阅时不发请求（基线已在退订时删除，重新订阅按首次运行重建）。
    """
    if not store.any_group_subscribed("subscribe_news"):
        return
    try:
        news = await request_news()
    except Exception as e:
        # 网络故障是轮询的常态，按 warning 记录单行即可，避免每分钟刷完整堆栈
        logger.warning(f"获取 DOTA2 新闻失败：{e}")
        return
    events = [e for e in ((news or {}).get("events") or []) if e.get("gid") and e.get("event_name")]
    if not events:
        return

    state = _load_state()
    first_run = not state
    seen: list[str] = [str(g) for g in (state.get("seen_gids") or [])]
    seen_set = set(seen)

    # 未播报过的新闻（接口按时间倒序，reversed 后为旧→新）
    fresh = [e for e in reversed(events) if str(e["gid"]) not in seen_set]
    # 无论是否播报都先更新并持久化已见列表（播报失败不重发，避免刷屏）
    for e in events:
        gid = str(e["gid"])
        if gid not in seen_set:
            seen.append(gid)
            seen_set.add(gid)
    try:
        _save_state(seen)
    except Exception as e:
        logger.warning(f"新闻状态写入失败：{e}")

    if first_run or not fresh:
        return
    lines = [f"[news] {e['event_name']} www.dota2.com/newsentry/{e['gid']}" for e in fresh]
    await broadcast_text("\n".join(lines), "subscribe_news")
