"""订阅玩家数据模型。"""

from dataclasses import dataclass, field
from typing import Any

from ..utils import player_team


@dataclass
class Player:
    """一位被订阅的 DOTA2 玩家。"""

    short_steamID: int = 0
    nickname: str = ""
    last_DOTA2_match_ID: int = 0
    display_recent_match: bool = True
    # 连胜 / 连败纪录：每场战报按本局胜负就地累加（二者必有一个为 0）
    win_streak: int = 0
    lose_streak: int = 0
    # 已计入纪录的比赛 ID：同一玩家被多个群订阅时同一场会被处理多次，
    # 用它保证一场只累加一次
    streak_match_id: int = 0
    # 连续「战绩被隐藏」次数：Steam 返回 status 15 时累加，Steam 恢复正常时归零。
    # 达到阈值才回退小黑盒（见 services/match_history.py），用于控制小黑盒访问频率
    hidden_fail_count: int = 0
    # 最近一次从小黑盒采纳的比赛 ID：小黑盒数据可能滞后，用它作为跨数据源判旧的基准
    last_xiaoheihe_match_id: int = 0
    # 最近一场比赛的对局数据（由 generate_message 填充）
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为可持久化的 dict。"""
        return {
            "short_steamID": self.short_steamID,
            "nickname": self.nickname,
            "last_DOTA2_match_ID": self.last_DOTA2_match_ID,
            "display_recent_match": self.display_recent_match,
            "win_streak": self.win_streak,
            "lose_streak": self.lose_streak,
            "streak_match_id": self.streak_match_id,
            "hidden_fail_count": self.hidden_fail_count,
            "last_xiaoheihe_match_id": self.last_xiaoheihe_match_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Player":
        """从持久化 dict 反序列化出 Player。"""
        return cls(
            short_steamID=int(d.get("short_steamID", 0)),
            nickname=d.get("nickname", ""),
            last_DOTA2_match_ID=int(d.get("last_DOTA2_match_ID", 0) or 0),
            display_recent_match=bool(d.get("display_recent_match", True)),
            win_streak=int(d.get("win_streak", 0) or 0),
            lose_streak=int(d.get("lose_streak", 0) or 0),
            streak_match_id=int(d.get("streak_match_id", 0) or 0),
            hidden_fail_count=int(d.get("hidden_fail_count", 0) or 0),
            last_xiaoheihe_match_id=int(d.get("last_xiaoheihe_match_id", 0) or 0),
        )

    def load_player_info(self, info: dict) -> None:
        """从 openDota/Steam 的单局玩家数据中提取本插件需要的统计字段。"""
        kills = info.get("kills", 0)
        deaths = info.get("deaths", 0)
        assists = info.get("assists", 0)
        self.stats = {
            "kill": kills,
            "death": deaths,
            "assist": assists,
            "kda": (kills + assists) / max(deaths, 1),
            "dota2_team": player_team(info),
            "hero": info.get("hero_id"),
            "last_hit": info.get("last_hits", 0),
            "damage": info.get("hero_damage", 0),
            "gpm": info.get("gold_per_min", 0),
            "xpm": info.get("xp_per_min", 0),
            "benchmarks": info.get("benchmarks"),
            "xiaoheihe_score": info.get("xiaoheihe_score"),
        }
