"""治理后台测试：覆盖需求中每一条承诺。

运行：python -m unittest discover -s tests -v
"""

import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from src.governance import GovernanceBackend
from src.governance.audit import AuditLog, verify_chain
from src.governance.escalation import RecordingNotifier
from src.governance.models import (
    GuardianConsent,
    HumanContact,
    ModelVersion,
    ServiceWindow,
    StudentRecord,
)
from src.governance.redaction import make_signal, pseudonym_for, redact_text
from src.governance.retention import RetentionPolicy, RetentionStore, TrendStore
from src.governance.rules import RuleSet
from src.governance.views import GuardianView, ProfessionalView, StudentView, TrendView

RULES_V1 = RuleSet.load(Path("fixtures/rules_v1.json"))
RULES_V2 = RuleSet.load(Path("fixtures/rules_v2.json"))

BASE = datetime(2026, 9, 21, 20, 0, 0)


class FakeStaff:
    def qualified_staff(self, *, level, night):
        if night:
            return ["staff-night-li"]
        if level >= 3:
            return ["staff-crisis-wang"]
        if level == 2:
            return ["staff-counselor-zhao"]
        return ["staff-careteacher-chen"]

    def qualification_of(self, staff_id):
        return {
            "staff-night-li": "夜间值守持证心理教师",
            "staff-crisis-wang": "危机干预资质社工",
            "staff-counselor-zhao": "中小学心理教师（中级）",
            "staff-careteacher-chen": "德育关怀教师",
        }[staff_id]


def build_backend():
    clock = {"now": BASE}
    notifier = RecordingNotifier()
    backend = GovernanceBackend(
        school_id="school-fuxing-01",
        staff=FakeStaff(),
        clock=lambda: clock["now"],
        notifier=notifier,
        night_duty_school_id="school-district-night-duty",
    )
    backend.register_model(ModelVersion(
        model_version="companion-q3-2026", deployed_at=datetime(2026, 9, 1)))
    backend.load_rules(RULES_V1)
    consent = GuardianConsent(
        guardian_id="guardian-li", student_id="stu-0117",
        scope=("safety_conclusion",),
        granted_at=BASE - timedelta(days=30), valid_from=date(2026, 8, 20))
    student = StudentRecord(
        student_id="stu-0117", school_id="school-fuxing-01", age=14,
        display_name="小禾", consent=consent, enrolled_at=BASE - timedelta(days=20),
        service_window=ServiceWindow(7, 22), model_version="companion-q3-2026")
    backend.enroll(student)
    return backend, clock, notifier, student


class RedactionTest(unittest.TestCase):
    def test_direct_identifiers_removed(self):
        text = "我叫欧阳晓晓，电话13800138000，身份证110101201001011234，邮箱x@y.com"
        out = redact_text(text)
        for secret in ("欧阳晓晓", "13800138000", "110101201001011234", "x@y.com"):
            self.assertNotIn(secret, out)
        self.assertIn("［已隐去］", out)

    def test_signal_carries_no_raw_field(self):
        signal = make_signal(
            signal_id="s1", student_id="stu", observed_at=BASE,
            raw_text="我的QQ号123456789", features=frozenset(), model_version="m1")
        self.assertNotIn("123456789", signal.text)
        self.assertFalse(hasattr(signal, "raw_text"))

    def test_pseudonym_is_stable_but_opaque(self):
        a = pseudonym_for("stu-0117", "salt-a")
        b = pseudonym_for("stu-0117", "salt-a")
        c = pseudonym_for("stu-0117", "salt-b")
        self.assertEqual(a, b)          # 校内稳定
        self.assertNotEqual(a, c)       # 换盐不可关联
        self.assertNotIn("0117", a)


