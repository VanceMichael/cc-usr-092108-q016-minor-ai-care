"""三视图：学生、监护人、专业人员看到的东西严格不同。

- 学生视图：明确“谁能看到我的哪些信息”，列出每一类数据的可见方与用途；
- 监护人视图：只收到必要的安全结论，看不到任何逐句对话；
- 专业人员视图：审查一次升级是否及时完成了真实人际接力，
  能看到规则依据、时间线、SLA 是否满足，但同样看不到无关的闲聊内容。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.governance.escalation import EscalationCase, EscalationPipeline
from src.governance.models import LEVEL_LABELS, RiskLevel, StudentRecord
from src.governance.retention import TrendPoint
from src.governance.rules import RuleEngine


@dataclass(frozen=True)
class VisibilityItem:
    data: str
    visible_to: str
    purpose: str


# 学生可见性总表：任何后台持有的数据类别都必须在这里登记可见方
VISIBILITY_TABLE: tuple[VisibilityItem, ...] = (
    VisibilityItem("我的逐句对话原文", "无人（不进入治理后台）", "只在陪伴端本地处理，后台从不保存"),
    VisibilityItem("脱敏后的风险信号", "当班心理老师 / 有资质人员（仅在命中风险规则时）",
                   "判断是否需要把人接进来"),
    VisibilityItem("风险等级与判断依据", "我本人、当班有资质人员", "让我知道为什么被关注，也可以提出异议"),
    VisibilityItem("给监护人的安全结论", "监护人（仅2级及以上）", "只告知结论与已采取的行动，不含对话"),
    VisibilityItem("去标识化趋势统计", "学校管理层（无法识别到我个人）", "了解全校整体趋势"),
    VisibilityItem("授权与撤回记录", "我本人、监护人、学校管理员", "证明授权状态与数据处理边界"),
)


class StudentView:
    def __init__(self, record: StudentRecord):
        self._record = record

    def profile(self) -> dict:
        return {
            "display_name": self._record.display_name,
            "age": self._record.age,
            "school_id": self._record.school_id,
            "consent_active": self._record.consent.active,
            "model_version": self._record.model_version,
        }

    def visibility(self) -> tuple[VisibilityItem, ...]:
        return VISIBILITY_TABLE


class GuardianView:
    def __init__(self, *, pipeline: EscalationPipeline):
        self._pipeline = pipeline

    def conclusions_for(self, student_id: str) -> list[dict]:
        """监护人能看到的全部内容：实际送达过的安全结论。

        边界：1级不通知、被人工否决的2级工单不通知、工单不存在则什么都看不到。
        监护人永远无法翻到任何聊天记录。
        """
        result = []
        for case in self._pipeline.cases:
            if case.student_id != student_id or not case.guardian_notified:
                continue
            conclusion = self._pipeline.safety_conclusion_for(case)
            result.append({
                "level": LEVEL_LABELS[conclusion.level],
                "concluded_at": conclusion.concluded_at.isoformat(),
                "headline": conclusion.headline,
                "action_taken": conclusion.action_taken,
            })
        return result


class ProfessionalView:
    def __init__(self, *, pipeline: EscalationPipeline, engine: RuleEngine):
        self._pipeline = pipeline
        self._engine = engine

    def case_review(self, case_id: str) -> dict:
        """专业人员审查一次升级：判断依据 + 是否及时 + 是否真实人际接力。"""
        case = self._pipeline.case(case_id)
        report = case.review_report()
        report["assessment_explanation"] = case.assessment.explain()
        report["relay_complete"] = case.human_contact is not None
        report["timely"] = case.sla_met()
        return report

    def open_cases(self) -> list[str]:
        """等待人工处理的工单（用于值守台）。"""
        return [c.case_id for c in self._pipeline.cases if not c.closed]

    def overdue_cases(self, *, now) -> list[str]:
        """超过时限仍未完成人际接力的工单。"""
        overdue = []
        for case in self._pipeline.cases:
            if not case.closed and case.human_contact is None and case.deadline < now:
                overdue.append(case.case_id)
        return overdue


class TrendView:
    """去标识化趋势视图：任何查看者都无法从中还原到具体学生。"""

    def __init__(self, points: tuple[TrendPoint, ...]):
        self._points = points

    def rows(self) -> tuple[dict, ...]:
        return tuple(
            {
                "bucket": p.bucket,
                "age_band": p.age_band,
                "level": LEVEL_LABELS[p.level],
                "count": p.count,
            }
            for p in self._points
        )
