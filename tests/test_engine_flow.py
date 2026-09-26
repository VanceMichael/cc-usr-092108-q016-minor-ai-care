import unittest

from tests.support import (
    build_engine, register_student, SCHOOL, GUARDIAN_A,
    COUNSELOR_CHEN, PSYCH_ZHOU, TEACHER_WANG,
)
from src.governance.signals import (
    F_AI_BOND, F_GOOD_MOMENT, F_ISOLATION, F_MOOD, F_SELFHARM, F_SLEEP,
)
from src.governance.rules import EMERGENCY, NORMAL, URGENT, WATCH
from src.governance.cases import ContactRejected
from src.governance.audit import TamperDetected


def signal(pseudo, at, **features):
    return {
        "pseudo": pseudo, "at": at, "model": "companion-7.2",
        "window": "evening", "features": features,
    }


class RegistrationGateTest(unittest.TestCase):
    def test_unknown_and_retired_model_rejected(self):
        engine = build_engine()
        pseudo = register_student(engine)
        bad = engine.ingest(signal(pseudo, "2026-09-02T20:00+08:00",
                                   **{"mood_score": 70}))
        # 覆盖 model 字段
        bad = engine.ingest({
            "pseudo": pseudo, "at": "2026-09-02T20:00+08:00",
            "model": "companion-6.0", "features": {"mood_score": 70},
        })
        self.assertFalse(bad.accepted)
        self.assertIn("模型版本", bad.reason)

        engine.retire_model("companion-7.2", "2026-09-03T00:00+08:00", "ops-system")
        retired = engine.ingest({
            "pseudo": pseudo, "at": "2026-09-03T08:00+08:00",
            "model": "companion-7.2", "features": {"mood_score": 70},
        })
        self.assertFalse(retired.accepted)
        # 新版模型（已登记，9/15 生效）可正常入域
        ok = engine.ingest({
            "pseudo": pseudo, "at": "2026-09-16T08:00+08:00",
            "model": "companion-7.3", "features": {"mood_score": 70},
        })
        self.assertTrue(ok.accepted)


class IngestGateTest(unittest.TestCase):
    def test_unregistered_rejected(self):
        engine = build_engine()
        result = engine.ingest(signal("nope", "2026-09-02T20:00+08:00", mood_score=10))
        self.assertFalse(result.accepted)
        self.assertIn("未登记", result.reason)

    def test_withdrawn_consent_blocks_new_signals(self):
        engine = build_engine()
        pseudo = register_student(engine)
        ok = engine.ingest(signal(pseudo, "2026-09-02T20:00+08:00", mood_score=80, good_moment=1))
        self.assertTrue(ok.accepted)
        engine.withdraw_consent(pseudo, GUARDIAN_A, "2026-09-03T08:00+08:00", "家庭自主决定")
        blocked = engine.ingest(signal(pseudo, "2026-09-03T20:00+08:00", mood_score=10))
        self.assertFalse(blocked.accepted)
        self.assertIn("撤回", blocked.reason)


class NormalFluxTest(unittest.TestCase):
    def test_normal_flux_only_feeds_trends(self):
        engine = build_engine()
        pseudo = register_student(engine)
        result = engine.ingest(signal(pseudo, "2026-09-02T20:00+08:00",
                                      mood_score=72, isolation_score=20, good_moment=1))
        self.assertTrue(result.accepted)
        self.assertEqual(result.level, NORMAL)
        self.assertEqual(result.actions, ("trend-only",))
        self.assertEqual(result.notices, ())
        self.assertIsNone(engine.cases.active_for(pseudo))
        # 家长端什么都收不到
        self.assertEqual(engine.guardian_view(GUARDIAN_A, "2026-09-02T21:00+08:00"), [])


class WatchNightTest(unittest.TestCase):
    def test_watch_at_night_queues_and_next_morning_routed(self):
        engine = build_engine()
        pseudo = register_student(engine)
        # 近14日孤立感3次>=70：前两次白天，第三次落在夜间
        engine.ingest(signal(pseudo, "2026-09-05T19:00+08:00", isolation_score=80))
        engine.ingest(signal(pseudo, "2026-09-08T19:00+08:00", isolation_score=80))
        result = engine.ingest(signal(pseudo, "2026-09-11T22:30+08:00", isolation_score=80))
        self.assertEqual(result.level, WATCH)
        self.assertTrue(result.night)
        self.assertIn("queued-for-next-day-review", result.actions)
        self.assertEqual(len(engine.night_queue()), 1)
        # 夜间不发家长通知
        self.assertEqual(engine.guardian_view(GUARDIAN_A, "2026-09-11T23:00+08:00"), [])
        # 次日早晨复核，分配给白班心理老师
        routed = engine.process_night_reviews("2026-09-12T07:30+08:00")
        self.assertEqual(routed, [pseudo])
        case = engine.cases.active_for(pseudo)
        # watch 等级优先派给满足资质的最低岗位：班主任日常关心
        self.assertEqual(case.current_leg().assignee.staff_id, TEACHER_WANG.staff_id)


