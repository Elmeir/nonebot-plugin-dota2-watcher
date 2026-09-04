"""网络请求与通用工具。"""

import asyncio
import base64
import json
import os
import ssl
import sys
import time
import urllib.request
from typing import Any

import httpx

if __package__:
    from .config import config
else:
    from config import config


class DOTA2HTTPError(Exception):
    """DOTA2 相关请求/解析失败时抛出的异常。"""


_client: httpx.AsyncClient | None = None


def prompt_error(response: httpx.Response, url: str) -> None:
    """根据 HTTP 状态码抛出友好的错误信息。"""
    if response.status_code >= 400:
        if response.status_code == 401:
            raise DOTA2HTTPError("未经授权的请求 401。请验证 API 密钥。")
        if response.status_code == 503:
            raise DOTA2HTTPError("服务器繁忙或您超出了限制。请等待 30 秒后重试。")
        raise DOTA2HTTPError(f"无法获取数据：{response.status_code}。URL：{url}")


def network_timeout_error() -> DOTA2HTTPError:
    """返回「连接超时」业务异常（统一文案，含 d2w_timeout 配置）。"""
    return DOTA2HTTPError(
        f"{config.d2w_timeout}秒内无法连接到网站，建议检查网络，或者尝试使用代理服务器"
    )


async def get_json(
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    timeout: float | None = None,
) -> Any:
    """异步 GET 指定 URL 并返回解析后的 JSON。

    统一各数据源的「取共享客户端 + GET + 超时转异常 + 状态码校验」样板：
    - 网络异常统一抛出 network_timeout_error()；
    - HTTP 状态码 >=400 由 prompt_error 抛出友好信息。
    """
    client = await get_http_client()
    try:
        response = await client.get(url, headers=headers, params=params, timeout=timeout)
    except Exception:
        raise network_timeout_error()
    prompt_error(response, url)
    return response.json()


def _proxies_kwargs() -> dict:
    """将配置中的代理转为 httpx 支持的关键字参数。

    httpx >= 0.28 已移除 Client(proxies=...)，这里针对不同版本做兼容：
    - 仅一个 http/https 代理时使用 proxy=...
    - 多个不同代理时使用 mounts=...
    """
    proxies: dict[str, str] = {}
    for key, value in (config.d2w_proxies or {}).items():
        if value:
            proxies[key if "://" in key else f"{key}://"] = value
    if not proxies:
        return {}
    if "http://" in proxies and "https://" in proxies and proxies["http://"] == proxies["https://"]:
        return {"proxy": proxies["https://"]}
    return {"mounts": {k: httpx.AsyncHTTPTransport(proxy=v) for k, v in proxies.items()}}


