"""确定性端到端回放：用虚构数据走完整治理流程并产出可核验样例。

运行：python -m scripts.replay
产物写入 fixtures/replay/，全部由固定时间戳的虚构事件生成，
重复运行得到逐字节一致的结果（可复现）。

覆盖：监护授权登记 → 普通波动只入趋势 → 夜间持续孤立排队
→ 自伤暗示升级与24小时真人接力 → 家长单次通知与重试幂等
→ 真人核实后人工否决降级 → 规则换版/模型换版 → 授权撤回
→ 夜间紧急30分钟接力 → 跨校转介 → 到期删除证明 → 审计校验。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.governance import Identity
from src.governance.rules import relaxed_pack
from tests.support import build_engine

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "fixtures" / "replay"

LIN = "pseudo-lin-13"
HE = "pseudo-he-14"
MATES = [f"pseudo-mate-{i}" for i in range(1, 6)]
SCHOOL2 = "school-linhai"
GUARDIAN_A = "guardian-7001"
GUARDIAN_B = "guardian-7002"
TEACHER_WANG = "staff-2001"
COUNSELOR_CHEN = "staff-3001"
PSYCH_ZHOU = "staff-4001"
NIGHT_PSYCH = "staff-4002"
REVIEWER = "reviewer-001"


def sig(pseudo, at, model="companion-7.2", **features):
    return {
        "pseudo": pseudo, "at": at, "model": model,
        "window": "evening", "features": features,
    }


def write(name, payload) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )


def replay() -> dict:
    engine = build_engine(k_anonymity=5, night_psych=True)

    # ---- 登记：学生与监护授权来自 fixtures/setup.json ----
    setup = json.loads((ROOT / "fixtures" / "setup.json").read_text(encoding="utf-8"))
    for row in setup["students"]:
        engine.identities.register(Identity(
            pseudo=row["pseudo"], school_id=row["school_id"],
            age=row["age"], grade=row["grade"], guardian_id=row["guardian_id"],
        ))
    for row in setup["consents"]:
        engine.consents.grant(row["pseudo"], row["guardian_id"], row["at"],
                              method=row["method"])
        engine.audit.record(row["at"], row["guardian_id"], "consent-granted",
                            row["pseudo"], {"scope": "ai-companion-care",
                                            "school": next(
                                                s["school_id"] for s in setup["students"]
                                                if s["pseudo"] == row["pseudo"])})

    # ---- 普通波动：小林九月上旬情绪平稳，只沉淀去标识趋势 ----
    normal_days = {
        "2026-09-02T20:00+08:00": (72, 18),
        "2026-09-03T20:00+08:00": (70, 22),
        "2026-09-04T20:00+08:00": (75, 15),
        "2026-09-07T20:00+08:00": (68, 25),
        "2026-09-08T20:00+08:00": (71, 20),
    }
    for at, (mood, iso) in normal_days.items():
        r = engine.ingest(sig(LIN, at, mood_score=mood, isolation_score=iso, good_moment=1))
        assert r.level == "normal" and r.actions == ("trend-only",)

    # 同班同学的普通信号：让年级趋势桶满足 k=5 匿名
    for i, pseudo in enumerate(MATES, start=2):
        engine.ingest(sig(pseudo, f"2026-09-{i:02d}T19:00+08:00",
                          mood_score=66 + i, isolation_score=30 + i))

    # ---- 持续孤立：第三次数值落在夜间，进入次日复核，不打扰任何人 ----
    engine.ingest(sig(LIN, "2026-09-10T19:00+08:00", isolation_score=80))
    engine.ingest(sig(LIN, "2026-09-11T19:00+08:00", isolation_score=80))
    night_watch = engine.ingest(sig(LIN, "2026-09-11T22:30+08:00", isolation_score=81))
    assert night_watch.level == "watch"
    lin_case_id = night_watch.case_id

    # 周一早晨复核，派给班主任做日常关心；班主任在早自习后完成一次现实接触
    routed = engine.process_night_reviews("2026-09-14T07:30+08:00")
    assert routed == [LIN]
    engine.record_human_contact(
        lin_case_id, TEACHER_WANG, "teacher", "in-person",
        "2026-09-14T08:10+08:00",
        "早自习后简短交谈，学生表示周末独自在家，已约同桌一起打球",
    )

    # ---- 周一下午出现自伤暗示：升级 urgent，24小时真人接力 ----
    urgent = engine.ingest(sig(LIN, "2026-09-14T15:00+08:00", self_harm_phrase=1))
    assert urgent.level == "urgent"
    urgent_key = urgent.notices[0]["key"]
    # 消息系统重试5次：家长只收到一次
    for _ in range(5):
        assert engine.retry_notice(urgent_key) == 1
    # 心理老师16:20完成面对面首次谈话，在24小时SLA内
    engine.record_human_contact(
        lin_case_id, COUNSELOR_CHEN, "counselor", "in-person",
        "2026-09-14T16:20+08:00",
        "学生主动说明是歌词创作；面谈情绪平稳，仍约定本周持续关心",
    )

    # ---- 人工否决：专职心理教师复核后降级为 watch（机器依据保留） ----
    overridden = engine.override_verdict(
        LIN, PSYCH_ZHOU, "psychologist", "watch",
        "复核：自伤词来自歌词创作，面对面谈话确认无自伤意图；保留周度关注",
        "2026-09-14T17:00+08:00",
    )
    assert overridden.source == "manual-override"
    # watch 新一轮：班主任在下一工作日中午前完成日常关心
    engine.record_human_contact(
        lin_case_id, TEACHER_WANG, "teacher", "in-person",
        "2026-09-15T10:00+08:00", "课间邀请加入社团招新，学生接受",
    )

    # ---- 另一名学生：夜间紧急，30分钟内真人接触 ----
    emergency = engine.ingest(sig(HE, "2026-09-16T23:05+08:00", self_harm_phrase=3))
    assert emergency.level == "emergency"
    he_case_id = emergency.case_id
    engine.record_human_contact(
        he_case_id, NIGHT_PSYCH, "psychologist", "voice-call",
        "2026-09-16T23:22+08:00", "夜间语音陪伴至情绪平复，约定次日家访",
    )
    engine.review_case(he_case_id, REVIEWER, "reviewer", "2026-09-17T09:00+08:00")

    # ---- 家长撤回授权：新信号被拒绝入域，留痕 ----
    engine.withdraw_consent(HE, GUARDIAN_B, "2026-09-18T08:00+08:00",
                            "家长希望先暂停AI陪伴")
    blocked = engine.ingest(sig(HE, "2026-09-18T20:00+08:00", mood_score=50))
    assert not blocked.accepted

    # ---- 规则换版 v2（9/20 生效）与模型换版（7.2 停用、7.3 启用） ----
    engine.publish_pack(relaxed_pack(), "2026-09-20T09:00+08:00", by="compliance-001")
    engine.retire_model("companion-7.2", "2026-09-25T00:00+08:00", by="ops-system")
    rejected_model = engine.ingest(sig(LIN, "2026-09-25T08:00+08:00",
                                       model="companion-7.2", mood_score=70))
    assert not rejected_model.accepted
    new_model = engine.ingest(sig(LIN, "2026-09-25T08:30+08:00",
                                  model="companion-7.3", mood_score=72, good_moment=1))
    assert new_model.accepted

    # ---- 跨校转介：小林随家搬迁，只移交安全结论，接收校重确认授权 ----
    referral = engine.make_referral(
        "ref-0007", LIN, SCHOOL2, COUNSELOR_CHEN, "2026-09-25T10:00+08:00",
    )
    engine.accept_referral("ref-0007", GUARDIAN_A, "2026-09-25T11:00+08:00")
    completed = engine.complete_referral(
        "ref-0007", "staff-5001", "2026-09-26T09:00+08:00",
    )
    new_case_id = completed.new_case_id
    engine.review_case(new_case_id, REVIEWER, "reviewer", "2026-09-26T09:30+08:00")

    # ---- 到期删除：90天保留期满（9/25+90天=12/24），9月信号全部物理擦除 ----
    certs = engine.sweep_retention("2026-12-26T00:00+08:00")
    assert certs and all(c["erased"] for c in certs)
    assert engine.history.for_(LIN) == [] and engine.history.for_(HE) == []

    # ---- 最终校验：哈希链完整 ----
    engine.verify_evidence()

    # ---- 产出样例文件 ----
    write("audit-chain.json", {
        "notice": "只追加哈希链；任一条目被改动，AuditLog.verify() 即失败。",
        "entries": engine.audit.export(),
    })
    write("case-review-lin.json", {
        "lin_case": engine.cases.get(lin_case_id).review_relay(),
        "transferred_case": engine.cases.get(new_case_id).review_relay(),
        "he_case": engine.cases.get(he_case_id).review_relay(),
    })
    write("verdicts-lin.json", engine.verdicts_for(LIN))
    write("guardian-view.json", {
        GUARDIAN_A: engine.guardian_view(GUARDIAN_A, "2026-09-26T10:00+08:00"),
        GUARDIAN_B: engine.guardian_view(GUARDIAN_B, "2026-09-26T10:00+08:00"),
    })
    write("student-visibility.json", engine.student_visibility())
    write("trends.json", {
        "notice": "仅输出满足 k=5 匿名的年级-周-指标桶，无化名、无片段。",
        "suppressed_buckets": engine.trends.suppressed_buckets(),
        "rows": engine.trends.export(),
    })
    write("referral.json", engine.referrals.get("ref-0007").as_dict())
    write("safety-summary-transferred.txt", referral.safety_summary)
    write("purge-certificates.json", certs)
    write("ingest-gates.json", {
        "withdrawn_consent_rejected": {
            "at": blocked.signal_at, "reason": blocked.reason,
        },
        "retired_model_rejected": {
            "at": rejected_model.signal_at, "reason": rejected_model.reason,
        },
    })

    summary = build_summary(engine, lin_case_id, he_case_id, new_case_id,
                            urgent_key, len(certs))
    write("summary.md", summary)
    return {"entries": len(engine.audit.entries), "certs": len(certs)}


def build_summary(engine, lin_case_id, he_case_id, new_case_id,
                  urgent_key, cert_count) -> str:
    lin = engine.cases.get(lin_case_id).review_relay()
    he = engine.cases.get(he_case_id).review_relay()
    transferred = engine.cases.get(new_case_id).review_relay()
    return f"""# 端到端回放说明（虚构样例）

