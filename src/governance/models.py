"""治理后台共享数据类型。

设计原则：

- 原始对话文本不属于治理对象，后台只登记“信号”（脱敏后的短文本与结构化特征）。
- 所有实体都带不可变标识与时间戳，所有自动结论都可回溯到规则版本与命中依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum
from typing import Any, Optional


class RiskLevel(IntEnum):
    """风险等级，数字越大越紧急。"""

    ROUTINE = 0          # 普通波动：只沉淀去标识化趋势
    ISOLATION = 1        # 持续孤立 / 情感依赖：校内关怀人员关注
    SELF_HARM = 2        # 自伤暗示：有资质人员限时介入
    IMMINENT_HARM = 3    # 迫在眉睫的现实伤害：立即人工接力 + 必要时紧急联络


RISK_LEVELS = [int(level) for level in RiskLevel]

LEVEL_LABELS: dict[int, str] = {
    RiskLevel.ROUTINE: "普通波动",
    RiskLevel.ISOLATION: "持续孤立",
    RiskLevel.SELF_HARM: "自伤暗示",
    RiskLevel.IMMINENT_HARM: "迫在眉睫伤害",
}


@dataclass(frozen=True)
class GuardianConsent:
    """监护授权登记。

    scope 描述监护人被授权可见的范围；最小化原则下监护人永远拿不到逐句对话。
    """

    guardian_id: str
    student_id: str
    scope: tuple[str, ...]              # 如 ("safety_conclusion",)
    granted_at: datetime
    valid_from: date
    valid_until: Optional[date] = None  # None 表示长期有效，直到撤回
    revoked_at: Optional[datetime] = None
    revoke_reason: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def covers(self, item: str, on_date: date) -> bool:
        if not self.active:
            return False
        if on_date < self.valid_from:
            return False
        if self.valid_until is not None and on_date > self.valid_until:
            return False
        return item in self.scope


@dataclass(frozen=True)
class ServiceWindow:
    """服务时段与夜间值守安排。"""

    weekday_start_hour: int = 7     # 周一至周日的服务开始小时（本地时间，含）
    weekday_end_hour: int = 22      # 服务结束小时（不含）
    night_duty_school_id: Optional[str] = None  # 夜间值守承接校（学区联合值守）

    def is_night(self, moment: datetime) -> bool:
        hour = moment.hour
        if self.weekday_start_hour <= self.weekday_end_hour:
            return not (self.weekday_start_hour <= hour < self.weekday_end_hour)
        # 跨零点的时段，例如 19:00 至次日 06:00
        return hour >= self.weekday_start_hour or hour < self.weekday_end_hour


@dataclass(frozen=True)
class ModelVersion:
    """陪伴模型版本登记。"""

    model_version: str
    deployed_at: datetime
    retired_at: Optional[datetime] = None
    notes: str = ""

    @property
    def active(self) -> bool:
        return self.retired_at is None


@dataclass(frozen=True)
class StudentRecord:
    """学籍登记：年龄决定服务时段与规则适用。"""

    student_id: str
    school_id: str
    age: int
    display_name: str                 # 仅用于学生端自我确认，可随时改
    consent: GuardianConsent
    enrolled_at: datetime
    service_window: ServiceWindow
    model_version: str

    def __post_init__(self) -> None:
        if not (3 <= self.age <= 17):
            raise ValueError("仅登记未成年人（3-17岁）")


@dataclass(frozen=True)
class Signal:
    """进入治理后台的脱敏会话信号。

    text 必须已由 ``redaction`` 脱敏；raw_text 不进入后台，因此这里也不设该字段。
    features 是陪伴端提取的结构化特征，供规则引擎使用。
    """

    signal_id: str
    student_id: str
    observed_at: datetime
    text: str
    features: frozenset[str] = field(default_factory=frozenset)
    model_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "student_id": self.student_id,
            "observed_at": self.observed_at.isoformat(),
            "text": self.text,
            "features": sorted(self.features),
            "model_version": self.model_version,
        }


@dataclass(frozen=True)
class RuleHit:
    """单条规则命中依据。"""

    rule_id: str
    rule_version: str
    weight: int
    matched_terms: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Assessment:
    """一次自动判断结果：等级 + 完整依据。"""

    signal_id: str
    student_id: str
    level: int
    assessed_at: datetime
    rule_set_version: str
    hits: tuple[RuleHit, ...]
    rationale: str
    window_days: int

    def explain(self) -> str:
        """生成给人看的判断说明（自动判断必须说明依据）。"""
        lines = [
            f"等级：{LEVEL_LABELS.get(self.level, self.level)}",
            f"规则集版本：{self.rule_set_version}；观察窗口：{self.window_days}天",
            f"理由：{self.rationale}",
        ]
        if self.hits:
            lines.append("命中规则：")
            for hit in self.hits:
                terms = "、".join(hit.matched_terms) if hit.matched_terms else "结构化特征"
                lines.append(f"  - [{hit.rule_id}] {hit.reason}（依据：{terms}，权重{hit.weight}）")
        else:
            lines.append("命中规则：无（未达任何规则阈值）")
        return "\n".join(lines)


@dataclass(frozen=True)
class SafetyConclusion:
    """发给监护人的最小化安全结论（不含对话内容）。"""

    student_id: str
    level: int
    concluded_at: datetime
    headline: str           # 例如“孩子目前安全，校内有人关注”
    action_taken: str       # 例如“心理老师将在一个工作日内谈心”
    contact_word: str = ""  # 学生希望被如何称呼（来自学生本人，不是真名泄露）


@dataclass(frozen=True)
class HumanContact:
    """一次真实人际接触凭证：升级必须以“人接到人”闭环。"""

    channel: str            # face_to_face / voice_call，禁止 ai_chat
    staff_id: str
    staff_qualification: str
    contacted_at: datetime
    note: str               # 只记录接触事实，不记录学生倾诉内容

    def __post_init__(self) -> None:
        if self.channel == "ai_chat":
            raise ValueError("人际接力不能由AI聊天完成")


@dataclass(frozen=True)
class TrendPoint:
    """去标识化趋势点：不含任何学生标识与文本。"""

    bucket: str             # 例如 2026-W38（学年周）
    age_band: str           # 例如 13-15
    level: int
    count: int
