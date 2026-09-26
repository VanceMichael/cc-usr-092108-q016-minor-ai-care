"""编排引擎：脱敏信号入域 → 判定 → 分级把人接进来 → 留证。

这是领域内核的唯一用例入口；所有状态变更都写审计哈希链，
所有对外联络都走幂等发件箱。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import Any

from .audit import AuditLog
from .cases import (
    CaseBook, ContactRejected, NoDutyAvailable, Outbox, Router,
    render_digest,
)
from .clock import parse
from .consent import ConsentBook, GRANTED
from .identity import IdentityRegistry
from .referral import Referral, ReferralBook
from .retention import RetentionLedger
from .rules import EMERGENCY, NORMAL, RuleEngine, RulePack, RulePackBook, URGENT, WATCH
from .schedule import DutyRoster, is_night
from .signals import SignalHistory, TrendStore, redact_signal
from .triage import Verdict, normal_verdict
from .visibility import GUARDIAN, authorize, student_access_report


@dataclass
class IngestResult:
    accepted: bool
    reason: str
    signal_at: str
    level: str = NORMAL
    verdict: Verdict | None = None
    case_id: str | None = None
    night: bool = False
    actions: tuple[str, ...] = ()
    notices: tuple[dict[str, Any], ...] = ()


class GovernanceEngine:
    def __init__(
        self,
        audit: AuditLog,
        identities: IdentityRegistry,
        consents: ConsentBook,
        history: SignalHistory,
        trends: TrendStore,
        packs: RulePackBook,
        active_pack: str,
        roster: DutyRoster,
        cases: CaseBook,
        outbox: Outbox,
        referrals: ReferralBook,
        retention: RetentionLedger,
        models=None,
        schools=None,
        router: Router | None = None,
    ) -> None:
        self.audit = audit
        self.identities = identities
        self.consents = consents
        self.history = history
        self.trends = trends
        self.packs = packs
        self.active_pack = active_pack
        self.roster = roster
        self.cases = cases
        self.outbox = outbox
        self.referrals = referrals
        self.retention = retention
        self.models = models
        self.schools = schools
        self.router = router or Router(roster)
        self._engine = RuleEngine()
        self._seq = count(1)
        self._verdicts: dict[str, list[Verdict]] = {}
        self._night_review_queue: list[tuple[str, str]] = []

    # ---------- 规则版本管理 ----------

    def publish_pack(self, pack: RulePack, at: str, by: str) -> None:
        self.packs.publish(pack)
        self.audit.record(
            at, by, "rule-pack-published", pack.version,
            {"rule_codes": [r.code for r in pack.rules],
             "previous_version": self.active_pack},
        )
        self.active_pack = pack.version

    def retire_model(self, version: str, at: str, by: str) -> None:
        if self.models is None:
            raise RuntimeError("未接入模型登记册")
        self.models.retire(version, at)
        self.audit.record(at, by, "model-retired", version, {"retired_at": at})

    def register_model(self, version: str, extractor_version: str,
                       at: str, by: str = "ops-system") -> None:
        if self.models is None:
            raise RuntimeError("未接入模型登记册")
        self.models.register(version, extractor_version, at)
        self.audit.record(at, by, "model-registered", version,
                          {"extractor_version": extractor_version})

    # ---------- 信号入域 ----------

    def ingest(self, raw: dict[str, Any]) -> IngestResult:
        signal = redact_signal(raw)
        at = signal.at
        night = is_night(at)

        if not self.identities.is_registered(signal.pseudo):
            self.audit.record(at, "system", "ingest-rejected", signal.pseudo,
                              {"reason": "unregistered"})
            return IngestResult(False, "未登记的化名，拒绝入域", at, night=night)

        if self.consents.state_at(signal.pseudo, at) != GRANTED:
            self.audit.record(at, "system", "ingest-rejected", signal.pseudo,
                              {"reason": "consent-not-granted"})
            return IngestResult(False, "监护授权未授予或已撤回，拒绝入域", at, night=night)

        if self.models is not None and not self.models.is_allowed(signal.model, at):
            self.audit.record(at, "system", "ingest-rejected", signal.pseudo,
                              {"reason": "model-not-registered", "model": signal.model})
            return IngestResult(False, f"模型版本 {signal.model} 未登记或已停用，拒绝入域",
                                at, night=night)

        school_ok = True
        if self.schools is not None:
            ident0 = self.identities.get(signal.pseudo)
            school_ok = self.schools.has(ident0.school_id)
        if not school_ok:
            self.audit.record(at, "system", "ingest-rejected", signal.pseudo,
                              {"reason": "school-not-registered"})
            return IngestResult(False, "学生所在学校未登记服务，拒绝入域", at, night=night)

        unit_id = f"signal-{next(self._seq)}"
        self.history.add(signal, unit_id)
        self.retention.register(
            unit_id, "signal", signal.pseudo, at,
            snapshot=lambda uid=unit_id: self.history.snapshot_unit(uid),
            erase=lambda uid=unit_id: self.history.erase_unit(uid),
        )
        # 普通波动：进入去标识化趋势
        ident = self.identities.get(signal.pseudo)
        week = f"{parse(at).isocalendar().year}-W{parse(at).isocalendar().week:02d}"
        for metric in ("mood_score", "isolation_score", "sleep_disrupt"):
            if metric in signal.features:
                self.trends.add(signal.pseudo, ident.grade, week, metric, signal.features[metric])

        pack = self.packs.get(self.active_pack)
        level, hits = self._engine.evaluate(pack, self.history.for_(signal.pseudo))
        if level == NORMAL:
            verdict = normal_verdict(signal.pseudo, pack.version, at)
        else:
            verdict = Verdict(
                pseudo=signal.pseudo, level=level,
                rule_pack_version=pack.version,
                hits=tuple(hits), decided_at=at,
            )
        self._verdicts.setdefault(signal.pseudo, []).append(verdict)
        self.audit.record(
            at, "system", "auto-verdict", signal.pseudo,
            {"level": level, "rule_pack": pack.version,
             "matched_rules": [h["rule"] for h in hits],
             "model": signal.model, "night": night,
             "window": signal.window},
        )

        if level == NORMAL:
            return IngestResult(True, "普通波动，仅沉淀去标识化趋势", at,
                                NORMAL, verdict, night=night,
                                actions=("trend-only",))

        actions: list[str] = []
        notices: list[dict[str, Any]] = []
        case = self.cases.active_for(signal.pseudo)
        new_leg = None
        if case is None:
            case_id = f"case-{next(self._seq)}"
            case = self.cases.open(
                case_id, signal.pseudo, ident.school_id, at, level, pack.version,
            )
            new_leg = case.legs[-1]
            self.audit.record(at, "system", "case-opened", case_id,
                              {"pseudo": signal.pseudo, "level": level})
        else:
            case_id = case.case_id
            new_leg = case.escalate(level, at, pack.version)
            if new_leg is not None:
                self.audit.record(at, "system", "escalation", case_id,
                                  {"to_level": level, "deadline_at": new_leg.deadline_at,
                                   "rule_pack": pack.version})

        if new_leg is None:
            # 同一轮接力仍在进行：不重复派单、不重复通知，只更新判定证据
            return IngestResult(True, "关怀进行中，沿用当前接力", at, level, verdict,
                                case_id, night, actions=("relay-in-progress",))

        actions.append(self._dispatch(case, level, at))
        key = self._guardian_notice(case, level, at)
        if key:
            notices.append({"recipient": "guardian", "level": level, "key": key})
            actions.append("guardian-notified")

        return IngestResult(True, "已按等级升级人工关怀", at, level, verdict,
                            case_id, night, tuple(actions), tuple(notices))

    # ---------- 派单与家长通知（自动/人工共用） ----------

    def _dispatch(self, case, level: str, at: str, source: str = "auto") -> str:
        """为最新接力段派单；夜间 watch 排队，其余按资质路由。"""
        if is_night(at) and level == WATCH:
            self._night_review_queue.append((case.pseudo, at))
            self.audit.record(at, "system", "night-review-queued", case.case_id,
                              {"level": level})
            return "queued-for-next-day-review"
        try:
            slot = self.router.route(case.school_id, level, at)
            case.assign(slot, at)
            if slot.can_unmask:
                self.identities.unmask(
                    case.pseudo, slot.staff_id, slot.qualification,
                    purpose=f"level-{level} 现实关怀", granted_at=at,
                    audit_seq=self.audit.entries[-1].seq,
                )
            self.audit.record(at, slot.staff_id, "staff-assigned", case.case_id,
                              {"level": level, "can_unmask": slot.can_unmask,
                               "qualification": slot.qualification,
                               "trigger": source})
            return "staff-assigned"
        except NoDutyAvailable as exc:
            self.audit.record(at, "system", "no-duty-available", case.case_id,
                              {"level": level, "detail": str(exc), "trigger": source})
            return "escalation-unassigned"

    def _guardian_notice(self, case, level: str, at: str) -> str | None:
        if level not in (URGENT, EMERGENCY):
            return None
        identity = self.identities.get(case.pseudo)
        leg_index = len(case.legs)
        key = f"guardian:{case.case_id}:leg{leg_index}:{level}"
        content = render_digest(GUARDIAN, case, [], [])
        msg = self.outbox.send(key, "guardian-portal", identity.guardian_id, content, at)
        self.audit.record(at, "system", "guardian-notified", case.case_id,
                          {"level": level, "dedup_key": key,
                           "sent_count": msg.sent_count})
        return key

    def retry_notice(self, key: str) -> int:
        """投递层重试：绝不产生第二条联络，返回实际发出条数（恒为已发数）。"""
        msg = self.outbox.retry(key)
        return msg.sent_count

    # ---------- 人工否决 ----------

    def override_verdict(self, pseudo: str, staff_id: str, role: str,
                         new_level: str, reason: str, at: str) -> Verdict:
        pack = self.packs.get(self.active_pack)
        case = self.cases.active_for(pseudo)
        prior = (self._verdicts.get(pseudo) or [None])[-1]
        verdict = Verdict(
            pseudo=pseudo, level=new_level, rule_pack_version=pack.version,
            hits=prior.hits if prior else (), decided_at=at, source="manual-override",
            override_reason=reason, staff_id=staff_id,
        )
        self._verdicts.setdefault(pseudo, []).append(verdict)

        new_leg = False
        if case is not None and new_level != case.level:
            from .rules import RANK
            if RANK[new_level] < RANK[case.level]:
                # 下调等级前，当前等级的接力段必须已真人闭合
                if any(not leg.completed for leg in case.legs if leg.level == case.level):
                    raise ContactRejected(
                        "当前等级尚未被真人接触，不能下调等级；请先完成现实关怀核实"
                    )
                leg = case.deescalate(new_level, at, pack.version)
                new_leg = leg is not None
                self.audit.record(at, staff_id, "manual-deescalation", case.case_id,
                                  {"to_level": new_level, "reason": reason,
                                   "deadline_at": leg.deadline_at if leg else None})
            else:
                leg = case.escalate(new_level, at, pack.version)
                new_leg = leg is not None
                self.audit.record(at, staff_id, "manual-escalation", case.case_id,
                                  {"to_level": new_level,
                                   "deadline_at": leg.deadline_at if leg else None})
        elif case is None and new_level != NORMAL:
            # 人工发现机器未识别的风险：直接立案并开接力段
            ident = self.identities.get(pseudo)
            case = self.cases.open(
                f"case-{next(self._seq)}", pseudo, ident.school_id, at,
                new_level, pack.version,
            )
            new_leg = True
            self.audit.record(at, staff_id, "case-opened-manual", case.case_id,
                              {"level": new_level, "reason": reason})

        if new_leg and case is not None:
            self._dispatch(case, new_level, at, source="manual")
            self._guardian_notice(case, new_level, at)

        self.audit.record(at, staff_id, "verdict-override", pseudo,
                          {"new_level": new_level, "reason": reason,
                           "role": role, "rule_pack": pack.version})
        return verdict

    # ---------- 真人接触闭合接力 ----------

    def record_human_contact(self, case_id: str, staff_id: str, qualification: str,
                             channel: str, at: str, note_minimal: str) -> dict[str, Any]:
        case = self.cases.get(case_id)
        contact = case.record_contact(staff_id, qualification, channel, at, note_minimal)
        self.audit.record(at, staff_id, "human-contact", case_id,
                          {"level": case.level, "channel": channel,
                           "qualification": qualification,
                           "note_minimal": note_minimal})
        return case.review_relay()

    def close_case(self, case_id: str, staff_id: str, at: str, reason: str) -> None:
        case = self.cases.get(case_id)
        case.close(at, reason)
        self.audit.record(at, staff_id, "case-closed", case_id, {"reason": reason})

    def review_case(self, case_id: str, staff_id: str, role: str, at: str) -> dict[str, Any]:
        if not authorize(role, "professional_digest"):
            raise PermissionError(f"{role} 无权审查专业接力记录")
        case = self.cases.get(case_id)
        basis: list[str] = []
        for verdict in self._verdicts.get(case.pseudo, []):
            basis.extend(verdict.basis)
        digest_text = render_digest("professional", case, basis,
                                     [h["human_hint"] for v in self._verdicts.get(case.pseudo, [])
                                      for h in v.hits])
        self.audit.record(at, staff_id, "case-reviewed", case_id, {"role": role})
        review = case.review_relay()
        review["digest"] = digest_text
        return review

    def verdicts_for(self, pseudo: str) -> list[dict[str, Any]]:
        return [
            {
                "decided_at": v.decided_at,
                "level": v.level,
                "source": v.source,
                "rule_pack_version": v.rule_pack_version,
                "basis": v.basis,
                "override_reason": v.override_reason,
                "staff_id": v.staff_id,
            }
            for v in self._verdicts.get(pseudo, [])
        ]

    # ---------- 授权撤回 ----------

    def withdraw_consent(self, pseudo: str, guardian_id: str, at: str, reason: str) -> None:
        self.consents.withdraw(pseudo, guardian_id, at, reason)
        self.audit.record(at, guardian_id, "consent-withdrawn", pseudo,
                          {"reason": reason, "open_carried_on": bool(self.cases.active_for(pseudo))})

    # ---------- 跨校转介 ----------

    def make_referral(self, referral_id: str, pseudo: str, to_school_id: str,
                      staff_id: str, at: str) -> Referral:
        case = self.cases.active_for(pseudo)
        if case is None:
            raise RuntimeError("没有进行中的案件，无需转介")
        if any(not leg.completed for leg in case.legs):
            raise ContactRejected(
                "仍有未完成真人接触的接力段：先在本校稳住学生，再发起转介"
            )
        guardian_view = render_digest(GUARDIAN, case, [], [])
        referral = Referral(
            referral_id=referral_id, pseudo=pseudo,
            from_school_id=case.school_id, to_school_id=to_school_id,
            requested_at=at, open_level=case.level,
            rule_pack_version=case.rule_pack_version,
            safety_summary=guardian_view,
            consent_reconfirmed=False,
        )
        self.referrals.request(referral)
        self.audit.record(at, staff_id, "referral-requested", referral_id,
                          {"pseudo": pseudo, "to_school": to_school_id,
                           "open_level": case.level})
        return referral

    def accept_referral(self, referral_id: str, new_guardian_id: str, at: str) -> Referral:
        record = self.referrals.get(referral_id)
        self.consents.grant_transfer(record.pseudo, new_guardian_id, at, referral_id)
        self.referrals.mark_consent_reconfirmed(referral_id)
        accepted = self.referrals.accept(referral_id, at)
        self.audit.record(at, "system", "referral-accepted", referral_id,
                          {"pseudo": record.pseudo, "guardian_id": new_guardian_id})
        return accepted

    def complete_referral(self, referral_id: str, staff_id: str, at: str) -> Referral:
        record = self.referrals.get(referral_id)
        old_case = self.cases.active_for(record.pseudo)
        new_case_id = f"case-{next(self._seq)}"
        if old_case is not None and all(leg.completed for leg in old_case.legs):
            old_case.close(at, f"跨校转介 {referral_id}")
        self.identities.change_school(record.pseudo, record.to_school_id)
        new_case = self.cases.open(new_case_id, record.pseudo, record.to_school_id, at,
                                   record.open_level, record.rule_pack_version)
        # 转介完成即在新校按等级尝试值守路由，保证关怀不断档
        try:
            slot = self.router.route(record.to_school_id, record.open_level, at)
            new_case.assign(slot, at)
            self.audit.record(at, staff_id, "transfer-assigned", new_case_id,
                              {"level": record.open_level, "staff": slot.staff_id,
                               "qualification": slot.qualification})
        except NoDutyAvailable as exc:
            self.audit.record(at, staff_id, "transfer-unassigned", new_case_id,
                              {"level": record.open_level, "detail": str(exc)})
        completed = self.referrals.complete(referral_id, at, new_case_id)
        self.audit.record(at, staff_id, "referral-completed", referral_id,
                          {"new_case_id": new_case_id})
        return completed

    # ---------- 夜间值守 ----------

    def night_queue(self) -> list[tuple[str, str]]:
        return list(self._night_review_queue)

    def process_night_reviews(self, at: str, by: str = "duty-counselor") -> list[str]:
        """次日值守：夜间排队的 watch 信号统一在早晨复核并分配。"""
        processed: list[str] = []
        remaining: list[tuple[str, str]] = []
        for pseudo, queued_at in self._night_review_queue:
            case = self.cases.active_for(pseudo)
            if case is None or case.status == "closed":
                continue
            try:
                slot = self.router.route(case.school_id, case.level, at)
                case.assign(slot, at)
                self.audit.record(at, by, "night-review-routed", case.case_id,
                                  {"queued_at": queued_at, "level": case.level})
                processed.append(pseudo)
            except NoDutyAvailable:
                remaining.append((pseudo, queued_at))
        self._night_review_queue[:] = remaining
        return processed

    def sweep_retention(self, at: str, by: str = "system") -> list[dict[str, Any]]:
        certs = self.retention.purge_due(at)
        for cert in certs:
            self.audit.record(at, by, "data-purged", cert.unit_id,
                              {"kind": cert.kind, "pseudo": cert.pseudo,
                               "expires_at": cert.expires_at,
                               "digest_before_purge": cert.digest_before_purge,
                               "erased": cert.erased})
        return [cert.as_dict() for cert in certs]

    # ---------- 面向三类受众的视图 ----------

    def student_visibility(self) -> list[dict[str, Any]]:
        return [line.__dict__ for line in student_access_report()]

    def guardian_view(self, guardian_id: str, at: str) -> list[dict[str, Any]]:
        children = set(self.identities.lookup(guardian_id))
        out: list[dict[str, Any]] = []
        for msg in self.outbox.all_for(guardian_id):
            # 键格式 guardian:{case_id}:{level}，据此反查归属孩子
            parts = msg.key.split(":")
            pseudo = None
            if len(parts) >= 2:
                case = self.cases.get(parts[1])
                pseudo = case.pseudo
            if pseudo is not None and pseudo not in children:
                continue
            out.append({
                "pseudo": pseudo,
                "at": msg.first_at,
                "safety_conclusion": msg.content_minimal,
                "attempts": msg.attempts,
                "delivered_once": msg.sent_count == 1,
            })
        return out

    def verify_evidence(self) -> None:
        self.audit.verify()
