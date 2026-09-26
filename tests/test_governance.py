import unittest

from tests.support import (
    build_engine, register_student, SCHOOL, SCHOOL2, GUARDIAN_A, GUARDIAN_B,
    COUNSELOR_CHEN, PSYCH_ZHOU,
)
from src.governance.rules import EMERGENCY, URGENT, WATCH, relaxed_pack
from src.governance.schedule import is_night, within_window, next_workday_noon
from src.governance.visibility import (
    GUARDIAN, PSYCHOLOGIST, STUDENT, TEACHER, COUNSELOR, REVIEWER,
    authorize, student_access_report,
)


def signal(pseudo, at, **features):
    return {
        "pseudo": pseudo, "at": at, "model": "companion-7.2",
        "window": "evening", "features": features,
    }


class ScheduleTest(unittest.TestCase):
    def test_night_windows(self):
        self.assertTrue(is_night("2026-09-11T22:30+08:00"))
        self.assertTrue(is_night("2026-09-12T06:59+08:00"))
        self.assertFalse(is_night("2026-09-12T07:00+08:00"))
        self.assertTrue(within_window("2026-09-12T12:00+08:00"))
        self.assertFalse(within_window("2026-09-12T22:30+08:00"))

    def test_watch_deadline_skips_weekend(self):
        # 2026-09-12 是周六
        deadline = next_workday_noon("2026-09-12T10:00+08:00")
        self.assertEqual(deadline, "2026-09-14T12:00+08:00")  # 周一中午


class VisibilityTest(unittest.TestCase):
    def test_nobody_sees_raw_messages(self):
        for role in (STUDENT, GUARDIAN, TEACHER, COUNSELOR, PSYCHOLOGIST, REVIEWER):
            self.assertFalse(authorize(role, "raw_message"), role)

    def test_guardian_minimal_only(self):
        self.assertTrue(authorize(GUARDIAN, "guardian_digest"))
        self.assertTrue(authorize(GUARDIAN, "level"))
        for resource in ("signal_features", "signal_snippets", "rule_basis",
                         "professional_digest"):
            self.assertFalse(authorize(GUARDIAN, resource), resource)

    def test_student_can_read_basis_and_audit(self):
        self.assertTrue(authorize(STUDENT, "signal_features"))
        self.assertTrue(authorize(STUDENT, "rule_basis"))
        self.assertTrue(authorize(STUDENT, "audit"))

    def test_reviewer_sees_audit_and_relay(self):
        self.assertTrue(authorize(REVIEWER, "audit"))
        self.assertTrue(authorize(REVIEWER, "professional_digest"))

    def test_student_report_names_every_audience(self):
        audiences = {line.audience for line in student_access_report()}
        self.assertEqual(audiencies := audiences, {
            STUDENT, GUARDIAN, TEACHER, COUNSELOR, PSYCHOLOGIST, REVIEWER,
        })


class RuleVersioningTest(unittest.TestCase):
    def test_pack_swap_is_audited_and_legacy_keeps_version(self):
        engine = build_engine()
        pseudo = register_student(engine)
        engine.ingest(signal(pseudo, "2026-09-12T10:00+08:00", self_harm_phrase=1))
        case = engine.cases.active_for(pseudo)
        self.assertEqual(case.current_leg().rule_pack_version, "risk-rules-v1")
        engine.publish_pack(relaxed_pack(), "2026-09-20T09:00+08:00",
                            by="compliance-001")
        self.assertEqual(engine.active_pack, "risk-rules-v2")
        actions = [e.action for e in engine.audit.entries]
        self.assertIn("rule-pack-published", actions)
        # 旧案件的接力段仍标注旧版规则
        self.assertEqual(case.current_leg().rule_pack_version, "risk-rules-v1")
        # 新判定用新版：同样信号在 v2 下仍 urgent（R-URG-01 未变）
        result = engine.ingest(signal(pseudo, "2026-09-21T10:00+08:00", self_harm_phrase=1))
        self.assertEqual(result.verdict.rule_pack_version, "risk-rules-v2")