class RulesTest(unittest.TestCase):
    def _assess(self, text, features, rules=RULES_V1, history=()):
        backend, *_ = build_backend()
        if rules is not RULES_V1:
            backend.load_rules(rules)
        signal = make_signal(
            signal_id="s", student_id="stu-0117", observed_at=BASE,
            raw_text=text, features=features, model_version="companion-q3-2026")
        return backend.engine.assess(signal, history=history, at=BASE)

    def test_routine_when_nothing_hits(self):
        a = self._assess("今天有点烦", frozenset({"negative_emotion"}))
        self.assertEqual(a.level, 0)
        self.assertEqual(a.hits, ())
        self.assertIn("普通波动", a.explain())

    def test_isolation_needs_persistent_feature(self):
        a = self._assess("只有你懂我，我一个人", frozenset({"ai_primary_attachment"}))
        self.assertEqual(a.level, 0, "未跨持续阈值不应升级")
        b = self._assess("只有你懂我，我一个人",
                         frozenset({"ai_primary_attachment", "isolation_days_3+"}))
        self.assertEqual(b.level, 1)
        self.assertEqual(b.hits[0].rule_version, "rules-2026-v1")
        self.assertTrue(b.rationale)

    def test_self_harm_level_2_explains_basis(self):
        a = self._assess("我有时候觉得消失就好了",
                         frozenset({"self_harm_keyphrase", "negative_emotion"}))
        self.assertEqual(a.level, 2)
        rule_ids = {h.rule_id for h in a.hits}
        self.assertIn("R-SELFHARM-01", rule_ids)
        explanation = a.explain()
        self.assertIn("规则集版本", explanation)
        self.assertIn("命中规则", explanation)

    def test_imminent_level_3(self):
        a = self._assess("再见了，今晚我已经准备好", frozenset(
            {"self_harm_keyphrase", "imminent_plan", "means_available"}))
        self.assertEqual(a.level, 3)
        self.assertEqual(len(a.hits), 2)

    def test_v2_raises_persistence_threshold(self):
        a = self._assess("只有你懂我",
                         frozenset({"ai_primary_attachment", "isolation_days_3+"}),
                         rules=RULES_V2)
        self.assertEqual(a.level, 0, "v2 要求4天阈值")
        b = self._assess("只有你懂我",
                         frozenset({"ai_primary_attachment", "isolation_days_4+"}),
                         rules=RULES_V2)
        self.assertEqual(b.level, 1)
        self.assertEqual(b.rule_set_version, "rules-2026-v2")


class EscalationTest(unittest.TestCase):
    def _ingest(self, backend, clock, notifier, text, features, at, sid):
        clock["now"] = at
        _, assessment, case = backend.ingest(
            student_id="stu-0117", raw_text=text, features=features,
            signal_id=sid, observed_at=at)
        return assessment, case

    def test_routine_opens_no_case_and_leaves_only_trend(self):
        backend, clock, notifier, _ = build_backend()
        _, case = self._ingest(backend, clock, notifier, "今天有点烦",
                               frozenset({"negative_emotion"}), BASE, "s0")
        self.assertIsNone(case)
        self.assertEqual(backend.retention.live_signal_ids, ())
        self.assertEqual(len(backend.trends.points()), 1)
        self.assertEqual(notifier.sent, [])

    def test_level1_sla_and_real_human_relay_required(self):
        backend, clock, notifier, _ = build_backend()
        _, case = self._ingest(
            backend, clock, notifier, "只有你懂我，我一个人",
            frozenset({"ai_primary_attachment", "isolation_days_3+"}), BASE, "s1")
        self.assertEqual(case.assignee_id, "staff-careteacher-chen")
        self.assertEqual(case.deadline, BASE + timedelta(days=3))
        with self.assertRaises(ValueError):
            backend.close_case(case.case_id, staff_id="staff-careteacher-chen", note="未接触先关")
        with self.assertRaises(ValueError):
            HumanContact(channel="ai_chat", staff_id="x",
                         staff_qualification="q", contacted_at=BASE, note="")

    def test_level2_guardian_notified_only_after_human_review(self):
        backend, clock, notifier, _ = build_backend()
        _, case = self._ingest(
            backend, clock, notifier, "我觉得消失就好了",
            frozenset({"self_harm_keyphrase", "negative_emotion"}), BASE, "s2")
        self.assertFalse(case.guardian_notified)
        self.assertEqual(notifier.sent, [])
        backend.acknowledge(case.case_id, staff_id="staff-counselor-zhao")
        self.assertEqual(len(notifier.sent), 1)
        self.assertNotIn("消失就好了", notifier.sent[0]["body"], "监护人通知不得包含对话内容")

    def test_override_before_ack_blocks_guardian_notice(self):
        backend, clock, notifier, _ = build_backend()
        _, case = self._ingest(
            backend, clock, notifier, "小说台词：撑不下去",
            frozenset({"self_harm_keyphrase", "negative_emotion"}), BASE, "s2b")
        with self.assertRaises(ValueError):
            backend.override_case(case.case_id, staff_id="staff-counselor-zhao", reason="  ")
        backend.override_case(case.case_id, staff_id="staff-counselor-zhao",
                              reason="系创作台词，已与本人确认", new_level=0)
        self.assertEqual(notifier.sent, [])
        # 原评估保留可查，否决以批注留痕
        got = backend.pipeline.case(case.case_id)
        self.assertEqual(got.assessment.level, 2)
        self.assertTrue(any(e.action == "case.overridden" for e in got.timeline))

    def test_level3_emergency_immediate_and_idempotent(self):
        backend, clock, notifier, _ = build_backend()
        at = datetime(2026, 9, 22, 1, 0)
        _, case = self._ingest(
            backend, clock, notifier, "再见了，今晚我已经准备好",
            frozenset({"self_harm_keyphrase", "imminent_plan", "means_available"}),
            at, "s3")
        self.assertTrue(case.night_routed)
        subjects = {n["subject"] for n in notifier.sent}
        self.assertIn("紧急：请立即与孩子当面确认安全", subjects)
        self.assertFalse(backend.pipeline.retry_emergency_contact(case.case_id))
        self.assertFalse(backend.pipeline.retry_emergency_contact(case.case_id))
        emergency = [n for n in notifier.sent if n["dedupe_key"] == f"emergency:{case.case_id}"]
        self.assertEqual(len(emergency), 1, "消息重试不能重复发出紧急联络")

    def test_sla_timeliness_review(self):
        backend, clock, notifier, _ = build_backend()
        at = datetime(2026, 9, 22, 1, 0)
        _, case = self._ingest(
            backend, clock, notifier, "再见了，今晚",
            frozenset({"self_harm_keyphrase", "imminent_plan", "means_available"}),
            at, "s3b")
        # 超过15分钟时限才接触
        backend.acknowledge(case.case_id, staff_id="staff-night-li")
        backend.record_human_contact(case.case_id, HumanContact(
            channel="voice_call", staff_id="staff-night-li",
            staff_qualification="夜间值守持证心理教师",
            contacted_at=at + timedelta(minutes=40), note="电话接通并保持通话"))
        self.assertFalse(case.sla_met())
        report = ProfessionalView(pipeline=backend.pipeline, engine=backend.engine)\
            .case_review(case.case_id)
        self.assertTrue(report["relay_complete"])
        self.assertFalse(report["timely"])


