"""Stratz 开黑记录数据源：查询玩家最常一起开黑的队友（共同游玩场次）。

接口：https://api.stratz.com/graphql（GraphQL 端点），使用 Bearer Token 鉴权。
Token 配置：config.json 的 d2w_stratz_token，或环境变量 D2W_STRATZ_TOKEN / STRATZ_TOKEN。

对应 stratz.com/players/{id}/peers 页面「同队（WITH）」一组：
查询走 stratz.page.player.peers，服务端已完成全量历史聚合，
每条含 matchCount（共同场次）/ winCount（共同胜场）/ lastMatchDateTime / steamAccount。
一次 POST 即取回完整队友列表（take: 10000，与 stratz.com 一致），无需本地翻页。

与 pro_peers 的区别：/pro 只保留 STRATZ 标记的职业选手，且合并队友+对手两组；
本模块面向「开黑」场景，保留全部普通玩家（不按职业身份过滤），且只看同队（WITH）一组。

每次调用先尝试抓取 API；抓取成功按 steam 账号缓存到 data/playmates/，
抓取失败（网络/限流）才回退本地缓存（缓存不设时间限制）。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from nonebot.log import logger

from ..config import DATA_DIR
from ..utils import cache_with_fallback, load_cache
from .hero_pool import HeroPoolError, _graphql_post, _RateLimited, _token
from .pro_peers import _PEERS_VARS_BASE, _parse_peer_entry

# 与 stratz.com peers 页面一致的聚合请求：只取同队（WITH）一组全量列表，
# 额外请求 steamAccount.avatar（渲染每行头像用）。
QUERY = """
query GetPlaymates($steamId: Long!, $withRequest: PlayerTeammatesGroupByRequestType!, $take: Int) {
  player(steamAccountId: $steamId) {
    steamAccount { name avatar }
  }
  stratz {
    page {
      player(steamAccountId: $steamId) {
        peers: peers(request: $withRequest, take: $take) {
          matchCount
          winCount
          lastMatchDateTime
          steamAccount { id name avatar }
        }
      }
    }
  }
}
"""

# 抓取结果缓存：按 steam 账号各存一份到 data/playmates/ 目录，缓存不设时间限制
CACHE_DIR = DATA_DIR / "playmates"
CACHE_VERSION = 1  # 缓存结构版本；升级后旧缓存自动失效
OUTPUT_LIMIT = 20  # 图片最多展示的开黑队友条数


class PlaymatesError(Exception):
    """开黑记录抓取失败（供上层转为用户提示）。"""


def _cache_path(steam_id) -> Path:
    """返回指定 steam 账号对应的缓存文件路径。"""
    return CACHE_DIR / f"playmates_{int(steam_id)}.json"


def _load_cache(cache_path: Path, steam_id):
    """读取缓存；命中（结构/账号一致）即返回 (player_name, player_avatar, rows)，否则 None。"""
    data = load_cache(cache_path)
    if data is None or data.get("cache_version") != CACHE_VERSION:
        return None
    if data.get("steam_id") != int(steam_id):
        return None
    return (
        data.get("player_name") or "玩家",
        data.get("player_avatar") or "",
        data.get("rows") or [],
    )


def _save_cache(
    cache_path: Path, steam_id, player_name: str, player_avatar: str, rows: list[dict]
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "cache_version": CACHE_VERSION,
                "steam_id": int(steam_id),
                "fetched_at": time.time(),
                "player_name": player_name,
                "player_avatar": player_avatar,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def parse_playmates(payload: dict, steam_id: int) -> list[dict]:
    """把 GraphQL 响应解析为开黑队友列表（按共同场次降序）。

    返回每条 {'steam_id', 'name', 'avatar', 'games', 'wins', 'last'}：
    games 为共同游玩场次，wins 为其中胜场（同队一组的 winCount 即查询玩家视角胜场）。
    不过滤职业身份——普通好友同样是开黑对象。
    """
    page_player = (((payload.get("data") or {}).get("stratz") or {}).get("page") or {}).get(
        "player"
    ) or {}

    rows: list[dict] = []
    for entry in page_player.get("peers") or []:
        parsed = _parse_peer_entry(entry)
        if parsed is None:
            continue
        pid, count, win, last = parsed
        if pid == steam_id:
            continue  # 服务端偶发把本人计入，跳过
        account = entry.get("steamAccount") or {}
        pro = account.get("proSteamAccount") or {}
        rows.append(
            {
                "steam_id": pid,
                # 职业选手优先显示职业名（STRATZ 已给出），否则用游戏内昵称
                "name": pro.get("name") or account.get("name") or str(pid),
                "avatar": account.get("avatar") or "",
                "games": count,
                "wins": win,
                "last": last,
            }
        )
    rows.sort(key=lambda r: (-r["games"], r["name"]))
    return rows


async def fetch_playmates(steam_id):
    """单次 GraphQL 查询玩家最常一起开黑的队友。

    每次调用先尝试抓取 API，抓取失败（网络/限流）才回退本地缓存（不设时间限制）。
    返回 (player_name, player_avatar, rows)：rows 按共同场次降序，见 parse_playmates。
    """
    steam_id = int(steam_id)
    cache_path = _cache_path(steam_id)
    try:
        token = _token()
    except HeroPoolError as e:
        raise PlaymatesError(str(e)) from e
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "stratz-playmates/0.1",
    }
    variables = {
        "steamId": steam_id,
        "take": _PEERS_VARS_BASE["take"],
        "withRequest": {**_PEERS_VARS_BASE},
    }

    async def _fetch():
        # 单次 POST 聚合查询；对限流(429/503)做退避重试
        for attempt in range(4):
            try:
                payload = await _graphql_post(QUERY, variables, headers)
                break
            except _RateLimited:
                if attempt < 3:
                    await asyncio.sleep(15 * (attempt + 1))
                    continue
                raise
        if payload.get("errors"):
            raise PlaymatesError(f"Stratz GraphQL 返回错误：{payload['errors']}")
        account = (((payload.get("data") or {}).get("player") or {}).get("steamAccount")) or {}
        player_name = account.get("name") or "玩家"
        player_avatar = account.get("avatar") or ""
        rows = parse_playmates(payload, steam_id)
        try:
            _save_cache(cache_path, steam_id, player_name, player_avatar, rows)
        except Exception as e:
            logger.warning(f"开黑记录缓存写入失败：{e}")
        return player_name, player_avatar, rows

    return await cache_with_fallback(
        cache_path,
        _fetch,
        max_age=None,
        force_update=True,
        loader=lambda p: _load_cache(p, steam_id),
        warn=lambda: logger.warning(f"Stratz 抓取失败，回退本地缓存 {cache_path.name}"),
    )