所有人物、学校、账号均为虚构；系统内不存在任何对话原文，只有脱敏特征与结论。

## 学生小林（化名 {lin['pseudo']}）

1. 9月上旬普通情绪波动：只进入去标识化年级趋势（k=5 匿名桶），不开案件、不通知家长。
2. 9月11日 22:30 第三次持续孤立信号在夜间到达：排队至次日早晨复核，无人被深夜打扰。
3. 9月14日 15:00 出现自伤暗示，自动判定 `urgent`：
   - 依据：近3日 self_harm_phrase ≥ 1（规则 R-URG-01，规则包 risk-rules-v1）；
   - 16:20 心理老师完成面对面首次谈话，在24小时时限内闭合接力；
   - 家长只收到一次必要安全结论（去重键 `{urgent_key}`），投递层重试5次未产生第二条。
4. 17:00 专职心理教师**人工否决**降为 `watch`：机器依据保留，降级理由与责任人留痕；
   次日班主任完成日常关心。
5. 9月20日规则换版 v2，9月25日模型 7.2 停用：旧案件仍标注旧规则版本，旧模型新信号被拒绝。
6. 9月25-26日跨校转介：只移交安全结论，接收校重新确认监护授权，新校当日完成派单。

接力段审查：共 {len(lin['legs'])} 段，全部完成={lin['all_completed']}，全部在SLA内={lin['all_within_sla']}。

## 学生小何（化名 {he['pseudo']}）

- 9月16日 23:05 夜间紧急信号：夜间专职心理教师 23:22 语音接触，30分钟时限内闭合；
- 9月18日家长撤回授权，之后的信号一律拒绝入域并留痕。

接力段审查：全部在SLA内={he['all_within_sla']}。

## 数据到期删除

- 信号保留期 90 天：本次清扫出具 {cert_count} 份删除证明，
  每份含擦除前状态哈希，审计链中可核验 `data-purged` 事件。

## 学生视角

见 `student-visibility.json`：学生可逐条查看谁能看到哪些信息；
家长列下没有 signal_features / signal_snippets / rule_basis。

## 复核入口

审查人员可凭 `audit-chain.json` 的哈希链核对：
授权与撤回、每次判定依据、每次派单、每次真人接触（渠道/资质/时间）、
家长通知去重键、规则与模型换版、转介三阶段、到期删除证明。
"""


def main() -> None:
    stats = replay()
    print(f"回放完成：审计条目 {stats['entries']} 条，删除证明 {stats['certs']} 份")
    print(f"产物目录：{OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