class PrivacyBoundaryTest(unittest.TestCase):
    def test_raw_text_never_lands_in_backend(self):
        backend, clock, _, _ = build_backend()
        secret_phone = "13912345678"
        secret_name = "我叫西门小禾"
        clock["now"] = datetime(2026, 9, 22, 23, 30)
        backend.ingest(
            student_id="stu-0117",
            raw_text=f"{secret_name}，{secret_phone}，再见了今晚我已经准备好",
            features=frozenset({"self_harm_keyphrase", "imminent_plan", "means_available"}),
            signal_id="s-secret", observed_at=clock["now"])
        dumped = json.dumps(
            [{"detail": e.detail, "kind": e.kind} for e in backend.audit.events],
            ensure_ascii=False, default=str)
        self.assertNotIn(secret_phone, dumped)
        self.assertNotIn("西门小禾", dumped)
        for signal_id in backend.retention.live_signal_ids:
            stored = backend.retention.signal(signal_id)
            self.assertNotIn(secret_phone, stored.text)

    def test_without_consent_ingest_is_refused(self):
        backend, clock, _, _ = build_backend()
        backend.revoke_consent("stu-0117", reason="家长撤回")
        with self.assertRaises(PermissionError):
            backend.ingest(student_id="stu-0117", raw_text="任何内容",
                           features=frozenset(), signal_id="sx",
                           observed_at=BASE + timedelta(hours=1))

    def test_revoke_purges_signals_and_issues_proof(self):
        backend, clock, _, _ = build_backend()
        at = datetime(2026, 9, 22, 23, 30)
        clock["now"] = at
        backend.ingest(student_id="stu-0117", raw_text="再见了今晚",
                       features=frozenset({"self_harm_keyphrase", "imminent_plan",
                                           "means_available"}),
                       signal_id="s-del", observed_at=at)
        self.assertTrue(backend.retention.signals_of("stu-0117"))
        cert = backend.revoke_consent("stu-0117", reason="停止使用")
        self.assertEqual(cert.kind, "consent.revoked")
        self.assertEqual(cert.payload["signals_deleted"], 1)
        self.assertEqual(backend.retention.signals_of("stu-0117"), ())
        self.assertTrue(cert.verify(backend.audit.events))


