"""版本化风险规则引擎。

规则集以 JSON 发布（见 ``fixtures/rules_v*.json``），每次换版生成新版本号，
后台换版时写审计并签发证书。引擎对每条脱敏信号给出：

- 风险等级（取命中规则的最高等级）；
- 命中规则清单（规则号、版本、命中词、权重、人话理由）；
- 一段可读的总体 rationale。

引擎是纯函数式的：不存状态、不发通知，只负责“判断 + 说明依据”。
是否升级、是否通知监护人，由 ``escalation`` 决定，且人始终能否决。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.governance.models import Assessment, RiskLevel, RuleHit, Signal


@dataclass(frozen=True)
class Rule:
    rule_id: str
    level: int
    weight: int
    any_terms: tuple[str, ...]       # 命中任一即触发（文本关键词）
    all_features: tuple[str, ...]    # 需同时具备的结构化特征
    min_days_in_window: int          # 窗口内至少出现的天数（持续类规则用）
    reason: str                      # 给人看的命中理由


@dataclass(frozen=True)
class RuleSet:
    version: str
    window_days: int
    rules: tuple[Rule, ...]
    issued_at: datetime
    supersedes: Optional[str] = None

    @staticmethod
    def load(path: Path) -> "RuleSet":
        raw = json.loads(path.read_text(encoding="utf-8"))
        required = {"version", "window_days", "issued_at", "rules"}
        if not required.issubset(raw):
            raise ValueError("规则集缺少必要字段")
        rules = tuple(
            Rule(
                rule_id=r["rule_id"],
                level=int(r["level"]),
                weight=int(r.get("weight", 1)),
                any_terms=tuple(r.get("any_terms", ())),
                all_features=tuple(r.get("all_features", ())),
                min_days_in_window=int(r.get("min_days_in_window", 1)),
                reason=r["reason"],
            )
            for r in raw["rules"]
        )
        if not rules:
            raise ValueError("规则集不能为空")
        return RuleSet(
            version=str(raw["version"]),
            window_days=int(raw["window_days"]),
            rules=rules,
            issued_at=datetime.fromisoformat(raw["issued_at"]),
            supersedes=raw.get("supersedes"),
        )


def _level_of(rule: Rule, signal: Signal, history: tuple[Signal, ...]) -> Optional[RuleHit]:
    """判断单条规则是否命中；持续类规则结合窗口内历史信号。"""
    matched_terms = tuple(t for t in rule.any_terms if t in signal.text)
    has_terms = not rule.any_terms or bool(matched_terms)
    has_features = all(f in signal.features for f in rule.all_features)
    if not (has_terms and has_features):
        return None

    if rule.min_days_in_window > 1:
        # 持续类：统计窗口内命中同一规则的不同日期数
        days = {signal.observed_at.date()}
        for past in history:
            past_terms = tuple(t for t in rule.any_terms if t in past.text)
            past_ok = (not rule.any_terms or past_terms) and all(
                f in past.features for f in rule.all_features
            )
            if past_ok:
                days.add(past.observed_at.date())
        if len(days) < rule.min_days_in_window:
            return None

    return RuleHit(
        rule_id=rule.rule_id,
        rule_version="",  # 由引擎填规则集版本
        weight=rule.weight,
        matched_terms=matched_terms,
        reason=rule.reason,
    )


class RuleEngine:
    """对信号应用当前规则集，产出带依据的评估。"""

    def __init__(self, rule_set: RuleSet):
        self._rule_set = rule_set

    @property
    def version(self) -> str:
        return self._rule_set.version

    @property
    def rule_set(self) -> RuleSet:
        return self._rule_set

    def assess(
        self,
        signal: Signal,
        *,
        history: tuple[Signal, ...] = (),
        at: Optional[datetime] = None,
    ) -> Assessment:
        hits: list[RuleHit] = []
        for rule in self._rule_set.rules:
            hit = _level_of(rule, signal, history)
            if hit is not None:
                hits.append(RuleHit(
                    rule_id=hit.rule_id,
                    rule_version=self._rule_set.version,
                    weight=hit.weight,
                    matched_terms=hit.matched_terms,
                    reason=hit.reason,
                ))

        level = max((h_weight_level(self._rule_set, h.rule_id) for h in hits), default=RiskLevel.ROUTINE)
        rationale = _rationale(self._rule_set, hits, level)
        return Assessment(
            signal_id=signal.signal_id,
            student_id=signal.student_id,
            level=int(level),
            assessed_at=at or signal.observed_at,
            rule_set_version=self._rule_set.version,
            hits=tuple(hits),
            rationale=rationale,
            window_days=self._rule_set.window_days,
        )


def h_weight_level(rule_set: RuleSet, rule_id: str) -> int:
    for rule in rule_set.rules:
        if rule.rule_id == rule_id:
            return rule.level
    return int(RiskLevel.ROUTINE)


def _rationale(rule_set: RuleSet, hits: list[RuleHit], level: int) -> str:
    if not hits:
        return "未命中任何风险规则，按普通波动处理，仅计入去标识化趋势。"
    top = max(hits, key=lambda h: h_weight_level(rule_set, h.rule_id))
    others = len(hits) - 1
    suffix = f"；另有{others}条规则同时命中" if others else ""
    return f"主要依据规则[{top.rule_id}]：{top.reason}{suffix}。"
