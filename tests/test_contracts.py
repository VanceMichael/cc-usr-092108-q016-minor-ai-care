"""契约与样例一致性：不引入第三方依赖，用标准库做结构性校验。"""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ContractFixtureTest(unittest.TestCase):
    def setUp(self):
        self.contracts = ROOT / "contracts"
        self.fixtures = ROOT / "fixtures"

    def _load(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_schemas_are_valid_json_with_required_fields(self):
        for name in ("redacted-signal.schema.json", "governance-event.schema.json"):
            schema = self._load(self.contracts / name)
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertTrue(schema["required"])

    def test_setup_fixture_shape(self):
        setup = self._load(self.fixtures / "setup.json")
        self.assertIn("虚构", setup["notice"])
        self.assertTrue(setup["schools"])
        self.assertTrue(setup["models"])
        for student in setup["students"]:
            self.assertLessEqual(student["age"], 17)
        # 监护授权与学生一一对应
        self.assertEqual(
            {c["pseudo"] for c in setup["consents"]},
            {s["pseudo"] for s in setup["students"]},
        )

    def test_replay_audit_entries_match_contract(self):
        chain = self._load(self.fixtures / "replay" / "audit-chain.json")
        schema = self._load(self.contracts / "governance-event.schema.json")
        allowed = set(schema["properties"]["action"]["enum"])
        required = schema["required"]
        prev = "GENESIS"
        for entry in chain["entries"]:
            for field in required:
                self.assertIn(field, entry, f"审计条目缺少 {field}")
            self.assertIn(entry["action"], allowed)
            self.assertRegex(entry["hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(entry["prev_hash"], prev)
            # 证据中不得出现任何对话原文形态字段
            self.assertNotIn("message", entry["evidence"])
            self.assertNotIn("text", entry["evidence"])
            prev = entry["hash"]

    def test_replay_outputs_exist(self):
        replay_dir = self.fixtures / "replay"
        for name in (
            "audit-chain.json", "case-review-lin.json", "verdicts-lin.json",
            "guardian-view.json", "student-visibility.json", "trends.json",
            "referral.json", "purge-certificates.json", "ingest-gates.json",
            "safety-summary-transferred.txt", "summary.md",
        ):
            self.assertTrue((replay_dir / name).exists(), name)

    def test_guardian_digest_never_contains_feature_codes(self):
        view = self._load(self.fixtures / "replay" / "guardian-view.json")
        for rows in view.values():
            for row in rows:
                for forbidden in ("self_harm", "isolation_score", "mood_score", "snippet"):
                    self.assertNotIn(forbidden, row["safety_conclusion"])
                self.assertTrue(row["delivered_once"])
