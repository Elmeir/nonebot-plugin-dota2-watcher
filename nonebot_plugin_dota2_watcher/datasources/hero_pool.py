"""Stratz 英雄池数据源：GraphQL 拉取玩家最近比赛英雄与位置占比，带本地缓存。

接口：https://stratz.com/api （GraphQL 端点 /graphql），使用 Bearer Token 鉴权。
Token 配置：config.json 的 d2w_stratz_token，或环境变量 D2W_STRATZ_TOKEN / STRATZ_TOKEN。
英雄头像来源：https://cdn.stratz.com/images/dota2/heroes/{short}_icon.png

每次调用先尝试抓取 API；抓取成功按 steam 账号缓存到 data/hero_pool/，
抓取失败（网络/限流）才回退本地缓存（缓存不设时间限制）。
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import time
from pathlib import Path

from nonebot.log import logger

from .. import assets
from ..config import DATA_DIR, config
from ..utils import cache_with_fallback, get_http_client, load_cache

GRAPHQL_URL = "https://api.stratz.com/graphql"

QUERY = """
query PlayerHeroPool($id: Long!) {
  player(steamAccountId: $id) {
    steamAccount { name avatar }
    matches(request: { take: 25 }) {
      players(steamAccountId: $id) {
        hero {
          displayName
          name
          shortName
        }
        position
      }
    }
  }
}
"""

# 抓取结果缓存：按 steam 账号各存一份到 data/hero_pool/ 目录，缓存不设时间限制
CACHE_DIR = DATA_DIR / "hero_pool"
CACHE_VERSION = 3  # 缓存结构版本（含 position、avatar 字段）；升级后旧缓存自动失效


class HeroPoolError(Exception):
    """英雄池数据抓取失败（供上层转为用户提示）。"""


def _token() -> str:
    """返回 Stratz Token；未配置时抛 HeroPoolError。"""
    token = (
        config.d2w_stratz_token
        or os.environ.get("D2W_STRATZ_TOKEN")
        or os.environ.get("STRATZ_TOKEN")
        or ""
    ).strip()
    if not token:
        raise HeroPoolError(
            "未配置 Stratz Token，请在 config.json 设置 d2w_stratz_token 或环境变量 D2W_STRATZ_TOKEN"
        )
    return token


def _cache_path(steam_id, count=25) -> Path:
    """返回指定 steam 账号 + 比赛数量对应的缓存文件路径。"""
    return CACHE_DIR / f"hero_pool_{steam_id}_{int(count)}.json"


def _load_cache(cache_path: Path, key: tuple[int, int], now: float, max_age: float):
    """读取缓存；命中（结构/账号一致）即返回 (player_name, avatar, matches)，否则 None。

    max_age 仅用于兼容调用方；当前缓存不设时间限制，传 None 表示永不过期。
    """
    data = load_cache(cache_path)
    if data is None:
        return None
    if data.get("cache_version") != CACHE_VERSION:
        return None
    if data.get("steam_id") != key[0] or data.get("count") != key[1]:
        return None
    if max_age is not None and now - data.get("fetched_at", 0) > max_age:
        return None
    return data.get("player_name"), data.get("avatar") or "", data.get("matches") or []


def _save_cache(
    cache_path: Path,
    key: tuple[int, int],
    player_name: str,
    avatar: str,
    matches: list[dict],
    now: float,
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "cache_version": CACHE_VERSION,
                "steam_id": key[0],
                "count": key[1],
                "fetched_at": now,
                "player_name": player_name,
                "avatar": avatar,
                "matches": matches,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


class _RateLimited(Exception):
    """Stratz 限流（HTTP 429/503），触发退避重试。"""


async def _graphql_post(query: str, variables: dict, headers: dict) -> dict:
    """POST Stratz GraphQL，返回解析后的 JSON。

    api.stratz.com 被 Cloudflare 反爬保护，httpx 等常规客户端会被 403 挑战页拦截，
    因此优先用插件共享浏览器（真实浏览器 TLS 指纹可正常通过）请求；浏览器不可用时
    回退到 httpx。
    """
    # 浏览器方案
    try:
        from ..generators.shared_browser import get_browser

        browser = await get_browser()
        context = await browser.new_context()
        try:
            resp = await context.request.post(
                GRAPHQL_URL,
                headers=headers,
                data=json.dumps({"query": query, "variables": variables}),
            )
            status = resp.status
            if status in (429, 503):
                raise _RateLimited(status)
            if status >= 400:
                body = (await resp.text())[:200]
                raise HeroPoolError(f"Stratz API 返回 {status}：{body}")
            return await resp.json()
        finally:
            await context.close()
    except _RateLimited:
        raise
    except Exception:
        pass  # 浏览器方案失败，回退 httpx

    # httpx 兜底
    client = await get_http_client()
    resp = await client.post(
        GRAPHQL_URL,
        headers=headers,
        json={"query": query, "variables": variables},
    )
    if resp.status_code in (429, 503):
        raise _RateLimited(resp.status_code)
    if resp.status_code >= 400:
        body = resp.text[:200]
        raise HeroPoolError(f"Stratz API 返回 {resp.status_code}：{body}")
    return resp.json()


def _parse_payload(payload: dict, steam_id) -> tuple[str, str, list[dict]]:
    """把 GraphQL 响应解析为 (player_name, avatar, matches)。"""
    if payload.get("errors"):
        raise HeroPoolError(f"Stratz GraphQL 返回错误：{payload['errors']}")
    player = (payload.get("data") or {}).get("player") or {}
    account = player.get("steamAccount") or {}
    player_name = account.get("name") or "玩家"
    avatar = account.get("avatar") or ""
    matches = []
    for match in player.get("matches") or []:
        for p in match.get("players") or []:
            hero = p.get("hero")
            if hero:
                matches.append(
                    {
                        "name": hero.get("name"),
                        "display": hero.get("displayName"),
                        "short": hero.get("shortName"),
                        "position": p.get("position"),
                    }
                )
                break
    return player_name, avatar, matches


async def fetch_matches(
    steam_id, count=25, refresh=False, cache_path: Path | None = None, max_age=None
):
    """拉取玩家最近比赛数据：先尝试抓取 API，失败（网络/限流/解析错误）才回退本地缓存。

    返回 (player_name, avatar, matches)：
    - player_name：玩家昵称。
    - avatar：玩家 steam 头像 URL（可能为空串）。
    - matches：每场比赛的英雄信息，含 {'name','display','short','position'}。
    """
    if cache_path is None:
        cache_path = _cache_path(steam_id, count)
    key = (int(steam_id), int(count))
    query = QUERY.replace("take: 25", f"take: {int(count)}") if count != 25 else QUERY
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "User-Agent": "stratz-hero-pool/0.1",
    }

    def _load(path):
        return _load_cache(path, key, time.time(), max_age)

    async def _fetch():
        # 先抓 API；对限流(429/503)做退避重试
        for attempt in range(4):
            try:
                payload = await _graphql_post(query, {"id": int(steam_id)}, headers)
                break
            except _RateLimited:
                if attempt < 3:
                    await asyncio.sleep(15 * (attempt + 1))
                    continue
                raise
        player_name, avatar, matches = _parse_payload(payload, steam_id)

        # 抓取成功，写回缓存
        try:
            _save_cache(cache_path, key, player_name, avatar, matches, time.time())
        except Exception as e:
            logger.warning(f"英雄池缓存写入失败：{e}")
        return player_name, avatar, matches

    # 稳定抓取 API，失败才回退本地缓存；refresh 时则不回退（直接报错）
    return await cache_with_fallback(
        cache_path,
        _fetch,
        max_age=None,
        force_update=True,
        loader=_load,
        fallback=not refresh,
        warn=lambda: logger.warning(f"Stratz API 抓取失败，回退本地缓存 {cache_path.name}"),
    )


def build_stats(matches: list[dict]) -> list[dict]:
    """按英雄聚合出场场次，按场次降序返回 [{'name','display','short','count'}...]。"""
    counter = collections.Counter()
    meta = {}
    for m in matches:
        key = m.get("name") or m.get("display") or "unknown"
        counter[key] += 1
        meta[key] = m  # 任意一条即可取到 display / short
    return [
        {
            "name": key,
            "display": meta[key].get("display") or key,
            "short": meta[key].get("short") or "",
            "count": count,
        }
        for key, count in counter.most_common()
    ]


def pos_distribution(matches: list[dict]) -> list[tuple[str | int, int]]:
    """统计各位置(1-5)的出场占比，返回 [(位置键, 场次)]（按降序）。

    与 STRATZ 内环占比图一致：基于每场比赛的 position 字段统计（不能按英雄聚合）。
    position 为 null 就是 unknown，保留计入（画成半透明白扇区），不做任何推测归类。
    """

    def norm(pos) -> str | int:
        if pos is None:
            return "unknown"
        s = str(pos).strip().lower()
        if s.startswith("position_"):
            s = s[len("position_") :]
        if s.isdigit():
            n = int(s)
            return n if 1 <= n <= 5 else "unknown"
        return "unknown"

    counter = collections.Counter(norm(item.get("position")) for item in matches)
    return counter.most_common()


# ============================================================
# 英雄 / 玩家头像：统一走 assets（目录、下载与内存缓存均由 assets 管理）
# ============================================================
async def load_icon_img(short: str):
    """下载（并缓存）英雄头像为 Pillow RGBA 图；失败返回 None（供 PNG 渲染用）。

    头像落盘到 assets.ICON_IMAGES_DIR，内存缓存也在 assets 内，跨模块共用同一份。
    """
    return await assets.load_hero_icon(short)


async def load_avatar_img(url: str):
    """下载（并缓存）玩家 steam 头像为 Pillow RGBA 图；失败返回 None。

    头像 URL 来自各数据源（Stratz / 小黑盒 / Steam），按 URL 哈希落盘，跨数据源复用。
    """
    return await assets.load_avatar(url)
