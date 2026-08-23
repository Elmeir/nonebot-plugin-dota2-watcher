"""玩家订阅数据的内存存储与 JSON 持久化。

数据结构：
    {group_id(str): {
        "players": [Player, ...],
        "subscribe_news": bool,  # 默认 True
        "subscribe_ti": bool,    # 默认 True
    }}

所有读写通过本模块提供的接口完成，内部使用锁保证线程安全，
避免命令处理器与定时任务并发修改时发生竞态。
"""

import threading
from pathlib import Path

from ..config import DATA_DIR, config
from ..utils import dumpjson, loadjson
from .player import Player

_STORE_FILE: Path = DATA_DIR / "player_info.json"
_lock = threading.RLock()
_data: dict[str, dict] = {}
_loaded = False  # 是否已从磁盘加载过（区分"未加载"与"已加载但为空"）


def _new_group() -> dict:
    """创建默认的群数据结构。"""
    return {"players": [], "subscribe_news": True, "subscribe_ti": True}


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict[str, dict]:
    """加载全部数据（惰性，仅首次读取磁盘）。"""
    global _data, _loaded
    with _lock:
        if _loaded:
            return _data
        _ensure_dir()
        raw: dict = {}
        if _STORE_FILE.exists():
            raw = loadjson(_STORE_FILE, {})
        migrated = False
        for gid, value in raw.items():
            if isinstance(value, list):
                # 旧格式：{群号: [Player, ...]} → 迁移为新格式
                migrated = True
                group = {"players": [], "subscribe_news": True, "subscribe_ti": True}
                for info in value:
                    if isinstance(info, dict):
                        group["players"].append(Player.from_dict(info))
                _data[str(gid)] = group
            elif isinstance(value, dict):
                # 新格式：{群号: {"players": [...], ...}}
                group = {"players": [], "subscribe_news": True, "subscribe_ti": True}
                for p_info in value.get("players", []):
                    if isinstance(p_info, dict):
                        group["players"].append(Player.from_dict(p_info))
                group["subscribe_news"] = bool(value.get("subscribe_news", True))
                group["subscribe_ti"] = bool(value.get("subscribe_ti", True))
                _data[str(gid)] = group
        _loaded = True
        if migrated:
            # 检测到旧格式后立即写回新格式，持久化迁移结果
            save()
        return _data


def save() -> None:
    """将内存数据写回 JSON 文件（含清空后的空数据）。"""
    global _data, _loaded
    with _lock:
        if not _loaded:
            return
        _ensure_dir()
        tmp = {}
        for gid, info in _data.items():
            tmp[gid] = {
                "players": [p.to_dict() for p in info["players"]],
                "subscribe_news": info.get("subscribe_news", True),
                "subscribe_ti": info.get("subscribe_ti", True),
            }
        dumpjson(tmp, _STORE_FILE)


def get_all() -> dict[str, list[Player]]:
    """返回全部群组订阅数据（{群号: [Player, ...]}）。"""
    return {gid: info["players"] for gid, info in load().items()}


def get_all_groups() -> dict[str, dict]:
    """返回全部群组完整数据（{群号: {"players": [...], "subscribe_news": bool, ...}}）。"""
    return dict(load())


def get_group(gid: str) -> list[Player]:
    """返回某群的订阅列表；不存在时注册该群并返回空列表。"""
    return load().setdefault(str(gid), _new_group())["players"]


def get_group_settings(gid: str) -> dict:
    """返回某群的设置（news / ti 开关）；不存在时创建默认。"""
    return load().setdefault(str(gid), _new_group())


def toggle_subscription(gid: str, key: str) -> bool:
    """切换某群某类订阅开关（key 为 "news"/"ti"）；返回切换后的状态。"""
    field = f"subscribe_{key}"
    with _lock:
        info = get_group_settings(str(gid))
        info[field] = not info.get(field, True)
        return info[field]


def set_subscription(gid: str, key: str, enabled: bool) -> bool:
    """将某群某类订阅设为指定开关状态（key 为 "news"/"ti"）；返回设置后的状态。"""
    field = f"subscribe_{key}"
    with _lock:
        info = get_group_settings(str(gid))
        info[field] = bool(enabled)
        return info[field]


def any_group_subscribed(key: str) -> bool:
    """是否存在任一群的订阅开关（如 "subscribe_ti"/"subscribe_news"）为开启状态。

    用于定时任务在没有任何群订阅时直接跳过网络请求，避免无谓的性能消耗。
    与 _broadcast 的过滤逻辑保持一致：缺失字段视为开启（默认 True）。
    """
    with _lock:
        return any(info.get(key, True) for info in load().values())


def upsert_player(gid: str, nickname: str, steam_id: int) -> str:
    """新增玩家；steam_id 已存在则更新昵称。返回提示文案。"""
    with _lock:
        players = load().setdefault(str(gid), _new_group())["players"]
        for p in players:
            if p.short_steamID == steam_id:
                p.nickname = nickname
                return "玩家已存在，更新昵称"
        players.append(Player(short_steamID=steam_id, nickname=nickname))
        return "玩家添加成功"


def delete_player(gid: str, nickname: str) -> str:
    """按昵称删除玩家；返回提示文案。"""
    with _lock:
        info = load().get(str(gid))
        if not info or not info["players"]:
            return "当前群组没有添加任何玩家"
        players = info["players"]
        for i, p in enumerate(players):
            if p.nickname == nickname:
                players.pop(i)
                return f"已删除玩家 {nickname}"
        return "未找到该玩家"


def set_display(gid: str, player_name: str, display: bool) -> str | None:
    """开启/关闭某玩家（或全体）的播报。返回提示文案；无需回复时返回 None。"""
    with _lock:
        data = load()
        if str(gid) not in data:
            return "当前群组没有添加任何玩家"
        players = data[str(gid)]["players"]
        if not players:
            return "当前群组没有添加任何玩家"
        if player_name == config.d2w_all_nickname:
            for p in players:
                p.display_recent_match = display
            return None  # 全体操作不单独回复
        for p in players:
            if p.nickname == player_name:
                p.display_recent_match = display
                return f"已{'开启' if display else '关闭'}{player_name}的播报"
        return "未找到该玩家"
