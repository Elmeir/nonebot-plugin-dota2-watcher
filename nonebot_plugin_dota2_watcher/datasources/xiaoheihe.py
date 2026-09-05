"""小黑盒 Dota2 比赛详情数据源（httpx 异步实现，无需登录）。

从公开接口 `https://api.xiaoheihe.cn/game/dota2/match_detail` 拉取指定
`match_id` 的比赛结果，并转换为与 OpenDota `matches/{id}` 兼容的结构，
作为 OpenDota 比赛数据不可用时的回退数据源。

说明：
- 该接口为公开接口：无需 Cookie、无需扫码登录即可获取比赛结果。
- hkey 签名算法独立移植自 MIT 许可的 heybox-core 实现。
- 返回结构兼容 `generators/match_report.py`（战报图片）与
  `generators/match_builder.py`（战报文本）所需字段；其中 `team_number` /
  `isRadiant` 等由 player_slot 推导，与 OpenDota 保持一致。
"""

import hashlib
import os
import re
import secrets
import time

from ..config import DATA_DIR, MATCHES_DIR, OPENDOTA_ITEMS_URL
from ..utils import DOTA2HTTPError, dumpjson, get_json, loadjson

API_BASE = "https://api.xiaoheihe.cn"
# 注意：该接口 URL 不能带末尾斜杠（带斜杠会 404）；hkey 签名内部会自行归一化路径，无影响
MATCH_DETAIL_PATH = "/game/dota2/match_detail"

# 小黑盒 Dota2 比赛详情接口固定请求参数（与 tests/_xiaoheihe.py 保持一致）
MATCH_BASE_PARAMS = {
    "os_type": "web",
    "app": "heybox",
    "client_type": "mobile",
    "version": "999.0.4",
    "x_client_type": "web",
    "x_os_type": "Android",
    "x_app": "maxjia",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 15; NTH-AN00 Build/V417IR; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 "
        "Safari/537.36 ApiMaxJia/1.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.xiaoheihe.cn",
    "X-Requested-With": "com.dotamax.app",
    "Referer": "https://www.xiaoheihe.cn/",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}


# ============================================================
# 请求签名（hkey），独立移植自 MIT 许可的 heybox-core 实现
# ============================================================
_HKEY_ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def _map_to_alphabet(value, alphabet):
    return "".join(alphabet[ord(c) % len(alphabet)] for c in value)


def _xtime(v):
    return (255 & ((v << 1) ^ 27)) if v & 128 else v << 1


def _mul3(v):
    return _xtime(v) ^ v


def _mul4(v):
    return _mul3(_xtime(v))


def _mul8(v):
    return _mul4(_mul3(_xtime(v)))


def _mul14(v):
    return _mul8(v) ^ _mul4(v) ^ _mul3(v)


def _mix_tail(values):
    a, b, c, d = values[:4]
    return [
        _mul14(a) ^ _mul8(b) ^ _mul4(c) ^ _mul3(d),
        _mul3(a) ^ _mul14(b) ^ _mul8(c) ^ _mul4(d),
        _mul4(a) ^ _mul3(b) ^ _mul14(c) ^ _mul8(d),
        _mul8(a) ^ _mul4(b) ^ _mul3(c) ^ _mul14(d),
        *values[4:],
    ]


def generate_hkey(path, timestamp, nonce):
    """根据路径、时间戳、nonce 生成上游要求的 hkey 签名。"""
    normalized = "/" + "/".join(p for p in str(path).split("/") if p) + "/"
    parts = (
        _map_to_alphabet(str(timestamp), _HKEY_ALPHABET[:-2]),
        _map_to_alphabet(normalized, _HKEY_ALPHABET),
        _map_to_alphabet(str(nonce), _HKEY_ALPHABET),
    )
    interleaved = "".join(
        part[i] for i in range(max(len(p) for p in parts)) for part in parts if i < len(part)
    )[:20]
    digest = hashlib.md5(interleaved.encode(), usedforsecurity=False).hexdigest()
    mixed = _mix_tail([ord(c) for c in digest[-6:]])
    suffix = str(sum(mixed) % 100).zfill(2)
    prefix = _map_to_alphabet(digest[:5], _HKEY_ALPHABET[:-4])
    return f"{prefix}{suffix}"


