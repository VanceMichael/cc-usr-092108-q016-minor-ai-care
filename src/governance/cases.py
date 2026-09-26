"""关怀案件：按等级把人接进来，并证明人际接力及时完成。

关键不变量：
- 升级（watch→urgent→emergency）开启新的“接力段”，只有升级之后
  由资质达标的人员完成面对面/语音真人接触，才算该段闭合；
- AI对话、文字留言不算真人接触；
- 每条对外联络（尤其家长紧急联络）在发件箱按键去重，
  消息重试不会重复发出；
- 摘要按受众最小化：家长只收到安全结论，不含任何会话内容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .clock import parse
from .rules import EMERGENCY, NORMAL, URGENT, WATCH, RANK, higher
from .schedule import DutySlot, next_workday_noon

# 各等级所需最低资质（自低向高）
REQUIRED_QUALIFICATION = {
    WATCH: "teacher",
    URGENT: "counselor",
    EMERGENCY: "psychologist",
}
QUAL_RANK = {"teacher": 1, "counselor": 2, "psychologist": 3}

# 各等级真人接触时限
SLA = {
    WATCH: "next-workday-noon",
    URGENT: 24 * 60,      # 分钟
    EMERGENCY: 30,
}

HUMAN_CHANNELS = {"in-person", "voice-call"}

OPEN = "open"
CONTACTED = "contacted"
CLOSED = "closed"


class NoDutyAvailable(RuntimeError):
    pass


class ContactRejected(RuntimeError):
    pass


def _deadline_for(level: str, at: str) -> str:
    if level == WATCH:
        return next_workday_noon(at)
    from datetime import timedelta
    return (parse(at) + timedelta(minutes=SLA[level])).isoformat(timespec="minutes")


def qualification_meets(held: str, required_level: str) -> bool:
    needed = REQUIRED_QUALIFICATION[required_level]
    return QUAL_RANK.get(held, 0) >= QUAL_RANK[needed]


@dataclass
class RelayLeg:
    """一次升级对应的人际接力段。"""
    level: str
    escalated_at: str
    deadline_at: str
    rule_pack_version: str = ""
    assignee: DutySlot | None = None
    assigned_at: str | None = None
    contact_at: str | None = None
    contact_by: str | None = None
    contact_qualification: str | None = None
    channel: str | None = None

    def close(self, at: str, staff_id: str, qualification: str, channel: str) -> None:
        self.contact_at = at
        self.contact_by = staff_id
        self.contact_qualification = qualification
        self.channel = channel

    @property
    def completed(self) -> bool:
        return self.contact_at is not None

    @property
    def within_sla(self) -> bool | None:
        if not self.completed:
            return None
        return parse(self.contact_at) <= parse(self.deadline_at)

    @property
    def overdue(self) -> bool:
        return not self.completed

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "escalated_at": self.escalated_at,
            "deadline_at": self.deadline_at,
            "rule_pack_version": self.rule_pack_version,
            "assigned_to": self.assignee.staff_id if self.assignee else None,
            "assigned_at": self.assigned_at,
            "contact_at": self.contact_at,
            "contact_by": self.contact_by,
            "contact_qualification": self.contact_qualification,
            "channel": self.channel,
            "completed": self.completed,
            "within_sla": self.within_sla,
        }


@dataclass
class HumanContact:
    at: str
    staff_id: str
    qualification: str
    channel: str
    note_minimal: str


@dataclass
class CareCase:
    case_id: str
    pseudo: str
    school_id: str
    opened_at: str
    level: str = WATCH          # 当前轮次等级
    max_level: str = WATCH      # 历史最高等级（审查用）
    rule_pack_version: str = ""
    legs: list[RelayLeg] = field(default_factory=list)
    contacts: list[HumanContact] = field(default_factory=list)
    status: str = OPEN
    closed_at: str | None = None
    close_reason: str | None = None
    override_seq: int | None = None

    def _open_leg(self, level: str, at: str, rule_pack_version: str) -> RelayLeg:
        return RelayLeg(
            level=level,
            escalated_at=at,
            deadline_at=_deadline_for(level, at),
            rule_pack_version=rule_pack_version,
        )

    def escalate(self, level: str, at: str, rule_pack_version: str) -> RelayLeg | None:
        """按信号登记新一轮人际接力。

        - 仍有未闭合接力段：只在等级严格升高时加段；
        - 上一轮已全部闭合：任何非普通等级都开启新一轮
          （即使低于历史最高，例如紧急解除后再次出现关注级信号）。
        """
        self.rule_pack_version = rule_pack_version
        self.max_level = higher(self.max_level, level)
        open_leg = self.current_leg()
        if open_leg is not None:
            if RANK[level] <= RANK[open_leg.level]:
                return None
            self.level = level
            leg = self._open_leg(level, at, rule_pack_version)
            self.legs.append(leg)
            return leg
        # 上一轮已真人闭合：新信号开启新一轮关怀（状态回到 open）
        self.status = OPEN
        self.level = level
        leg = self._open_leg(level, at, rule_pack_version)
        self.legs.append(leg)
        return leg

    def current_leg(self) -> RelayLeg | None:
        return next(
            (leg for leg in reversed(self.legs) if not leg.completed), None
        )

    def deescalate(self, level: str, at: str, rule_pack_version: str) -> RelayLeg | None:
        """人工复核后下调等级：仍为较低等级开一条新接力段。"""
        self.level = level
        if level == NORMAL:
            return None
        leg = RelayLeg(
            level=level,
            escalated_at=at,
            deadline_at=_deadline_for(level, at),
            rule_pack_version=rule_pack_version,
        )
        self.legs.append(leg)
        return leg

    def assign(self, slot: DutySlot, at: str) -> RelayLeg:
        if not qualification_meets(slot.qualification, self.level):
            raise ContactRejected(
                f"{slot.staff_id} 资质 {slot.qualification} 不足以承接 {self.level}"
            )
        leg = self.current_leg()
        if leg is None:
            raise ContactRejected("案件没有待接力的升级段")
        leg.assignee = slot
        leg.assigned_at = at
        return leg

    def record_contact(self, staff_id: str, qualification: str, channel: str,
                       at: str, note_minimal: str) -> HumanContact:
        if channel not in HUMAN_CHANNELS:
            raise ContactRejected("只有面对面或语音真人接触才算人际接力")
        if not qualification_meets(qualification, self.level):
            raise ContactRejected(
                f"资质 {qualification} 不足以闭合 {self.level} 等级接力"
            )
        open_legs = [leg for leg in self.legs if not leg.completed]
        closable = [
            leg for leg in open_legs
            if qualification_meets(qualification, leg.level)
        ]
        if not closable:
            raise ContactRejected("没有可由该资质闭合的接力段")
        contact = HumanContact(at, staff_id, qualification, channel, note_minimal)
        self.contacts.append(contact)
        # 一次真人接触同时闭合其资质覆盖的所有未闭合段；
        # 已过时限的段可补登，但 within_sla 会如实记为 False。
        for leg in closable:
            leg.close(at, staff_id, qualification, channel)
        self.status = CONTACTED
        return contact

    def overdue_at(self, at: str) -> RelayLeg | None:
        now = parse(at)
        for leg in self.legs:
            if not leg.completed and now > parse(leg.deadline_at):
                return leg
        return None

    def close(self, at: str, reason: str, override_seq: int | None = None) -> None:
        if any(not leg.completed for leg in self.legs):
            raise ContactRejected("仍有接力段未完成真人接触，不能关闭")
        self.status = CLOSED
        self.closed_at = at
        self.close_reason = reason
        self.override_seq = override_seq

    def review_relay(self) -> dict[str, Any]:
        """供专业人员审查：每一段升级是否及时完成真实人际接力。"""
        return {
            "case_id": self.case_id,
            "pseudo": self.pseudo,
            "current_level": self.level,
            "status": self.status,
            "rule_pack_version": self.rule_pack_version,
            "legs": [leg.as_dict() for leg in self.legs],
            "all_completed": all(leg.completed for leg in self.legs),
            "all_within_sla": all(
                leg.within_sla is True for leg in self.legs if leg.completed
            ) and all(leg.completed for leg in self.legs),
            "contacts": [
                {
                    "at": c.at, "by": c.staff_id,
                    "qualification": c.qualification,
                    "channel": c.channel, "note_minimal": c.note_minimal,
                }
                for c in self.contacts
            ],
        }


class CaseBook:
    def __init__(self) -> None:
        self._cases: dict[str, CareCase] = {}
        self._active: dict[str, str] = {}

    def open(self, case_id: str, pseudo: str, school_id: str, at: str,
             level: str, rule_pack_version: str) -> CareCase:
        case = CareCase(
            case_id=case_id, pseudo=pseudo, school_id=school_id,
            opened_at=at, level=level, rule_pack_version=rule_pack_version,
        )
        case.legs.append(RelayLeg(
            level=level,
            escalated_at=at,
            deadline_at=_deadline_for(level, at),
            rule_pack_version=rule_pack_version,
        ))
        self._cases[case_id] = case
        self._active[pseudo] = case_id
        return case

    def get(self, case_id: str) -> CareCase:
        return self._cases[case_id]

    def active_for(self, pseudo: str) -> CareCase | None:
        case_id = self._active.get(pseudo)
        if not case_id:
            return None
        case = self._cases[case_id]
        return None if case.status == CLOSED else case

    def all_open(self) -> list[CareCase]:
        return [c for c in self._cases.values() if c.status != CLOSED]


class Router:
    """按学校与时段查值班人员，并校验资质。"""

    def __init__(self, roster, idfn: Callable[[], str] | None = None):
        self.roster = roster
        self._idfn = idfn

    def route(self, school_id: str, level: str, at: str) -> DutySlot:
        slots = self.roster.slots_on_duty(school_id, at)
        if not slots:
            raise NoDutyAvailable(f"{school_id} 在 {at} 无值班人员")
        eligible = [
            slot for slot in slots
            if qualification_meets(slot.qualification, level)
        ]
        if not eligible:
            names = ", ".join(f"{s.staff_id}({s.qualification})" for s in slots)
            raise NoDutyAvailable(
                f"{school_id} 值班 {names} 均不满足 {level} 资质要求"
            )
        # 优先派给满足要求的最低资质人员，把高强度人力留给更紧急情况
        return min(eligible, key=lambda s: QUAL_RANK[s.qualification])


@dataclass(frozen=True)
class OutboxMessage:
    key: str
    channel: str
    recipient_ref: str
    content_minimal: str
    first_at: str
    sent_count: int
    attempts: int


class Outbox:
    """对外联络发件箱：同键只发一次，重试只增加投递尝试。"""

    def __init__(self) -> None:
        self._messages: dict[str, OutboxMessage] = {}

    def send(self, key: str, channel: str, recipient_ref: str,
             content_minimal: str, at: str) -> OutboxMessage:
        existing = self._messages.get(key)
        if existing is not None:
            return existing
        message = OutboxMessage(
            key=key, channel=channel, recipient_ref=recipient_ref,
            content_minimal=content_minimal, first_at=at,
            sent_count=1, attempts=1,
        )
        self._messages[key] = message
        return message

    def retry(self, key: str) -> OutboxMessage:
        existing = self._messages.get(key)
        if existing is None:
            raise KeyError(f"发件箱中没有 {key}，不能对未发出的联络做重试")
        # 投递层重试：只增加尝试计数，绝不产生第二条联络
        object.__setattr__(existing, "attempts", existing.attempts + 1)
        return existing

    def sent_count(self, key: str) -> int:
        return self._messages[key].sent_count

    def all_for(self, recipient_ref: str) -> list[OutboxMessage]:
        return [m for m in self._messages.values() if m.recipient_ref == recipient_ref]

    def export(self) -> list[dict[str, Any]]:
        return [m.__dict__ for m in self._messages.values()]


def render_digest(audience: str, case: CareCase, verdict_basis: list[str],
                  hints: list[str]) -> str:
    """按受众生成最小化文字摘要。

    guardian 只得到安全结论与行动说明；professional 才看得到规则依据。
    任何受众都拿不到会话原文或片段。
    """
    if audience == "guardian":
        if case.level == EMERGENCY:
            return (
                "【紧急安全提醒】我们注意到孩子此刻可能处于需要立即关心的状态，"
                "学校专职心理教师已在第一时间陪伴并采取保护措施，请您尽快与学校联系。"
            )
        if case.level == URGENT:
            return (
                "孩子近期情绪信号值得关注，学校心理老师已安排面对面谈话并会持续陪伴；"
                "建议您在家中给予温和关心，无需追问聊天内容。"
            )
        if case.level == WATCH:
            return (
                "学校在日常关怀中留意到孩子近期可能有些压力，老师会增加关心，"
                "也欢迎您在方便时陪孩子做些喜欢的事。"
            )
        return "孩子近期状态平稳，学校将继续常规关怀。"
    if audience == "professional":
        lines = [
            f"案件 {case.case_id}｜化名 {case.pseudo}｜等级 {case.level}",
            f"规则包 {case.rule_pack_version}｜立案 {case.opened_at}",
        ]
        lines.extend(f"依据：{b}" for b in verdict_basis)
        lines.extend(f"处置提示：{h}" for h in hints)
        for leg in case.legs:
            status = "未闭合"
            if leg.completed:
                status = f"已于 {leg.contact_at} 由 {leg.contact_by}（{leg.contact_qualification}）经{leg.channel}闭合，SLA内：{leg.within_sla}"
            lines.append(f"接力段 {leg.level}（截止 {leg.deadline_at}）：{status}")
        return "\n".join(lines)
    if audience == "staff":
        return f"关怀提醒：{case.pseudo} 当前等级 {case.level}，请按学校关怀流程跟进，注意保护学生隐私。"
    raise ValueError(f"未知受众 {audience}")