class EscalationRelayTest(unittest.TestCase):
    def _watch_then_urgent(self, engine, pseudo):
        # watch 基线
        for day in (5, 8, 11):
            engine.ingest(signal(pseudo, f"2026-09-{day:02d}T19:00+08:00", isolation_score=80))
        case = engine.cases.active_for(pseudo)
        self.assertEqual(case.level, WATCH)
        # 白天由心理老师承接 watch 段
        engine.record_human_contact(
            case.case_id, COUNSELOR_CHEN.staff_id, "counselor", "in-person",
            "2026-09-11T20:00+08:00", "课后简短交谈，情绪平稳",
        )
        # 次日出现自伤暗示 -> urgent
        result = engine.ingest(signal(pseudo, "2026-09-12T10:00+08:00", self_harm_phrase=1))
        self.assertEqual(result.level, URGENT)
        return result.case_id

    def test_full_escalation_and_sla_relay(self):
        engine = build_engine()
        pseudo = register_student(engine)
        case_id = self._watch_then_urgent(engine, pseudo)
        case = engine.cases.get(case_id)
        self.assertEqual(len(case.legs), 2)
        urgent_leg = case.legs[-1]
        self.assertEqual(urgent_leg.level, URGENT)
        self.assertEqual(urgent_leg.deadline_at, "2026-09-13T10:00+08:00")
        # 24小时内完成真人谈话，SLA 达成
        engine.record_human_contact(
            case_id, COUNSELOR_CHEN.staff_id, "counselor", "voice-call",
            "2026-09-12T15:00+08:00", "已完成首次现实谈话，约定明日面谈",
        )
        review = case.review_relay()
        self.assertTrue(review["all_completed"])
        self.assertTrue(review["all_within_sla"])
        self.assertTrue(all(leg["channel"] in ("in-person", "voice-call") for leg in review["legs"]))

    def test_ai_chat_does_not_close_relay(self):
        engine = build_engine()
        pseudo = register_student(engine)
        case_id = self._watch_then_urgent(engine, pseudo)
        with self.assertRaises(ContactRejected):
            engine.record_human_contact(
                case_id, "ai-bot", "psychologist", "ai-chat",
                "2026-09-12T11:00+08:00", "AI又聊了一次",
            )

    def test_underqualified_staff_cannot_close(self):
        engine = build_engine()
        pseudo = register_student(engine)
        # 白天：紧急等级自动派给在场的专职心理教师
        result = engine.ingest(signal(pseudo, "2026-09-12T10:00+08:00", self_harm_phrase=3))
        case_id = result.case_id
        self.assertEqual(result.level, EMERGENCY)
        self.assertEqual(
            engine.cases.get(case_id).current_leg().assignee.staff_id,
            PSYCH_ZHOU.staff_id,
        )
        # 班主任不能承接紧急
        with self.assertRaises(ContactRejected):
            engine.cases.get(case_id).assign(TEACHER_WANG, "2026-09-12T10:01+08:00")

    def test_night_without_qualified_coverage_is_flagged(self):
        from tests.support import SCHOOL2, GUARDIAN_B
        engine = build_engine()
        pseudo = register_student(
            engine, pseudo="pseudo-hai-13", guardian=GUARDIAN_B, school=SCHOOL2,
        )
        # 临海校无夜间值守：夜间紧急信号标记为未派单
        night = engine.ingest(signal(pseudo, "2026-09-12T22:30+08:00", self_harm_phrase=3))
        self.assertEqual(night.level, EMERGENCY)
        self.assertIn("escalation-unassigned", night.actions)

    def test_late_contact_recorded_as_sla_breach(self):
        engine = build_engine()
        pseudo = register_student(engine)
        result = engine.ingest(signal(pseudo, "2026-09-12T10:00+08:00", self_harm_phrase=1))
        case_id = result.case_id
        # 超过24小时才接触
        engine.record_human_contact(
            case_id, COUNSELOR_CHEN.staff_id, "counselor", "in-person",
            "2026-09-13T11:00+08:00", "补登首次谈话",
        )
        review = engine.cases.get(case_id).review_relay()
        self.assertTrue(review["all_completed"])
        self.assertFalse(review["all_within_sla"])
        self.assertFalse(review["legs"][0]["within_sla"])

    def test_emergency_deadline_is_30_minutes(self):
        engine = build_engine()
        pseudo = register_student(engine)
        result = engine.ingest(signal(pseudo, "2026-09-12T10:00+08:00", self_harm_phrase=3))
        self.assertEqual(result.level, EMERGENCY)
        case = engine.cases.get(result.case_id)
        self.assertEqual(case.current_leg().deadline_at, "2026-09-12T10:30+08:00")