def sign_params(path, base_params=None):
    """为指定路径补充 _time / nonce / hkey 签名参数。"""
    params = dict(base_params or {})
    timestamp = int(time.time())
    nonce = (
        hashlib.md5(f"{timestamp}{secrets.token_hex(16)}".encode(), usedforsecurity=False)
        .hexdigest()
        .upper()
    )
    params["_time"] = str(timestamp)
    params["nonce"] = nonce
    params["hkey"] = generate_hkey(path, timestamp, nonce)
    return params


# ============================================================
# 数据转换工具
# ============================================================
_items_name2id_cache = None

# 与 match_report.py 的 _load_items_cache 共用同一份本地物品缓存（id -> name）
ITEM_CACHE_FILE = os.path.join(str(DATA_DIR), "items.json")


def _load_items_name2id_from_cache():
    """从本地 items.json（id -> name）反转为 name -> id 映射。

    读取失败或为空时返回空字典。
    """
    cached = loadjson(ITEM_CACHE_FILE)
    if not isinstance(cached, dict):
        return {}
    name2id = {}
    for iid, name in cached.items():
        try:
            name2id[str(name)] = int(iid)
        except (TypeError, ValueError):
            continue
    return name2id


async def load_item_name_to_id():
    """构建「物品名 -> 物品 ID」映射（进程内缓存）。

    优先读取本地 items.json（由 match_report 维护）；本地无缓存或为空时，
    才回退到 OpenDota 物品常量。失败时返回空字典：仅导致物品栏按空位处理，
    不影响其它比赛数据。
    """
    global _items_name2id_cache
    if _items_name2id_cache:
        return _items_name2id_cache

    name2id = _load_items_name2id_from_cache()
    if name2id:
        _items_name2id_cache = name2id
        return name2id

    name2id = {}
    try:
        items = await get_json(OPENDOTA_ITEMS_URL)
        for key, item in items.items():
            iid = item.get("id")
            if iid is not None:
                name2id[key.replace("item_", "")] = int(iid)
    except Exception:
        name2id = {}
    _items_name2id_cache = name2id
    return name2id


def item_name_from_url(url):
    """从小黑盒物品图片 URL 提取物品内部名（去掉 _lg 后缀等）。"""
    if not url:
        return ""
    name = str(url).rstrip("/").rsplit("/", 1)[-1]
    name = name.split("?", 1)[0]
    name = name.rsplit(".", 1)[0]
    if name.endswith("_lg"):
        name = name[:-3]
    return name


def item_id(url, name2id):
    """从物品图片 URL 解析出物品 ID；解析失败返回 0。"""
    name = item_name_from_url(url)
    if not name:
        return 0
    return name2id.get(name, 0)


def leading_int(value):
    """从 '369(+15.2)'、'107/5' 这类字符串中解析出开头的整数。"""
    s = str(value).replace(",", "").strip()
    m = re.match(r"^\d+", s)
    return int(m.group()) if m else 0


def hero_data_value(hero_data, desc):
    """从 hero_data 列表里取指定 desc 对应的 value；未命中返回空串。"""
    for hd in hero_data or []:
        if hd.get("desc") == desc:
            return hd.get("value", "")
    return ""


def split_int(value):
    """解析 '107/5' 这类 a/b 字符串，返回 (a, b)；解析失败返回 (0, 0)。"""
    parts = str(value or "").split("/")
    try:
        a = int(parts[0]) if parts[0].strip() else 0
        b = int(parts[1]) if len(parts) > 1 and parts[1].strip() else 0
        return a, b
    except (ValueError, IndexError):
        return 0, 0


def parse_rank_tier(dan_icon):
    """从段位图标 URL 解析 Dota2 段位编码（段位*10+星级）。"""
    if not dan_icon:
        return 0
    stem = str(dan_icon).rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    prefixes = [
        ("immortal", 8, 0),
        ("divine", 7, 0),
        ("ancient", 6, 0),
        ("legend", 5, 0),
        ("archon", 4, 0),
        ("crusader", 3, 0),
        ("guardian", 2, 0),
        ("herald", 1, 0),
    ]
    m = re.search(r"_(\d)$", stem)
    star = int(m.group(1)) if m else 0
    for prefix, rank_num, default_star in prefixes:
        if stem.startswith(prefix):
            star = star if star else default_star
            return rank_num * 10 + min(star, 5)
    return 0


