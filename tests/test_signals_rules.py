import unittest
from datetime import timedelta

from src.governance.clock import parse
from src.governance.signals import (
    F_AI_BOND, F_ISOLATION, F_MOOD, F_SELFHARM, F_HARM_OTHER,
    F_HELPLESS, F_SLEEP, redact_signal, SignalHistory, TrendStore,
)
from src.governance.rules import (
    EMERGENCY, NORMAL, URGENT, WATCH, RuleEngine, default_pack, relaxed_pack,
)


class RedactionTest(unittest.TestCase):
    def test_unknown_features_and_free_text_dropped(self):
        signal = redact_signal({
            "pseudo": "p1", "at": "2026-09-01T20:00+08:00",
            "model": "companion-7.2",
            "window": "w1",
            "features": {"unknown_x": 99, F_MOOD: 20},
            "snippets": ["  a\nb ", "a b", "x" * 100],
            "message": "这是不允许进入治理域的对话原文",
            "text": "同样拒绝",
        })
        self.assertEqual(signal.features, {F_MOOD: 20.0})
        self.assertEqual(signal.snippets, ("a b", "x" * 24))
        self.assertTrue(signal.redacted)
        self.assertFalse(hasattr(signal, "message"))

    def test_clamps_extreme_values(self):
        signal = redact_signal({
            "pseudo": "p1", "at": "2026-09-01T20:00+08:00",
            "model": "m", "features": {F_MOOD: -50, F_ISOLATION: 9999},
        })
        self.assertEqual(signal.features[F_MOOD], 0)
        self.assertEqual(signal.features[F_ISOLATION], 1000)


class RuleEvaluationTest(unittest.TestCase):
    def test_normal_when_no_signal(self):
        pack = default_pack()
        level, hits = RuleEngine().evaluate(pack, [])
        self.assertEqual(level, NORMAL)
        self.assertEqual(hits, [])

    def test_watch_isolation_rule_with_basis(self):
        from src.governance.signals import Signal
        rows = [
            Signal("p1", f"2026-09-{d:02d}T12:00+08:00", "m", "w",
                   {F_ISOLATION: 75})
            for d in range(1, 15, 4)  # 4 个时点
        ]
        level, hits = RuleEngine().evaluate(default_pack(), rows)
        self.assertEqual(level, WATCH)
        self.assertEqual(hits[0]["rule"], "R-WATCH-01")
        self.assertIn("孤立感", hits[0]["rationale"])
        fact = hits[0]["facts"][0]
        self.assertEqual(fact["hits"], 4)
        self.assertGreaterEqual(fact["observed"], 70)

    def test_urgent_selfharm_3day(self):
        from src.governance.signals import Signal
        rows = [Signal("p1", "2026-09-10T12:00+08:00", "m", "w", {F_SELFHARM: 1})]
        level, _ = RuleEngine().evaluate(default_pack(), rows)
        self.assertEqual(level, URGENT)

    def test_emergency_repeated_selfharm_24h(self):
        from src.governance.signals import Signal
        base = parse("2026-09-10T20:00+08:00")
        rows = [
            Signal("p1", (base + timedelta(hours=6 * i)).isoformat(), "m", "w",
                   {F_SELFHARM: 2})
            for i in range(3)
        ]
        level, hits = RuleEngine().evaluate(default_pack(), rows)
        self.assertEqual(level, EMERGENCY)
        self.assertIn("R-EMER-02", [h["rule"] for h in hits])

    def test_harm_other_emergency(self):
        from src.governance.signals import Signal
        rows = [Signal("p1", "2026-09-10T20:00+08:00", "m", "w", {F_HARM_OTHER: 1})]
        self.assertEqual(RuleEngine().evaluate(default_pack(), rows)[0], EMERGENCY)

    def test_urgent_multi_feature_depressed(self):
        from src.governance.signals import Signal
        rows = []
        for i, day in enumerate([1, 3, 5]):
            rows.append(Signal(
                "p1", f"2026-09-{day:02d}T12:00+08:00", "m", "w",
                {F_MOOD: 25, F_HELPLESS: 1},
            ))
        level, hits = RuleEngine().evaluate(default_pack(), rows)
        self.assertEqual(level, URGENT)
        self.assertIn("R-URG-02", [h["rule"] for h in hits])

    def test_old_signals_outside_window_dont_count(self):
        from src.governance.signals import Signal
        rows = [
            Signal("p1", "2026-08-01T12:00+08:00", "m", "w", {F_SELFHARM: 5}),
            Signal("p1", "2026-09-10T12:00+08:00", "m", "w", {}),
        ]
        level, _ = RuleEngine().evaluate(default_pack(), rows)
        self.assertEqual(level, NORMAL)

    def test_v2_pack_relaxes_watch_thresholds(self):
        from src.governance.signals import Signal
        rows = [
            Signal("p1", f"2026-09-{d:02d}T12:00+08:00", "m", "w", {F_ISOLATION: 75})
            for d in range(1, 15, 4)
        ]
        self.assertEqual(RuleEngine().evaluate(relaxed_pack(), rows)[0], NORMAL)
        # v2 阈值80：需要近14日3次>=80
        rows2 = rows + [
            Signal("p1", f"2026-09-{d:02d}T12:00+08:00", "m", "w", {F_ISOLATION: 85})
            for d in (13, 14, 15)
        ]
        self.assertEqual(RuleEngine().evaluate(relaxed_pack(), rows2)[0], WATCH)

    def test_v2_tightens_emergency_selfharm_repetition(self):
        from src.governance.signals import Signal
        base = parse("2026-09-10T20:00+08:00")
        rows = [
            Signal("p1", (base + timedelta(hours=4)).isoformat(), "m", "w", {F_SELFHARM: 1})
            for _ in range(2)
        ]
        # v1：24h 累计2次即紧急；v2：需累计3次
        self.assertEqual(RuleEngine().evaluate(default_pack(), rows)[0], EMERGENCY)
        self.assertEqual(RuleEngine().evaluate(relaxed_pack(), rows)[0], URGENT)


class TrendKAnonymityTest(unittest.TestCase):
    def test_small_bucket_suppressed(self):
        store = TrendStore(k=5)
        for i in range(4):
            store.add(f"s{i}", "初一", "2026-W36", F_MOOD, 40 + i)
        self.assertEqual(store.export(), [])
        self.assertEqual(store.suppressed_buckets(), 1)

    def test_big_bucket_exported_without_identifiers(self):
        store = TrendStore(k=5)
        for i in range(6):
            store.add(f"s{i}", "初一", "2026-W36", F_MOOD, 40 + i)
        rows = store.export()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["n"], 6)
        self.assertNotIn("pseudo", rows[0])
        self.assertNotIn("s0", rows[0])