async def get_http_client() -> httpx.AsyncClient:
    """获取（或创建）全局复用的异步 HTTP 客户端。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=config.d2w_timeout,
            verify=_UNVERIFIED_SSL_CONTEXT,  # 同 download_bytes：不校验证书
            **_proxies_kwargs(),
        )
    return _client


# 部分公开素材 CDN（如 cdn.cloudflare.steamstatic.com）的证书缺少 Authority Key Identifier，
# 在较新的 OpenSSL / Python 上会触发 CERTIFICATE_VERIFY_FAILED。这里为素材下载统一
# 使用不校验证书的上下文，下载内容为公开图片/数据，风险可控。
_UNVERIFIED_SSL_CONTEXT = ssl.create_default_context()
_UNVERIFIED_SSL_CONTEXT.check_hostname = False
_UNVERIFIED_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

_DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def download_bytes(url, timeout=None, headers=None, retries=1):
    """下载 url 内容为字节（不校验 SSL，用于公开素材）。最后一次失败抛出异常。"""
    req = urllib.request.Request(url, headers=headers or _DEFAULT_HEADERS)
    last_exc = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(
                req, timeout=timeout, context=_UNVERIFIED_SSL_CONTEXT
            ) as resp:
                return resp.read()
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(1.0)
    raise last_exc


async def async_download_bytes(url, timeout=None, headers=None, retries=1):
    """异步下载 url 内容为字节（不校验 SSL，用于公开素材）。

    用共享的 httpx.AsyncClient 请求，不阻塞事件循环；沿用与 download_bytes 一致的
    重试与「不校验证书」策略。仅命中冷缓存（真正走网络）时才有收益，磁盘缓存命中时
    不会触发。最后一次失败抛出异常。
    """
    client = await get_http_client()
    last_exc = None
    for attempt in range(retries):
        try:
            resp = await client.get(
                url,
                timeout=timeout,
                headers=headers or _DEFAULT_HEADERS,  # client 已配置不校验证书
            )
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                await asyncio.sleep(1.0)
    raise last_exc


def download_file(url, filepath, timeout=None, headers=None, quiet=False, retries=1):
    """下载 url 内容到本地文件（不校验 SSL，用于公开素材）。成功返回 True，失败返回 False。"""
    try:
        body = download_bytes(url, timeout=timeout, headers=headers, retries=retries)
    except Exception as e:
        if not quiet:
            print(f"警告: 下载 {os.path.basename(filepath)} 失败: {e}", file=sys.stderr)
        return False
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(body)
    return True


def loadjson(filepath, default=None):
    """读取 JSON 文件，成功返回解析结果，失败返回 default（默认空 dict）。

    该工具被 match_report / core_build 等模块复用，作为统一读缓存入口。
    """
    if default is None:
        default = {}
    try:
        with open(filepath, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def dumpjson(data, filepath):
    """将数据以 UTF-8 缩进的 JSON 格式写入文件。"""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def load_cache(filepath):
    """安全读取缓存 JSON：文件缺失或解析失败时返回 None。

    与 loadjson（失败返回默认空 dict）不同，这里保持「不存在/损坏 → None」语义，
    供需要据此触发远端回退的缓存逻辑复用。
    """
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


async def cache_with_fallback(
    filepath,
    fetch,
    max_age,
    *,
    force_update=False,
    loader=None,
    fallback=True,
    warn=None,
):
    """统一的「缓存优先读取 + 拉取失败回退」辅助（异步）。

    流程：
      1) 用 loader(filepath) 读取缓存（缺省 utils.load_cache，失败返回 None）；
      2) 缓存存在且未过期（且非 force_update）→ 直接返回缓存；
      3) 否则 await fetch() 拉取新数据；
      4) fetch 抛异常 → 若已有缓存且 fallback 为 True → 调用 warn() 并返回缓存，否则原样抛出。

    filepath    : 缓存文件路径
    fetch       : () -> data 的异步拉取函数（成功后应自行保存缓存）
    max_age     : 缓存有效期秒数；None 表示「文件存在即为有效」
    force_update: True 时忽略缓存新鲜度强制拉取（拉取失败仍按 fallback 处理）
    loader      : (path) -> data，读取缓存；失败返回 None（缺省 utils.load_cache）
    fallback    : 拉取失败时是否回退已读取的缓存
    warn        : 回退缓存时的零参回调（用于打日志）
    """
    cached = None
    if os.path.exists(filepath):
        cached = (loader or load_cache)(filepath)
    if cached is not None and not force_update:
        try:
            age = time.time() - os.path.getmtime(filepath)
        except OSError:
            age = None
        if age is not None and (max_age is None or age < max_age):
            return cached
    try:
        return await fetch()
    except Exception:
        if fallback and cached is not None:
            if warn:
                warn()
            return cached
        raise


def image_to_data_uri(path):
    """读取本地图片文件，转成 base64 data URI（png）。"""
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{data}"


# ---------------------------------------------------------------
# 进程内 single-flight：相同 key 的并发异步任务共享同一次执行
# ---------------------------------------------------------------
_single_flight_tasks: dict = {}


async def run_single_flight(key, factory):
    """相同 key 的并发调用共享 factory() 的同一次执行（single-flight 去重）。

    - 任务进行中：后续相同 key 的调用等待该任务，不重复执行 factory；
    - 任务已完成交付：下次调用会重新执行 factory（保证可再次刷新数据）；
    - factory 抛出的异常会原样传给所有等待者。
    适用于耗时查询去重（如 /pro 相同账号的并发请求只抓取一次）。
    """
    task = _single_flight_tasks.get(key)
    if task is not None and task.done():
        _single_flight_tasks.pop(key, None)
        task = None
    if task is None:
        task = asyncio.create_task(factory())
        _single_flight_tasks[key] = task
    try:
        # shield：单个等待者被取消时不波及共享任务与其他等待者
        return await asyncio.shield(task)
    finally:
        if task.done() and _single_flight_tasks.get(key) is task:
            _single_flight_tasks.pop(key, None)
