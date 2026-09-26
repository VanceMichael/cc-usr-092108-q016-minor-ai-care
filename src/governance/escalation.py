"""分级升级与人工接力。

核心约束：

- 普通波动（0级）不进入升级管线，只沉淀趋势；
- 1级起逐级派单给有资质人员，每级有明确时限（SLA）；
- 一次升级只有在“真实的人接触到学生”后才算闭环（HumanContact），
  AI 聊天不算接力；
- 自动判断必须能被人否决：有资质人员可改写等级或关闭工单，必须给理由，
  否决不删除原评估，而是以批注形式留在时间线上；
- 通知时序保证人能否决先行：
    * 3级（迫在眉睫伤害）：紧急联络立即发出，生命安全优先于等待审核；
    * 2级（自伤暗示）：监护人安全结论在有资质人员“接单并确认依据”后才发出，
      专业人员可在接单前否决，否决则监护人什么都收不到；
- 紧急联络通过幂等通知器发送：消息重试、网络重发、后台重启都不会让同一条
  紧急联络发出第二次；
- 夜间命中的高等级信号路由到学区夜间值守，交接写入审计链。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional, Protocol

from src.governance.models import Assessment, HumanContact, RiskLevel, SafetyConclusion


class CaseStatus(str, Enum):
    OPEN = "open"                    # 已派单，待接单
    ACKED = "acked"                  # 人员已接单
    CONTACTED = "contacted"          # 已完成真实人际接触（闭环的必要条件）
    CLOSED = "closed"                # 正常关闭
    OVERRIDDEN = "overridden"        # 被人工否决/改写后关闭


# 各等级的响应时限：从评估时间起算
SLA_BY_LEVEL: dict[int, timedelta] = {
    int(RiskLevel.ISOLATION): timedelta(days=3),
    int(RiskLevel.SELF_HARM): timedelta(hours=24),
    int(RiskLevel.IMMINENT_HARM): timedelta(minutes=15),
}

# 需要通知监护人的最低等级
GUARDIAN_NOTIFY_LEVEL = int(RiskLevel.SELF_HARM)
# 需要走紧急联络通道（含夜间值守）的等级
EMERGENCY_LEVEL = int(RiskLevel.IMMINENT_HARM)


@dataclass(frozen=True)
class CaseEvent:
    at: datetime
    actor: str
    action: str
    note: str = ""


@dataclass(frozen=True)
class EscalationCase:
    case_id: str
    student_id: str
    assessment: Assessment
    status: CaseStatus
    opened_at: datetime
    deadline: datetime
    assignee_id: Optional[str] = None
    night_routed: bool = False
    human_contact: Optional[HumanContact] = None
    timeline: tuple[CaseEvent, ...] = ()
    override_reason: Optional[str] = None
    override_by: Optional[str] = None
    guardian_notified: bool = False   # 2级结论是否已向监护人发出

    def with_event(self, event: CaseEvent, **changes) -> "EscalationCase":
        return replace(self, timeline=self.timeline + (event,), **changes)

    @property
    def closed(self) -> bool:
        return self.status in (CaseStatus.CLOSED, CaseStatus.OVERRIDDEN)

    def sla_met(self) -> Optional[bool]:
        """闭环是否在时限内完成；未闭环返回 None。"""
        if self.human_contact is None:
            return None
        return self.human_contact.contacted_at <= self.deadline

    def review_report(self) -> dict:
        """供专业人员审查：这次升级是否及时完成了真实的人际接力。"""
        return {
            "case_id": self.case_id,
            "level": self.assessment.level,
            "opened_at": self.opened_at.isoformat(),
            "deadline": self.deadline.isoformat(),
            "status": self.status.value,
            "night_routed": self.night_routed,
            "guardian_notified": self.guardian_notified,
            "human_contact": (
                None if self.human_contact is None else {
                    "channel": self.human_contact.channel,
                    "staff_id": self.human_contact.staff_id,
                    "qualification": self.human_contact.staff_qualification,
                    "contacted_at": self.human_contact.contacted_at.isoformat(),
                }
            ),
            "sla_met": self.sla_met(),
            "override": None if self.override_reason is None else {
                "by": self.override_by,
                "reason": self.override_reason,
            },
            "timeline": [
                {"at": e.at.isoformat(), "actor": e.actor, "action": e.action, "note": e.note}
                for e in self.timeline
            ],
        }


class StaffDirectory(Protocol):
    """有资质人员名录（由学校侧维护，后台只读）。"""

    def qualified_staff(self, *, level: int, night: bool) -> list[str]: ...
    def qualification_of(self, staff_id: str) -> str: ...


class Notifier(Protocol):
    """通知通道。实现方必须保证：同一 dedupe_key 只真正送达一次。"""

    def send(self, *, dedupe_key: str, recipient: str, subject: str, body: str) -> bool: ...


class RecordingNotifier:
    """内存版通知器：记录每次真实发送，用于演示与测试幂等性。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._seen: set[str] = set()

    def send(self, *, dedupe_key: str, recipient: str, subject: str, body: str) -> bool:
        if dedupe_key in self._seen:
            return False
        self._seen.add(dedupe_key)
        self.sent.append({
            "dedupe_key": dedupe_key,
            "recipient": recipient,
            "subject": subject,
            "body": body,
        })
        return True


