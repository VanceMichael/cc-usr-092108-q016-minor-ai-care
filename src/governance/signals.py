"""脱敏会话信号。

进入治理域的不是消息原文，而是由受控客户端在本地抽取的信号：
特征编号 + 强度/次数 + 可选的短语片段（长度受限、可被规则关闭）。
redact_signal 再做一层入域清洗（截断片段、剥离自由文本）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SNIPPET_LIMIT = 24

# 已登记特征码（feature codes），不承载原始语句
F_MOOD = "mood_score"            # 情绪分 0-100，越低越差
F_ISOLATION = "isolation_score"  # 孤立感分 0-100
F_SLEEP = "sleep_disrupt"        # 睡眠紊乱 0-100
F_SELFHARM = "self_harm_phrase"  # 自伤暗示短语命中计数
F_HARM_OTHER = "harm_other_phrase"  # 伤害他人暗示命中计数
F_HELPLESS = "helpless_phrase"   # 绝望/无价值短语计数
F_AI_BOND = "ai_exclusive_bond"  # “只愿和AI说”等排他依赖计数
F_SCHOOL_AVOID = "school_avoid"  # 拒校提及计数
F_GOOD_MOMENT = "good_moment"    # 积极互动计数
F_DIRECT_HELP = "direct_help_request"  # 直接向AI求助“我该怎么办”


@dataclass(frozen=True)
class Signal:
    pseudo: str
    at: str
    model: str           # 陪伴模型版本，例如 companion-7.2
    window: str          # 产生信号的客户端窗口标识，非对话内容
    features: dict[str, float]
    snippets: tuple[str, ...] = ()
    redacted: bool = True

    def get(self, code: str) -> float:
        return float(self.features.get(code, 0))


def redact_signal(raw: dict[str, Any]) -> Signal:
    """把客户端上报的字典清洗为域内信号。

    未知特征一律丢弃；snippet 强制截断并去重；不接受任何 message/text 字段。
    """
    known = {
        F_MOOD, F_ISOLATION, F_SLEEP, F_SELFHARM, F_HARM_OTHER,
        F_HELPLESS, F_AI_BOND, F_SCHOOL_AVOID, F_GOOD_MOMENT, F_DIRECT_HELP,
    }
    features: dict[str, float] = {}
    for key, value in (raw.get("features") or {}).items():
        if key not in known:
            continue
        number = float(value)
        features[key] = max(0.0, min(number, 1000.0))

    snippets: list[str] = []
    for text in (raw.get("snippets") or []):
        piece = str(text).replace("\n", " ").strip()
        if not piece:
            continue
        piece = piece[:SNIPPET_LIMIT]
        if piece not in snippets:
            snippets.append(piece)

    return Signal(
        pseudo=str(raw["pseudo"]),
        at=str(raw["at"]),
        model=str(raw["model"]),
        window=str(raw.get("window", "default")),
        features=features,
        snippets=tuple(snippets),
        redacted=True,
    )


class SignalHistory:
    def __init__(self) -> None:
        # unit_id -> (pseudo, Signal)，到期删除按单元摘除
        self._rows: dict[str, tuple[str, Signal]] = {}
        self._order: dict[str, list[str]] = {}

    def add(self, signal: Signal, unit_id: str) -> str:
        self._rows[unit_id] = (signal.pseudo, signal)
        self._order.setdefault(signal.pseudo, []).append(unit_id)
        return unit_id

    def for_(self, pseudo: str, since: str = "") -> list[Signal]:
        rows = [self._rows[uid][1] for uid in self._order.get(pseudo, []) if uid in self._rows]
        return [row for row in rows if row.at >= since] if since else list(rows)

    def snapshot_unit(self, unit_id: str) -> dict[str, Any]:
        if unit_id not in self._rows:
            return {}
        _, signal = self._rows[unit_id]
        return {"features": dict(signal.features), "snippets": list(signal.snippets)}

    def erase_unit(self, unit_id: str) -> None:
        if unit_id not in self._rows:
            return
        pseudo, _ = self._rows.pop(unit_id)
        if unit_id in self._order.get(pseudo, []):
            self._order[pseudo].remove(unit_id)

    def latest_model(self, pseudo: str) -> str | None:
        rows = self.for_(pseudo)
        return rows[-1].model if rows else None


class TrendStore:
    """普通波动只沉淀去标识化趋势。

    桶内人数不足 k 时该桶不输出（k-匿名）；任何输出都不含化名、
    不含片段，只有班级/年级聚合与分布统计。
    """

    def __init__(self, k: int = 5) -> None:
        self.k = k
        # (年级, 周, 指标) -> {化名: 值}；k-匿名按去重人数计
        self._buckets: dict[tuple[str, str, str], dict[str, float]] = {}

    def add(self, pseudo: str, grade: str, week: str, metric: str, value: float) -> None:
        self._buckets.setdefault((grade, week, metric), {})[pseudo] = float(value)

    def export(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (grade, week, metric), members in sorted(self._buckets.items()):
            if len(members) < self.k:
                # 桶内去重人数不足 k，抑制整桶
                continue
            values = list(members.values())
            avg = round(sum(values) / len(values), 1)
            out.append({
                "grade": grade, "week": week, "metric": metric,
                "n": len(values), "avg": avg,
                "suppressed": False,
            })
        return out

    def suppressed_buckets(self) -> int:
        return sum(1 for members in self._buckets.values() if len(members) < self.k)
