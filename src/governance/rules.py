"""风险规则包与分级。

规则包整体版本化（规则换版时旧案件保留适用的旧版号）；
每条规则给出等级、机器可读条件与中文依据模板，自动判定时
逐条记录命中事实，保证“自动判断必须说明依据”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import signals as sig

NORMAL = "normal"
WATCH = "watch"      # 持续孤立/情绪走低：校内关注
URGENT = "urgent"    # 自伤暗示：有资质心理人员限时接手
EMERGENCY = "emergency"  # 现实伤害紧迫：立即人工接触 + 必要联络

LEVELS = (NORMAL, WATCH, URGENT, EMERGENCY)
RANK = {NORMAL: 0, WATCH: 1, URGENT: 2, EMERGENCY: 3}
_RANK = RANK


def higher(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


@dataclass(frozen=True)
class Condition:
    feature: str
    op: str
    threshold: float
    window_days: int
    min_hits: int = 1
    aggregate: str = "signals"  # signals=命中信号条数；sum=窗口内特征值累计

    def evaluate(self, rows: list[sig.Signal]) -> tuple[bool, dict[str, Any]]:
        if not rows:
            return False, {}
        from .clock import parse
        start = parse(rows[-1].at)
        recent = [
            r for r in rows
            if (start - parse(r.at)).total_seconds() <= self.window_days * 86400
        ]
        hits = [r for r in recent if _compare(r.get(self.feature), self.op, self.threshold)]
        if self.aggregate == "sum":
            nonzero = [r for r in recent if r.get(self.feature) > 0]
            total = round(sum(r.get(self.feature) for r in recent), 1)
            matched = _compare(total, self.op, self.threshold) and bool(nonzero)
            observed = total
            hit_count = len(nonzero)
        else:
            matched = len(hits) >= self.min_hits
            values = [r.get(self.feature) for r in hits]
            observed = max(values) if self.op in (">=", ">") and values else (
                min(values) if values else 0
            )
            hit_count = len(hits)
        if not matched:
            return False, {}
        return True, {
            "feature": self.feature,
            "op": self.op,
            "threshold": self.threshold,
            "window_days": self.window_days,
            "hits": hit_count,
            "required_hits": self.min_hits,
            "aggregate": self.aggregate,
            "observed": observed,
        }


def _compare(value: float, op: str, threshold: float) -> bool:
    if op == ">=":
        return value >= threshold
    if op == ">":
        return value > threshold
    if op == "<=":
        return value <= threshold
    if op == "<":
        return value < threshold
    raise ValueError(op)


@dataclass(frozen=True)
class Rule:
    code: str
    level: str
    conditions: tuple[Condition, ...]
    rationale: str
    human_hint: str

    def evaluate(self, rows: list[sig.Signal]) -> tuple[bool, list[dict[str, Any]]]:
        facts: list[dict[str, Any]] = []
        for condition in self.conditions:
            matched, fact = condition.evaluate(rows)
            if not matched:
                return False, []
            facts.append(fact)
        return True, facts


@dataclass(frozen=True)
class RulePack:
    version: str
    rules: tuple[Rule, ...]

    def evaluate(self, rows: list[sig.Signal]) -> tuple[str, list[dict[str, Any]]]:
        best = NORMAL
        hits: list[dict[str, Any]] = []
        for rule in self.rules:
            matched, facts = rule.evaluate(rows)
            if matched:
                if _RANK[rule.level] > _RANK[best]:
                    best = rule.level
                hits.append({
                    "rule": rule.code,
                    "level": rule.level,
                    "rationale": rule.rationale,
                    "facts": facts,
                    "human_hint": rule.human_hint,
                })
        return best, hits


def default_pack(version: str = "risk-rules-v1") -> RulePack:
    return RulePack(version=version, rules=(
        Rule(
            code="R-EMER-01",
            level=EMERGENCY,
            conditions=(Condition(sig.F_HARM_OTHER, ">=", 1, 1),),
            rationale="近1日出现伤害他人现实计划或行动暗示",
            human_hint="立即由值班心理老师发起现实接触，确认安全；必要时启动紧急联络。",
        ),
        Rule(
            code="R-EMER-02",
            level=EMERGENCY,
            conditions=(Condition(sig.F_SELFHARM, ">=", 2, 1, aggregate="sum"),),
            rationale="24小时内自伤暗示累计命中≥2次，存在紧迫自伤风险",
            human_hint="立即人工接触，全程陪伴，禁止仅由AI回应。",
        ),
        Rule(
            code="R-URG-01",
            level=URGENT,
            conditions=(Condition(sig.F_SELFHARM, ">=", 1, 3),),
            rationale="近3日出现自伤暗示",
            human_hint="由有资质心理人员在24小时内完成首次现实谈话。",
        ),
        Rule(
            code="R-URG-02",
            level=URGENT,
            conditions=(
                Condition(sig.F_MOOD, "<=", 30, 7, 3),
                Condition(sig.F_HELPLESS, ">=", 2, 7, aggregate="sum"),
            ),
            rationale="近7日情绪分3次≤30且绝望短语累计≥2次，情绪风险叠加",
            human_hint="心理老师约谈，评估是否需要校外专业转介。",
        ),
        Rule(
            code="R-WATCH-01",
            level=WATCH,
            conditions=(Condition(sig.F_ISOLATION, ">=", 70, 14, 3),),
            rationale="近14日孤立感3次≥70，呈持续孤立",
            human_hint="班主任在日常场景中增加关心，不向学生展示风险标签。",
        ),
        Rule(
            code="R-WATCH-02",
            level=WATCH,
            conditions=(
                Condition(sig.F_AI_BOND, ">=", 3, 14, aggregate="sum"),
                Condition(sig.F_ISOLATION, ">=", 50, 14, 2),
            ),
            rationale="近14日AI排他依赖累计≥3次且孤立感2次≥50，现实关系可能被削弱",
            human_hint="心理老师发起一次低压力的现实兴趣连接。",
        ),
        Rule(
            code="R-WATCH-03",
            level=WATCH,
            conditions=(Condition(sig.F_SLEEP, ">=", 70, 10, 3),),
            rationale="近10日睡眠紊乱3次≥70，可能影响情绪稳定",
            human_hint="纳入周度关注名单，观察睡眠与情绪联动。",
        ),
    ))


def relaxed_pack() -> RulePack:
    """规则换版样例：放宽 WATCH 阈值、收紧紧急重复自伤口径。"""
    return RulePack(version="risk-rules-v2", rules=(
        Rule(
            code="R-EMER-01",
            level=EMERGENCY,
            conditions=(Condition(sig.F_HARM_OTHER, ">=", 1, 1),),
            rationale="近1日出现伤害他人现实计划或行动暗示",
            human_hint="立即由值班心理老师发起现实接触，确认安全；必要时启动紧急联络。",
        ),
        Rule(
            code="R-EMER-02",
            level=EMERGENCY,
            conditions=(Condition(sig.F_SELFHARM, ">=", 3, 1, aggregate="sum"),),
            rationale="24小时内自伤暗示累计命中≥3次，存在紧迫自伤风险",
            human_hint="立即人工接触，全程陪伴，禁止仅由AI回应。",
        ),
        Rule(
            code="R-URG-01",
            level=URGENT,
            conditions=(Condition(sig.F_SELFHARM, ">=", 1, 3),),
            rationale="近3日出现自伤暗示",
            human_hint="由有资质心理人员在24小时内完成首次现实谈话。",
        ),
        Rule(
            code="R-WATCH-01",
            level=WATCH,
            conditions=(Condition(sig.F_ISOLATION, ">=", 80, 14, 3),),
            rationale="近14日孤立感3次≥80，呈持续孤立（v2上调阈值）",
            human_hint="班主任在日常场景中增加关心，不向学生展示风险标签。",
        ),
        Rule(
            code="R-WATCH-02",
            level=WATCH,
            conditions=(
                Condition(sig.F_AI_BOND, ">=", 5, 14, aggregate="sum"),
                Condition(sig.F_ISOLATION, ">=", 60, 14, 2),
            ),
            rationale="近14日AI排他依赖累计≥5次且孤立感2次≥60（v2上调阈值）",
            human_hint="心理老师发起一次低压力的现实兴趣连接。",
        ),
    ))


class RulePackBook:
    def __init__(self) -> None:
        self._packs: dict[str, RulePack] = {}

    def publish(self, pack: RulePack) -> None:
        self._packs[pack.version] = pack

    def get(self, version: str) -> RulePack:
        return self._packs[version]

    def versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._packs))


class RuleEngine:
    """无状态判定器：给定规则包与历史信号，产出等级与命中依据。"""

    def evaluate(self, pack: RulePack, rows: list[sig.Signal]) -> tuple[str, list[dict[str, Any]]]:
        return pack.evaluate(rows)