class EscalationPipeline:
    """升级管线：开单、派单、夜间路由、闭环、否决、通知。"""

    def __init__(
        self,
        *,
        staff: StaffDirectory,
        notifier: Notifier,
        now: Callable[[], datetime],
        guardian_contact: Callable[[str], str] = lambda sid: f"guardian-of-{sid}",
    ):
        self._staff = staff
        self._notifier = notifier
        self._now = now
        self._guardian_contact = guardian_contact
        self._cases: dict[str, EscalationCase] = {}
        self._seq = 0

    @property
    def cases(self) -> tuple[EscalationCase, ...]:
        return tuple(self._cases.values())

    def case(self, case_id: str) -> EscalationCase:
        return self._cases[case_id]

    # ---- 开单 ----

    def open_case(self, assessment: Assessment, *, night: bool) -> Optional[EscalationCase]:
        """按评估等级开单。0级不开单（返回 None）。"""
        level = assessment.level
        if level <= int(RiskLevel.ROUTINE):
            return None

        self._seq += 1
        case_id = f"case-{self._seq:06d}"
        sla = SLA_BY_LEVEL[level]
        opened_at = assessment.assessed_at
        deadline = opened_at + sla

        candidates = self._staff.qualified_staff(level=level, night=night)
        assignee = candidates[0] if candidates else None

        case = EscalationCase(
            case_id=case_id,
            student_id=assessment.student_id,
            assessment=assessment,
            status=CaseStatus.OPEN,
            opened_at=opened_at,
            deadline=deadline,
            assignee_id=assignee,
            night_routed=night,
            timeline=(CaseEvent(
                at=opened_at,
                actor="system",
                action="case.opened",
                note=f"等级{level}，时限至{deadline.isoformat()}"
                     + ("；夜间值守路由" if night else ""),
            ),),
        )
        self._cases[case_id] = case

        if level >= EMERGENCY_LEVEL:
            # 3级：结论与紧急联络立即发出，不等审核
            self._notify_guardian(case)
            self._emergency_contact(case)
        # 2级的监护人结论推迟到有资质人员接单审核（acknowledge）后发出，
        # 给人工否决留出窗口。
        return case

    # ---- 通知（幂等） ----

    def _notify_guardian(self, case: EscalationCase) -> bool:
        """监护人只收到最小化安全结论，永远收不到对话内容。返回是否真实发出。"""
        conclusion = self.safety_conclusion_for(case)
        body = f"{conclusion.headline}\n{conclusion.action_taken}"
        sent = self._notifier.send(
            dedupe_key=f"guardian:{case.case_id}",
            recipient=self._guardian_contact(case.student_id),
            subject="学生安全情况告知",
            body=body,
        )
        if sent:
            case = case.with_event(
                CaseEvent(at=self._now(), actor="system", action="guardian.notified",
                          note="已发送最小化安全结论（不含对话内容）"),
                guardian_notified=True,
            )
            self._cases[case.case_id] = case
        return sent

    def _emergency_contact(self, case: EscalationCase) -> bool:
        """紧急联络：dedupe_key 只含 case_id，重试/重放不会重复发出。"""
        sent = self._notifier.send(
            dedupe_key=f"emergency:{case.case_id}",
            recipient=self._guardian_contact(case.student_id),
            subject="紧急：请立即与孩子当面确认安全",
            body="系统发现迫在眉睫的安全风险，校内人员正在赶往，请监护人立即当面确认孩子安全。",
        )
        if sent:
            self._log(case.case_id, "system", "emergency.contacted", "紧急联络已发出（幂等）")
        return sent

    def retry_emergency_contact(self, case_id: str) -> bool:
        """供消息补偿/重试任务调用：返回是否真的又发出一条（幂等命中时为 False）。"""
        return self._emergency_contact(self._cases[case_id])

    def safety_conclusion_for(self, case: EscalationCase) -> SafetyConclusion:
        level = case.assessment.level
        if level >= int(RiskLevel.IMMINENT_HARM):
            headline = "孩子可能正面临紧迫危险，需要立即当面确认安全。"
            action = "校内人员已按紧急流程行动，请您马上联系孩子。"
        elif level >= int(RiskLevel.SELF_HARM):
            headline = "孩子近期流露出伤害自己的暗示，需要专业人员介入。"
            action = "有资质人员将在24小时内与孩子当面谈话，请保持电话畅通。"
        else:
            headline = "孩子近期情绪持续低落、社交退缩，学校已关注。"
            action = "心理老师将在三个工作日内安排谈心，暂不需要您采取特别行动。"
        return SafetyConclusion(
            student_id=case.student_id,
            level=level,
            concluded_at=self._now(),
            headline=headline,
            action_taken=action,
        )

    # ---- 人工操作 ----

    def acknowledge(self, case_id: str, *, staff_id: str, at: datetime) -> EscalationCase:
        """有资质人员接单并审核依据。

        2级工单在此时才向监护人发出最小化安全结论——人员已看过判断依据，
        若认为依据不足应先调用 override 否决，监护人即不会收到任何通知。
        """
        case = self._require_open(case_id)
        case = case.with_event(
            CaseEvent(at=at, actor=staff_id, action="case.acked"),
            status=CaseStatus.ACKED, assignee_id=staff_id,
        )
        self._cases[case_id] = case
        if case.assessment.level >= GUARDIAN_NOTIFY_LEVEL and not case.guardian_notified:
            self._notify_guardian(case)
        return case

    def record_human_contact(self, case_id: str, contact: HumanContact) -> EscalationCase:
        """登记真实人际接触——升级闭环的唯一有效方式。"""
        case = self._require_open(case_id)
        case = case.with_event(
            CaseEvent(at=contact.contacted_at, actor=contact.staff_id,
                      action="case.contacted",
                      note=f"{contact.channel}（{contact.staff_qualification}）"),
            status=CaseStatus.CONTACTED, human_contact=contact,
        )
        self._cases[case_id] = case
        return case

    def close(self, case_id: str, *, staff_id: str, at: datetime, note: str) -> EscalationCase:
        case = self._require_open(case_id)
        if case.human_contact is None:
            raise ValueError("未完成真实人际接触，不能关闭工单")
        case = case.with_event(
            CaseEvent(at=at, actor=staff_id, action="case.closed", note=note),
            status=CaseStatus.CLOSED,
        )
        self._cases[case_id] = case
        return case

    def override(self, case_id: str, *, staff_id: str, at: datetime,
                 reason: str, new_level: Optional[int] = None) -> EscalationCase:
        """人工否决/改写自动判断。

        - 必须给出理由；
        - 原评估保留在工单里，否决以批注形式进入时间线；
        - 若工单尚未发出监护人通知且被降级到通知线以下，不产生任何通知。
        """
        if not reason.strip():
            raise ValueError("人工否决必须说明理由")
        case = self._require_open(case_id)
        note = f"人工否决：{reason}"
        if new_level is not None:
            note += f"（等级改写为{new_level}）"
        case = case.with_event(
            CaseEvent(at=at, actor=staff_id, action="case.overridden", note=note),
            status=CaseStatus.OVERRIDDEN,
            override_reason=reason,
            override_by=staff_id,
        )
        self._cases[case_id] = case
        return case

    # ---- 内部 ----

    def _log(self, case_id: str, actor: str, action: str, note: str) -> None:
        case = self._cases[case_id]
        self._cases[case_id] = case.with_event(
            CaseEvent(at=self._now(), actor=actor, action=action, note=note)
        )

    def _require_open(self, case_id: str) -> EscalationCase:
        try:
            case = self._cases[case_id]
        except KeyError:
            raise KeyError(f"未知工单：{case_id}") from None
        if case.closed:
            raise ValueError(f"工单已关闭：{case_id}")
        return case