class ReferralTest(unittest.TestCase):
    def test_cross_school_referral_carries_summary_only(self):
        engine = build_engine()
        pseudo = register_student(engine)
        result = engine.ingest(signal(pseudo, "2026-09-12T10:00+08:00", self_harm_phrase=1))
        case_id = result.case_id
        engine.record_human_contact(
            case_id, COUNSELOR_CHEN.staff_id, "counselor", "in-person",
            "2026-09-12T14:00+08:00", "完成谈话，建议转介至新学校持续关怀",
        )
        referral = engine.make_referral(
            "ref-0007", pseudo, SCHOOL2, COUNSELOR_CHEN.staff_id,
            "2026-09-15T09:00+08:00",
        )
        # 转介材料里没有特征码与片段
        self.assertNotIn("self_harm", referral.safety_summary)
        # 未重新确认授权不能接受
        with self.assertRaises(RuntimeError):
            engine.referrals.accept("ref-0007", "2026-09-15T10:00+08:00")
        engine.accept_referral("ref-0007", GUARDIAN_B, "2026-09-15T10:00+08:00")
        completed = engine.complete_referral(
            "ref-0007", COUNSELOR_CHEN.staff_id, "2026-09-16T09:00+08:00",
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(engine.identities.school_of(pseudo), SCHOOL2)
        new_case = engine.cases.get(completed.new_case_id)
        self.assertEqual(new_case.school_id, SCHOOL2)
        self.assertEqual(new_case.level, URGENT)
        # 旧案已关
        self.assertEqual(engine.cases.get(case_id).status, "closed")


class RetentionTest(unittest.TestCase):
    def test_expired_signals_erased_with_certificate(self):
        engine = build_engine()
        pseudo = register_student(engine)
        engine.ingest(signal(pseudo, "2026-09-01T20:00+08:00", mood_score=70))
        self.assertEqual(len(engine.history.for_(pseudo)), 1)
        # 未到期不删
        self.assertEqual(engine.sweep_retention("2026-10-01T00:00+08:00"), [])
        self.assertEqual(len(engine.history.for_(pseudo)), 1)
        # 信号保留期 90 天
        certs = engine.sweep_retention("2026-12-01T00:00+08:00")
        self.assertEqual(len(certs), 1)
        cert = certs[0]
        self.assertTrue(cert["erased"])
        self.assertEqual(cert["kind"], "signal")
        self.assertEqual(cert["pseudo"], pseudo)
        self.assertEqual(engine.history.for_(pseudo), [])
        # 审计链中可核验删除
        purged = [e for e in engine.audit.entries if e.action == "data-purged"]
        self.assertEqual(len(purged), 1)
        self.assertEqual(purged[0].evidence["digest_before_purge"],
                         cert["digest_before_purge"])
        engine.verify_evidence()


class ReviewerEndToEndTest(unittest.TestCase):
    def test_reviewer_can_audit_timely_relay(self):
        engine = build_engine()
        pseudo = register_student(engine)
        result = engine.ingest(signal(pseudo, "2026-09-12T10:00+08:00", self_harm_phrase=1))
        case_id = result.case_id
        engine.record_human_contact(
            case_id, COUNSELOR_CHEN.staff_id, "counselor", "in-person",
            "2026-09-12T11:00+08:00", "首次现实谈话完成",
        )
        review = engine.review_case(case_id, "reviewer-001", REVIEWER,
                                    "2026-09-12T16:00+08:00")
        self.assertTrue(review["all_within_sla"])
        self.assertIn("依据：", review["digest"])
        self.assertIn("R-URG-01", review["digest"])
        # 班主任无权查看专业接力
        with self.assertRaises(PermissionError):
            engine.review_case(case_id, "staff-2001", TEACHER,
                               "2026-09-12T16:05+08:00")
        engine.verify_evidence()
