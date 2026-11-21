"""Stratz GraphQL 传输层：鉴权、限流重试与浏览器兜底。

api.stratz.com 受 Cloudflare 保护，优先用共享浏览器请求，失败回退 httpx。
429/503 按 _RETRY_DELAYS 退避重试；查询语句与解析由各数据源负责。
"""

from __future__ import annotations

import asyncio
import json
import os

from ..config import config
from ..utils import get_http_client

GRAPHQL_URL = "https://api.stratz.com/graphql"

# 限流（HTTP 429/503）退避重试间隔（秒）；共 4 次尝试
_RETRY_DELAYS = (15, 30, 45)


class StratzError(Exception):
    """Stratz 请求失败基类；各数据源的领域异常均继承它。"""


class RateLimited(StratzError):
    """Stratz 限流（HTTP 429/503），触发退避重试。"""


def token(error_cls: type[StratzError] = StratzError) -> str:
    """返回 Stratz Token；未配置时抛出调用方指定的领域异常。"""
    value = (
        config.d2w_stratz_token
        or os.environ.get("D2W_STRATZ_TOKEN")
        or os.environ.get("STRATZ_TOKEN")
        or ""
    ).strip()
    if not value:
        raise error_cls(
            "未配置 Stratz Token，请在 config.json 设置 d2w_stratz_token 或环境变量 D2W_STRATZ_TOKEN"
        )
    return value


def _headers(bearer: str, user_agent: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
    }


async def _post_once(query: str, variables: dict, headers: dict) -> dict:
    """单次 POST：浏览器优先，失败后回退 httpx。"""
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
                raise RateLimited(f"Stratz 限流（{status}）")
            if status >= 400:
                body = (await resp.text())[:200]
                raise StratzError(f"Stratz API 返回 {status}：{body}")
            return await resp.json()
        finally:
            await context.close()
    except RateLimited:
        raise
    except Exception:
        pass  # 浏览器方案不可用，回退 httpx

    client = await get_http_client()
    resp = await client.post(
        GRAPHQL_URL, headers=headers, json={"query": query, "variables": variables}
    )
    if resp.status_code in (429, 503):
        raise RateLimited(f"Stratz 限流（{resp.status_code}）")
    if resp.status_code >= 400:
        raise StratzError(f"Stratz API 返回 {resp.status_code}：{resp.text[:200]}")
    return resp.json()


async def query(gql: str, variables: dict, *, bearer: str, user_agent: str) -> dict:
    """携带鉴权头请求 GraphQL；对限流做退避重试，其余错误直接抛出。

    返回完整响应体（含 data / errors），由调用方解析并决定错误语义。
    """
    headers = _headers(bearer, user_agent)
    for delay in _RETRY_DELAYS:
        try:
            return await _post_once(gql, variables, headers)
        except RateLimited:
            await asyncio.sleep(delay)
    return await _post_once(gql, variables, headers)