def parse_duration(value):
    """'47:28' 或秒数 -> 秒。"""
    s = str(value or "").strip()
    if ":" in s:
        try:
            m, sec = s.split(":", 1)
            return int(m) * 60 + int(sec)
        except ValueError:
            return 0
    return leading_int(s)


def convert_player(xp, duration, name2id):
    """把小黑盒玩家数据转换为 OpenDota player 结构。"""
    kda = xp.get("kda") or {}
    hero_info = xp.get("hero_info") or {}
    hero_data = xp.get("hero_data") or []

    account_id = xp.get("account_id")
    try:
        account_id = int(account_id) if account_id not in (None, "") else None
    except (TypeError, ValueError):
        account_id = None

    # 接口对隐私/被风控玩家会返回字面占位昵称「匿名玩家」，视为无名
    xh_name = str(xp.get("name") or "").strip()
    if xh_name == "匿名玩家":
        xh_name = ""

    player_slot = int(xp.get("playerSlot") or 0)

    item_ids = [item_id(u, name2id) for u in (xp.get("items") or [])]
    while len(item_ids) < 6:
        item_ids.append(0)
    bp_ids = [item_id(u, name2id) for u in (xp.get("backpack") or [])]
    while len(bp_ids) < 3:
        bp_ids.append(0)

    xp_per_min = leading_int(hero_data_value(hero_data, "XPM"))
    gpm = leading_int(hero_data_value(hero_data, "GPM"))
    last_hits, denies = split_int(hero_data_value(hero_data, "正补/反补"))
    creeps_stacked = leading_int(hero_data_value(hero_data, "堆叠野怪"))
    rune_pickups = leading_int(hero_data_value(hero_data, "赏金符"))

    radar_data = xp.get("radar_data_list") or []
    score_raw = hero_data_value(radar_data, "综合")
    try:
        xiaoheihe_score = float(str(score_raw).replace(",", "").strip())
    except (TypeError, ValueError):
        xiaoheihe_score = None

    return {
        "player_slot": player_slot,
        "team_number": 0 if player_slot < 128 else 1,
        "isRadiant": player_slot < 128,
        "hero_id": int(hero_info.get("hero_id") or 0),
        "account_id": account_id,
        "personaname": xh_name,
        # 小黑盒认证选手标记：仅认证玩家带 team_info 字段（口径较宽，含平台认证非职业玩家）
        "is_pro": bool(xp.get("team_info")),
        "level": int(hero_info.get("level") or 0),
        "kills": int(kda.get("kill") or 0),
        "deaths": int(kda.get("death") or 0),
        "assists": int(kda.get("assist") or 0),
        "kda": float(kda.get("kd") or 0),
        "xiaoheihe_score": xiaoheihe_score,
        "last_hits": last_hits,
        "denies": denies,
        "creeps_stacked": creeps_stacked,
        "rune_pickups": rune_pickups,
        "net_worth": int(xp.get("gold") or 0),
        "gold_per_min": gpm,
        "xp_per_min": xp_per_min,
        "total_xp": int(xp_per_min * max(duration / 60, 0)),
        "hero_damage": int(xp.get("damage") or 0),
        "tower_damage": leading_int(hero_data_value(hero_data, "塔伤")),
        "hero_healing": leading_int(hero_data_value(hero_data, "治疗")),
        "stuns": 0,
        "damage_inflictor_received": {},
        "item_0": item_ids[0],
        "item_1": item_ids[1],
        "item_2": item_ids[2],
        "item_3": item_ids[3],
        "item_4": item_ids[4],
        "item_5": item_ids[5],
        "item_neutral": item_id(xp.get("neutral"), name2id),
        "backpack_0": bp_ids[0],
        "backpack_1": bp_ids[1],
        "backpack_2": bp_ids[2],
        "aghanims_scepter": int(xp.get("aghanims_scepter") or 0),
        "aghanims_shard": int(xp.get("aghanims_shard") or 0),
        "purchase_log": [],
        "permanent_buffs": [],
        "rank_tier": parse_rank_tier(xp.get("dan_icon")),
        "lane_role": 0,
        "has_bkb": False,
    }


