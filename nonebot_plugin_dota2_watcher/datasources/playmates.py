"""Stratz 开黑记录：玩家最常一起开黑的队友（同队聚合）。

查询 stratz.page.player.peers，服务端完成全量聚合，一次请求取回。
与 pro_peers 的区别：保留全部玩家，且只看同队（WITH）。
按账号缓存到 data/playmates/；请求失败时回退旧缓存。
"""

from __future__ import annotations

from pathlib import Path

from nonebot.log import logger

from ..config import DATA_DIR
from ..utils import cache_with_fallback
from . import cache, stratz
from .stratz_peers import PEERS_VARS_BASE, parse_peer_entry

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


class PlaymatesError(stratz.StratzError):
    """开黑记录抓取失败（供上层转为用户提示）。"""


def _cache_path(steam_id) -> Path:
    """返回指定 steam 账号对应的缓存文件路径。"""
    return CACHE_DIR / f"playmates_{int(steam_id)}.json"


def _load_cache(cache_path: Path, steam_id):
    """读取缓存；命中（结构/账号一致）即返回 (player_name, player_avatar, rows)，否则 None。"""
    data = cache.read(cache_path, CACHE_VERSION, steam_id)
    if data is None:
        return None
    return (
        data.get("player_name") or "玩家",
        data.get("player_avatar") or "",
        data.get("rows") or [],
    )


def _save_cache(
    cache_path: Path, steam_id, player_name: str, player_avatar: str, rows: list[dict]
) -> None:
    cache.write(
        cache_path,
        CACHE_VERSION,
        steam_id,
        player_name=player_name,
        player_avatar=player_avatar,
        rows=rows,
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
        parsed = parse_peer_entry(entry)
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
    token = stratz.token(PlaymatesError)
    variables = {
        "steamId": steam_id,
        "take": PEERS_VARS_BASE["take"],
        "withRequest": {**PEERS_VARS_BASE},
    }

    async def _fetch():
        payload = await stratz.query(
            QUERY,
            variables,
            bearer=token,
            user_agent="stratz-playmates/0.1",
        )
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
