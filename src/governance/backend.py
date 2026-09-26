"""治理后台门面：把脱敏摄入、登记、规则判断、人工接力、留存与证明串成一条线。

一次信号处理的完整路径：

    原文（陪伴端边界，立即丢弃）
      → 脱敏信号 Signal
      → 校验监护授权与服务时段
      → 规则引擎评估 Assessment（带依据）
      ├─ 0级：只沉淀去标识化趋势，不留个人明细
      └─ 1级+：开工单、按SLA派单、夜间路由；
               2级+ 向监护人发最小化安全结论；
               3级 发幂等紧急联络
      → 全部关键动作写哈希链审计

可核验证明（证书）覆盖：授权撤回、跨校转介、规则换版、夜间值守交接、到期删除。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from src.governance.audit import AuditLog, Certificate, verify_chain
from src.governance.escalation import (
    EscalationCase,
    EscalationPipeline,
    Notifier,
    RecordingNotifier,
    StaffDirectory,
)
from src.governance.models import Assessment, RiskLevel, Signal, StudentRecord
from src.governance.redaction import make_signal, redact_text
from src.governance.registry import Registry
from src.governance.retention import RetentionStore, TrendStore
from src.governance.rules import RuleEngine, RuleSet


class GovernanceBackend:
    def __init__(
        self,
        *,
        school_id: str,
        staff: StaffDirectory,
        clock: Callable[[], datetime],
        notifier: Optional[Notifier] = None,
        night_duty_school_id: Optional[str] = None,
        guardian_contact: Optional[Callable[[str], str]] = None,
    ):
        self.clock = clock
        self.registry = Registry(school_id=school_id, night_duty_school_id=night_duty_school_id)
        self.audit = AuditLog()
        self.trends = TrendStore()
        self.retention = RetentionStore()
        notifier = notifier or RecordingNotifier()
        kwargs = {"staff": staff, "notifier": notifier, "now": clock}
        if guardian_contact is not None:
            kwargs["guardian_contact"] = guardian_contact
        self.pipeline = EscalationPipeline(**kwargs)
        self._engine: Optional[RuleEngine] = None
        self._referrals: list[dict] = []

    # ---- 配置：模型与规则 ----

    def register_model(self, version) -> None:
        self.registry.register_model(version)

    def load_rules(self, rule_set: RuleSet, *, actor: str = "admin") -> Certificate:
        """首次装载或换版：旧版本评估记录保留其原版本号，换版动作写审计并签证书。"""
        previous = self._engine.version if self._engine else None
        self._engine = RuleEngine(rule_set)
        at = self.clock()
        detail = {
            "new_version": rule_set.version,
            "previous_version": previous,
            "supersedes": rule_set.supersedes,
            "window_days": rule_set.window_days,
            "rule_count": len(rule_set.rules),
        }
        self.audit.append(
            kind="rules.switched", at=at, actor=actor,
            subject=f"ruleset:{rule_set.version}", detail=detail,
        )
        return self.audit.issue_certificate(kind="rules.switched", issued_at=at, payload=detail)

    @property
    def engine(self) -> RuleEngine:
        if self._engine is None:
            raise RuntimeError("尚未装载风险规则集")
        return self._engine

    def enroll(self, record: StudentRecord) -> None:
        self.registry.enroll(record)
        self.audit.append(
            kind="student.enrolled", at=self.clock(), actor="admin",
            subject=record.student_id,
            detail={"school_id": record.school_id, "age": record.age,
                    "consent_scope": list(record.consent.scope),
                    "model_version": record.model_version},
        )

    # ---- 信号摄入主路径 ----

    def ingest(
        self,
        *,
        student_id: str,
        raw_text: str,
        features: frozenset[str],
        signal_id: str,
        observed_at: Optional[datetime] = None,
    ) -> tuple[Signal, Assessment, Optional[EscalationCase]]:
        """处理一条学生消息。原文只在本方法内存在，返回的 Signal 已脱敏。"""
        at = observed_at or self.clock()
        record = self.registry.student(student_id)

        if not self.registry.consent_active(student_id, at.date()):
            # 无有效授权：信号不进入任何处理，只留审计痕迹证明“没有处理”
            self.audit.append(
                kind="signal.blocked", at=at, actor="system", subject=student_id,
                detail={"signal_id": signal_id, "reason": "监护授权缺失或已撤回"},
            )
            raise PermissionError("监护授权无效，拒绝处理该学生信号")

        signal = make_signal(
            signal_id=signal_id,
            student_id=student_id,
            observed_at=at,
            raw_text=raw_text,
            features=features,
            model_version=record.model_version,
        )
        # raw_text 在此随栈帧释放；后台任何存储中都不存在原文。
        del raw_text

        night = self.registry.is_night(student_id, at)
        history = self.retention.history_for(
            student_id, before=at, days=self.engine.rule_set.window_days
        )
        assessment = self.engine.assess(signal, history=history, at=at)

        self.audit.append(
            kind="signal.assessed", at=at, actor="rules-engine", subject=student_id,
            detail={
                "signal_id": signal_id,
                "level": assessment.level,
                "rule_set_version": assessment.rule_set_version,
                "hit_rules": [h.rule_id for h in assessment.hits],
                "redacted_text": signal.text,
                "night": night,
            },
        )

        case: Optional[EscalationCase] = None
        if assessment.level == int(RiskLevel.ROUTINE):
            # 普通波动：只留去标识化趋势，信号明细不落盘
            self.trends.absorb(moment=at, age=record.age, level=assessment.level)
            self.audit.append(
                kind="trend.absorbed", at=at, actor="system", subject=student_id,
                detail={"signal_id": signal_id, "kept": "deidentified_trend_only"},
            )
        else:
            self.retention.keep_signal(signal, stored_at=at)
            case = self.pipeline.open_case(assessment, night=night)
            if night and case is not None:
                self._night_handoff(case)
        return signal, assessment, case

    def _night_handoff(self, case: EscalationCase) -> Certificate:
        """夜间命中：交接给学区夜间值守校并签发交接证明。"""
        at = self.clock()
        detail = {
            "case_id": case.case_id,
            "student_school": self.registry.school_id,
            "duty_school": self.registry.night_duty_school_id,
            "level": case.assessment.level,
            "assignee": case.assignee_id,
        }
        self.audit.append(
            kind="night.handoff", at=at, actor="system",
            subject=case.case_id, detail=detail,
        )
        return self.audit.issue_certificate(kind="night.handoff", issued_at=at, payload=detail)

    # ---- 授权生命周期 ----

    def revoke_consent(self, student_id: str, *, reason: str) -> Certificate:
        """撤回授权：登记撤回、立即删除留存信号、签发可核验证明。"""
        at = self.clock()
        self.registry.revoke_consent(student_id, at=at, reason=reason)
        deletion = self.retention.purge_student(student_id, at=at, reason=reason)
        detail = {
            "student_id": student_id,
            "revoked_at": at.isoformat(),
            "reason": reason,
            "signals_deleted": deletion.count,
            "effect": "该学生信号即刻停止处理，留存明细已删除",
        }
        self.audit.append(
            kind="consent.revoked", at=at, actor="guardian",
            subject=student_id, detail=detail,
        )
        return self.audit.issue_certificate(kind="consent.revoked", issued_at=at, payload=detail)

    # ---- 跨校转介 ----

    def refer_student(self, student_id: str, *, to_school_id: str, reason: str) -> Certificate:
        """跨校转介：只转最小化安全信息，不转任何对话内容；全过程可核验。

        转介包包含：当前授权状态、未闭环工单及其等级与判断依据、模型/规则版本。
        接收校凭工单号继续人工接力，避免学生重复讲述创伤经历。
        """
        at = self.clock()
        record = self.registry.student(student_id)
        open_cases = [
            {
                "case_id": c.case_id,
                "level": c.assessment.level,
                "rule_set_version": c.assessment.rule_set_version,
                "hit_rules": [h.rule_id for h in c.assessment.hits],
                "deadline": c.deadline.isoformat(),
                "relay_complete": c.human_contact is not None,
            }
            for c in self.pipeline.cases
            if c.student_id == student_id and not c.closed
        ]
        detail = {
            "student_id": student_id,
            "from_school": record.school_id,
            "to_school": to_school_id,
            "reason": reason,
            "at": at.isoformat(),
            "consent_active": record.consent.active,
            "open_cases": open_cases,
            "transferred_content": "仅安全结论与工单状态；无任何对话原文或脱敏文本",
        }
        self._referrals.append(detail)
        self.audit.append(
            kind="student.referred", at=at, actor="admin",
            subject=student_id, detail=detail,
        )
        return self.audit.issue_certificate(kind="student.referred", issued_at=at, payload=detail)

    @property
    def referrals(self) -> tuple[dict, ...]:
        return tuple(self._referrals)

    # ---- 到期删除 ----

    def run_retention(self, *, today) -> list[Certificate]:
        """执行到期删除，每条删除记录写审计并签证书。"""
        records = self.retention.purge_expired(today=today)
        certs: list[Certificate] = []
        for rec in records:
            detail = {
                "category": rec.category,
                "subject": rec.subject,
                "deleted_at": rec.deleted_at.isoformat(),
                "policy": rec.policy,
                "count": rec.count,
            }
            self.audit.append(
                kind="data.expired", at=rec.deleted_at, actor="retention-job",
                subject=rec.subject, detail=detail,
            )
            certs.append(self.audit.issue_certificate(
                kind="data.expired", issued_at=rec.deleted_at, payload=detail))
        return certs

    # ---- 人工接力操作（转写审计） ----

    def acknowledge(self, case_id: str, *, staff_id: str) -> EscalationCase:
        case = self.pipeline.acknowledge(case_id, staff_id=staff_id, at=self.clock())
        self.audit.append(
            kind="case.acked", at=self.clock(), actor=staff_id,
            subject=case_id, detail={"student_id": case.student_id},
        )
        return case

    def record_human_contact(self, case_id: str, contact) -> EscalationCase:
        case = self.pipeline.record_human_contact(case_id, contact)
        self.audit.append(
            kind="case.contacted", at=contact.contacted_at, actor=contact.staff_id,
            subject=case_id,
            detail={"channel": contact.channel, "qualification": contact.staff_qualification},
        )
        return case

    def close_case(self, case_id: str, *, staff_id: str, note: str) -> EscalationCase:
        case = self.pipeline.close(case_id, staff_id=staff_id, at=self.clock(), note=note)
        self.retention.keep_case(case_id, closed_at=self.clock())
        self.audit.append(
            kind="case.closed", at=self.clock(), actor=staff_id,
            subject=case_id, detail={"note": note, "sla_met": case.sla_met()},
        )
        return case

    def override_case(self, case_id: str, *, staff_id: str, reason: str,
                      new_level: Optional[int] = None) -> EscalationCase:
        case = self.pipeline.override(
            case_id, staff_id=staff_id, at=self.clock(),
            reason=reason, new_level=new_level,
        )
        self.audit.append(
            kind="case.overridden", at=self.clock(), actor=staff_id,
            subject=case_id, detail={"reason": reason, "new_level": new_level},
        )
        return case

    # ---- 证明校验 ----

    def verify_proofs(self) -> bool:
        """校验整条审计链与全部证书。"""
        chain_ok, _ = verify_chain(self.audit.events)
        if not chain_ok:
            return False
        for cert in self.audit.certificates:
            if not cert.verify(self.audit.events):
                return False
        return True