class ProofsAndLifecycleTest(unittest.TestCase):
    def test_all_certificate_kinds_verify_even_after_chain_grows(self):
        backend, clock, _, _ = build_backend()
        at = datetime(2026, 9, 22, 23, 30)
        clock["now"] = at
        _, case = backend.ingest(
            student_id="stu-0117", raw_text="再见了今晚",
            features=frozenset({"self_harm_keyphrase", "imminent_plan", "means_available"}),
            signal_id="s4", observed_at=at)[1:]
        kinds = set()
        # 夜间交接证书在夜间开单时签发
        kinds.update(c.kind for c in backend.audit.certificates)
        backend.load_rules(RULES_V2)
        kinds.add("rules.switched")
        referral = backend.refer_student("stu-0117", to_school_id="school-heping-02",
                                         reason="转学")
        kinds.add(referral.kind)
        self.assertIn("night.handoff", kinds)
        # 转介包不含文本，只含工单与依据
        self.assertNotIn("text", json.dumps(referral.payload, ensure_ascii=False))
        self.assertEqual(referral.payload["open_cases"][0]["level"], 3)
        # 继续追加事件后，旧证书仍可校验（链前缀语义）
        backend.acknowledge(case.case_id, staff_id="staff-night-li")
        self.assertTrue(backend.verify_proofs())

    def test_expiry_deletion_certificates(self):
        store = RetentionStore(RetentionPolicy(signal_days=0, case_days=0))
        signal = make_signal(signal_id="s", student_id="stu-0117", observed_at=BASE,
                             raw_text="只有你懂我", features=frozenset(), model_version="m")
        store.keep_signal(signal, stored_at=BASE)
        store.keep_case("case-x", closed_at=BASE)
        records = store.purge_expired(today=BASE.date())
        categories = {r.category for r in records}
        self.assertEqual(categories, {"signal", "case"})
        self.assertEqual(store.live_signal_ids, ())
        self.assertEqual(store.live_case_ids, ())

    def test_audit_chain_tampering_detected(self):
        log = AuditLog()
        log.append(kind="rules.switched", at=BASE, actor="a", subject="r", detail={"v": 1})
        log.append(kind="consent.revoked", at=BASE, actor="g", subject="s", detail={"x": 1})
        ok, _ = verify_chain(log.events)
        self.assertTrue(ok)
        original = log.events[1]
        from dataclasses import replace
        forged = replace(original, detail={"x": 999})
        ok, _ = verify_chain((log.events[0], forged))
        self.assertFalse(ok, "篡改审计内容必须被发现")


class ViewsTest(unittest.TestCase):
    def test_student_sees_who_can_see_what(self):
        _, _, _, student = build_backend()
        items = StudentView(student).visibility()
        joined = "".join(i.data + i.visible_to for i in items)
        self.assertIn("逐句对话原文", joined)
        self.assertIn("无人", joined)

    def test_guardian_view_minimal_and_excludes_overridden(self):
        backend, clock, notifier, _ = build_backend()
        # 2级且被否决：监护人无结论
        _, case = backend.ingest(
            student_id="stu-0117", raw_text="小说台词撑不下去",
            features=frozenset({"self_harm_keyphrase", "negative_emotion"}),
            signal_id="v2", observed_at=BASE)[1:]
        backend.override_case(case.case_id, staff_id="staff-counselor-zhao",
                              reason="创作台词误报")
        # 3级：监护人收到一条结论
        at = datetime(2026, 9, 22, 1, 0)
        clock["now"] = at
        backend.ingest(
            student_id="stu-0117", raw_text="再见了今晚",
            features=frozenset({"self_harm_keyphrase", "imminent_plan", "means_available"}),
            signal_id="v3", observed_at=at)
        rows = GuardianView(pipeline=backend.pipeline).conclusions_for("stu-0117")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "迫在眉睫伤害")
        for row in rows:
            self.assertNotIn("再见", json.dumps(row, ensure_ascii=False))

    def test_trend_is_deidentified(self):
        store = TrendStore()
        store.absorb(moment=BASE, age=14, level=0)
        rows = TrendView(store.points()).rows()
        self.assertEqual(rows[0]["age_band"], "13-15")
        self.assertNotIn("stu", json.dumps(rows, ensure_ascii=False))


class RegistrationTest(unittest.TestCase):
    def test_age_and_consent_guardrails(self):
        with self.assertRaises(ValueError):
            StudentRecord(
                student_id="x", school_id="s", age=18, display_name="n",
                consent=None, enrolled_at=BASE, service_window=ServiceWindow(),
                model_version="m")

    def test_night_window(self):
        window = ServiceWindow(7, 22)
        self.assertTrue(window.is_night(datetime(2026, 9, 22, 23, 0)))
        self.assertFalse(window.is_night(datetime(2026, 9, 22, 12, 0)))


if __name__ == "__main__":
    unittest.main()
