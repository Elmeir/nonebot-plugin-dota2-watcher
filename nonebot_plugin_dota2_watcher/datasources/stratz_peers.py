"""Stratz peers 查询的共享参数与响应解析（开黑记录 / 职业选手对战记录共用）。"""

# 与 stratz.com peers 页面一致的聚合参数：服务端完成全量历史聚合，单次拉取
PEERS_VARS_BASE = {
    "playerTeammateSort": "WITH",  # AGAINST 由对手组覆盖
    "matchGroupOrderBy": "MATCH_COUNT",
    "orderBy": "DESC",
    "matchLimitMin": 1,
    "skip": 0,
    "take": 10000,  # 与 stratz.com 一致：单次拉全量
}


def parse_peer_entry(entry: dict) -> tuple[int, int, int, int] | None:
    """解析一条 peers 记录为 (steam_id, match_count, win_count, last_ts)；无效返回 None。"""
    account = entry.get("steamAccount") or {}
    steam_id = account.get("id")
    if not steam_id:
        return None
    return (
        int(steam_id),
        int(entry.get("matchCount") or 0),
        int(entry.get("winCount") or 0),
        int(entry.get("lastMatchDateTime") or 0),
    )