def team_kills(team):
    """队伍击杀总数（用于作为比分）。"""
    try:
        return int(str(team.get("kill") or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0


def convert_match(result, match_id, name2id):
    """把小黑盒比赛详情转换为 OpenDota matches/{id} 结构。"""
    radiant = result.get("radiant") or {}
    dire = result.get("dire") or {}
    match_info = result.get("match_info") or {}

    radiant_score = team_kills(radiant)
    dire_score = team_kills(dire)

    if int(radiant.get("win") or 0) == 1:
        radiant_win = True
    elif int(dire.get("win") or 0) == 1:
        radiant_win = False
    else:
        radiant_win = radiant_score > dire_score

    duration = parse_duration(match_info.get("duration"))
    # 小黑盒数据问题：finish_time 字段实际就是 OpenDota 的 start_time，
    # 直接作为开始时间使用，无需再减去持续时间。
    start_time = int(match_info.get("finish_time") or 0)

    players = []
    for team in (radiant, dire):  # 天辉在前、夜魇在后，与 OpenDota 顺序一致
        plist = sorted(
            team.get("player_list") or [],
            key=lambda x: int(x.get("playerSlot") or 0),
        )
        for xp in plist:
            players.append(convert_player(xp, duration, name2id))

    return {
        "match_id": int(match_id),
        "radiant_win": radiant_win,
        "radiant_score": radiant_score,
        "dire_score": dire_score,
        "duration": duration,
        "start_time": start_time,
        "from_valve": True,
        "data_source": "xiaoheihe",
        # 原始文本字段：小黑盒已提供可直接展示的中文文案，无需再查表转换
        "server": str(result.get("server") or ""),
        "mode_desc": str(match_info.get("mode_desc") or ""),
        "players": players,
    }


# ============================================================
# 请求入口
# ============================================================
async def fetch_xiaoheihe_match(match_id) -> dict:
    """请求小黑盒公开接口并转换为 OpenDota 兼容结构（不写比赛缓存）。

    供战报生成器做匿名玩家数据补充使用：不落盘可避免小黑盒简化数据
    （无 damage_inflictor 等分析字段）覆盖 OpenDota 的完整缓存。
    请求失败或上游返回异常时抛出 DOTA2HTTPError。
    """
    params = sign_params(MATCH_DETAIL_PATH, {**MATCH_BASE_PARAMS, "match_id": str(int(match_id))})
    try:
        data = await get_json(API_BASE + MATCH_DETAIL_PATH, params=params, headers=HEADERS)
    except ValueError:
        raise DOTA2HTTPError("小黑盒回退数据源返回解析失败")
    if not isinstance(data, dict) or data.get("status") != "ok" or not data.get("result"):
        raise DOTA2HTTPError(f"小黑盒回退数据源返回异常：{str(data)[:200]}")

    name2id = await load_item_name_to_id()
    match = convert_match(data["result"], match_id, name2id)

    # players 为空说明上游没有返回玩家数据，等价于无数据，抛出异常
    if not match["players"]:
        raise DOTA2HTTPError(f"小黑盒回退数据源返回异常：players 为空（match_id={match_id}）")
    return match


async def request_match_info_xiaoheihe(match_id):
    """从小黑盒公开接口拉取比赛结果，转换为 OpenDota 兼容结构并缓存到本地。

    返回 dict；请求失败或上游返回异常时抛出 DOTA2HTTPError。
    """
    match = await fetch_xiaoheihe_match(match_id)

    # 写入 match_report 使用的比赛缓存，便于战报图片生成器直接复用。
    try:
        os.makedirs(MATCHES_DIR, exist_ok=True)
        cache_file = os.path.join(str(MATCHES_DIR), f"{int(match_id)}.json")
        dumpjson(match, cache_file)
    except Exception:
        # 缓存失败不影响返回结果
        pass

    return match
