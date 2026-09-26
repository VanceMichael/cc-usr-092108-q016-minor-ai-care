"""自动判定结论与人工否决。

每条结论都携带规则版本与逐条命中依据；人工否决不删除机器结论，
而是追加一条带理由的覆盖记录，两者都可被审查人员复查。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .rules import NORMAL


@dataclass(frozen=True)
class Verdict:
    pseudo: str
    level: str
    rule_pack_version: str
    hits: tuple[dict[str, Any], ...]
    decided_at: str
    source: str = "auto"            # auto | manual-override
    override_reason: str | None = None
    staff_id: str | None = None
    supersedes: int | None = None   # 被覆盖结论的序号

    @property
    def basis(self) -> list[str]:
        """可直接展示给专业人员的中文依据列表。"""
        lines: list[str] = []
        for hit in self.hits:
            fact_bits = []
            for fact in hit["facts"]:
                fact_bits.append(
                    f"{fact['feature']} {fact['op']} {fact['threshold']}"
                    f"（近{fact['window_days']}日命中{fact['hits']}次，"
                    f"观察值{fact['observed']}）"
                )
            lines.append(f"[{hit['rule']}] {hit['rationale']}：" + "；".join(fact_bits))
        return lines

    @property
    def is_normal(self) -> bool:
        return self.level == NORMAL


def normal_verdict(pseudo: str, rule_pack_version: str, decided_at: str) -> Verdict:
    return Verdict(pseudo, NORMAL, rule_pack_version, (), decided_at)