class GuardianNoticeIdempotencyTest(unittest.TestCase):
    def test_retry_does_not_duplicate_emergency_contact(self):
        engine = build_engine()
        pseudo = register_student(engine)
        result = engine.ingest(signal(pseudo, "2026-09-12T10:00+08:00", self_harm_phrase=3))
        key = result.notices[0]["key"]
        # 模拟消息队列多次重试
        for _ in range(5):
            sent = engine.retry_notice(key)
            self.assertEqual(sent, 1)
        view = engine.guardian_view(GUARDIAN_A, "2026-09-12T10:05+08:00")
        self.assertEqual(len(view), 1)
        self.assertTrue(view[0]["delivered_once"])
        self.assertEqual(view[0]["attempts"], 6)
        self.assertNotIn("self_harm", view[0]["safety_conclusion"])

    def test_watch_sends_nothing_to_guardian(self):
        engine = build_engine()
        pseudo = register_student(engine)
        for day in (5, 8, 11):
            engine.ingest(signal(pseudo, f"2026-09-{day:02d}T19:00+08:00", isolation_score=80))
        self.assertEqual(engine.guardian_view(GUARDIAN_A, "2026-09-12T08:00+08:00"), [])


class OverrideTest(unittest.TestCase):
    def test_override_cannot_downgrade_before_human_contact(self):
        engine = build_engine()
        pseudo = register_student(engine)
        result = engine.ingest(signal(pseudo, "2026-09-12T10:00+08:00", self_harm_phrase=1))
        with self.assertRaises(ContactRejected):
            engine.override_verdict(
                pseudo, PSYCH_ZHOU.staff_id, "psychologist", WATCH,
                "复核认为是引用歌词", "2026-09-12T10:20+08:00",
            )

    def test_override_after_contact_is_recorded_with_reason(self):
        engine = build_engine()
        pseudo = register_student(engine)
        result = engine.ingest(signal(pseudo, "2026-09-12T10:00+08:00", self_harm_phrase=1))
        case_id = result.case_id
        engine.record_human_contact(
            case_id, COUNSELOR_CHEN.staff_id, "counselor", "in-person",
            "2026-09-12T11:00+08:00", "核实为创作歌词讨论，本人状态稳定",
        )
        verdict = engine.override_verdict(
            pseudo, PSYCH_ZHOU.staff_id, "psychologist", WATCH,
            "复核：自伤词来自歌词创作，面谈确认无自伤意图",
            "2026-09-12T11:30+08:00",
        )
        self.assertEqual(verdict.source, "manual-override")
        self.assertEqual(verdict.level, WATCH)
        self.assertIn("歌词", verdict.override_reason)
        # 机器依据仍保留
        self.assertTrue(verdict.hits)

    def test_human_can_escalate_upward(self):
        engine = build_engine()
        pseudo = register_student(engine)
        engine.ingest(signal(pseudo, "2026-09-05T19:00+08:00", mood_score=70))
        verdict = engine.override_verdict(
            pseudo, COUNSELOR_CHEN.staff_id, "counselor", URGENT,
            "家长来电告知重大家庭变故", "2026-09-05T20:00+08:00",
        )
        self.assertEqual(verdict.level, URGENT)
        case = engine.cases.active_for(pseudo)
        self.assertIsNotNone(case)
        # 人工升级同样立即派单（资质达标）并向家长发出一次必要通知
        self.assertIsNotNone(case.current_leg().assignee)
        view = engine.guardian_view(GUARDIAN_A, "2026-09-05T20:05+08:00")
        self.assertEqual(len(view), 1)
        self.assertIn("情绪信号", view[0]["safety_conclusion"])


class AuditChainTest(unittest.TestCase):
    def test_chain_verifies_and_detects_tampering(self):
        engine = build_engine()
        pseudo = register_student(engine)
        engine.ingest(signal(pseudo, "2026-09-05T19:00+08:00", mood_score=70))
        engine.verify_evidence()
        # 篡改一条历史证据
        target = engine.audit.entries[1]
        target.evidence["level"] = EMERGENCY
        with self.assertRaises(TamperDetected):
            engine.verify_evidence()

    def test_chain_links_entries(self):
        from src.governance.audit import GENESIS
        engine = build_engine()
        register_student(engine)
        engine.ingest(signal("pseudo-lin-13", "2026-09-05T19:00+08:00", mood_score=70))
        entries = engine.audit.entries
        self.assertGreaterEqual(len(entries), 2)
        self.assertEqual(entries[0].prev_hash, GENESIS)
        self.assertEqual(entries[1].prev_hash, entries[0].hash)
