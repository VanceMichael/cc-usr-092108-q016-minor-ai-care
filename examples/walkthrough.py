"""端到端走查（全部为虚构数据）。

运行：python -m examples.walkthrough

故事线：
- 学生“小禾”只愿意向AI倾诉；心理老师、监护人、夜间值守人员按等级被接进来；
- 普通波动只留趋势；持续孤立开1级工单；夜间自伤暗示走2级与人工审核；
  迫在眉睫伤害立即幂等紧急联络；
- 演示人工否决、消息重试不重复、授权撤回、跨校转介、规则换版、到期删除证书。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from src.governance import GovernanceBackend
from src.governance.escalation import RecordingNotifier
from src.governance.models import (
    GuardianConsent,
    HumanContact,
    ModelVersion,
    ServiceWindow,
    StudentRecord,
)
from src.governance.rules import RuleSet
from src.governance.views import GuardianView, ProfessionalView, StudentView, TrendView


class FakeStaff:
    """虚构的有资质人员名录。"""

    def qualified_staff(self, *, level: int, night: bool) -> list[str]:
        if night:
            return ["staff-night-li"]
        if level >= 3:
            return ["staff-crisis-wang"]
        if level == 2:
            return ["staff-counselor-zhao"]
        return ["staff-careteacher-chen"]

    def qualification_of(self, staff_id: str) -> str:
        return {
            "staff-night-li": "夜间值守持证心理教师",
            "staff-crisis-wang": "危机干预资质社工",
            "staff-counselor-zhao": "中小学心理教师（中级）",
            "staff-careteacher-chen": "德育关怀教师",
        }.get(staff_id, "未登记")


def main() -> None:
    base = datetime(2026, 9, 21, 20, 0, 0)
    clock = {"now": base}

    def now() -> datetime:
        return clock["now"]

    notifier = RecordingNotifier()
    backend = GovernanceBackend(
        school_id="school-fuxing-01",
        staff=FakeStaff(),
        clock=now,
        notifier=notifier,
        night_duty_school_id="school-district-night-duty",
        guardian_contact=lambda sid: f"guardian.li@example.test",
    )

    # 登记模型版本与规则集
    backend.register_model(ModelVersion(
        model_version="companion-q3-2026", deployed_at=datetime(2026, 9, 1)))
    v1 = RuleSet.load(__import__("pathlib").Path("fixtures/rules_v1.json"))
    cert_rules = backend.load_rules(v1, actor="principal-admin")
    print("【规则换版证书】", cert_rules.cert_id, cert_rules.payload_hash[:12])

    # 登记学生与监护授权：监护人只被授权接收 safety_conclusion
    consent = GuardianConsent(
        guardian_id="guardian-li", student_id="stu-2026-0117",
        scope=("safety_conclusion",),
        granted_at=base - timedelta(days=30),
        valid_from=date(2026, 8, 20),
    )
    student = StudentRecord(
        student_id="stu-2026-0117", school_id="school-fuxing-01", age=14,
        display_name="小禾", consent=consent, enrolled_at=base - timedelta(days=20),
        service_window=ServiceWindow(7, 22), model_version="companion-q3-2026",
    )
    backend.enroll(student)

    print("\n【学生视图：谁能看到什么】")
    for item in StudentView(student).visibility():
        print(f"  - {item.data} → {item.visible_to}（{item.purpose}）")

    # 1) 普通波动：脱敏后0级，只进趋势
    clock["now"] = datetime(2026, 9, 21, 20, 0)
    _, assess, case = backend.ingest(
        student_id="stu-2026-0117",
        raw_text="今天数学考砸了有点烦，我电话13800138000别告诉别人。",
        features=frozenset({"negative_emotion"}),
        signal_id="sig-1", observed_at=clock["now"],
    )
    print("\n【普通波动】等级", assess.level, "工单:", case)

    # 2) 持续孤立：学生连续多日只向AI倾诉。
    #    前几日的普通波动在陪伴端本地累计、不进后台；第3天跨阈值后才上传一条
    #    脱敏信号，携带 isolation_days_3+ 特征（窗口明细永不出设备）。
    clock["now"] = datetime(2026, 9, 7, 20, 30)
    _, assess, case = backend.ingest(
        student_id="stu-2026-0117",
        raw_text="在学校没人理我，只有你懂我，我不想去学校。",
        features=frozenset({"ai_primary_attachment", "negative_emotion", "isolation_days_3+"}),
        signal_id="sig-isolate-crossed", observed_at=clock["now"],
    )
    print("【持续孤立】等级", assess.level, "工单", case.case_id,
          "承办", case.assignee_id, "时限", case.deadline.isoformat())
    print(assess.explain())

    # 关怀教师完成真实人际接触并关闭
    contact_t = clock["now"] + timedelta(hours=20)
    backend.acknowledge(case.case_id, staff_id="staff-careteacher-chen")
    backend.record_human_contact(case.case_id, HumanContact(
        channel="face_to_face", staff_id="staff-careteacher-chen",
        staff_qualification="德育关怀教师", contacted_at=contact_t,
        note="课间当面谈心，学生愿意继续参加社团",
    ))
    backend.close_case(case.case_id, staff_id="staff-careteacher-chen", note="已恢复两项现实社交活动")

    # 3) 夜间自伤暗示 → 2级：夜间值守 + 接单审核后才通知监护人
    clock["now"] = datetime(2026, 9, 15, 23, 15)
    _, assess, night_case = backend.ingest(
        student_id="stu-2026-0117",
        raw_text="我有时候真的觉得消失就好了，撑不下去……",
        features=frozenset({"self_harm_keyphrase", "negative_emotion"}),
        signal_id="sig-night-1", observed_at=clock["now"],
    )
    print("\n【夜间自伤暗示】等级", assess.level, "夜间路由:", night_case.night_routed,
          "承办:", night_case.assignee_id)
    print("接单前监护人通知数:", len(notifier.sent))
    backend.acknowledge(night_case.case_id, staff_id="staff-night-li")
    print("夜间值守接单审核后通知数:", len(notifier.sent),
          "->", notifier.sent[-1]["subject"])

    # 4) 迫在眉睫伤害 → 3级：紧急联络立即发出，重试不重复
    clock["now"] = datetime(2026, 9, 16, 1, 5)
    _, assess, crisis = backend.ingest(
        student_id="stu-2026-0117",
        raw_text="再见了，这是最后一次跟你说话，今晚我已经准备好。",
        features=frozenset({"self_harm_keyphrase", "imminent_plan", "means_available"}),
        signal_id="sig-crisis-1", observed_at=clock["now"],
    )
    print("\n【迫在眉睫】等级", assess.level, "通知条数:", len(notifier.sent))

    # 模拟消息重试：补偿任务重复投递，同一条紧急联络不会发出第二次
    print("重试补偿又真实发出了吗:", backend.pipeline.retry_emergency_contact(crisis.case_id),
          "/", backend.pipeline.retry_emergency_contact(crisis.case_id))
    print("重试两次后紧急联络去重键唯一:",
          len({n["dedupe_key"] for n in notifier.sent if n["dedupe_key"].startswith("emergency:")}) == 1,
          "（去重键:", "emergency:" + crisis.case_id, "）")

    backend.acknowledge(crisis.case_id, staff_id="staff-crisis-wang")
    backend.record_human_contact(crisis.case_id, HumanContact(
        channel="face_to_face", staff_id="staff-crisis-wang",
        staff_qualification="危机干预资质社工",
        contacted_at=clock["now"] + timedelta(minutes=9),
        note="已当面陪护并联系监护人到场",
    ))
    backend.close_case(crisis.case_id, staff_id="staff-crisis-wang", note="危机解除，转持续关怀")

    # 5) 人工否决演示：新的一条2级信号，专业人员判定为误报并否决
    clock["now"] = datetime(2026, 9, 17, 18, 0)
    _, assess, maybe = backend.ingest(
        student_id="stu-2026-0117",
        raw_text="我写的小说主角说他撑不下去，这一段台词我练了好几遍。",
        features=frozenset({"self_harm_keyphrase", "negative_emotion"}),
        signal_id="sig-context-1", observed_at=clock["now"],
    )
    before = len(notifier.sent)
    backend.override_case(maybe.case_id, staff_id="staff-counselor-zhao",
                          reason="经核对上下文为学生创作小说台词，非自伤表达，已与本人确认",
                          new_level=0)
    print("\n【人工否决】工单", maybe.case_id, "-> overridden；否决前后监护人通知数:",
          before, "->", len(notifier.sent))

    # 三视图
    print("\n【监护人视图】只看得到结论：")
    for row in GuardianView(pipeline=backend.pipeline).conclusions_for("stu-2026-0117"):
        print(f"  - [{row['level']}] {row['headline']}｜行动：{row['action_taken']}")

    print("\n【专业人员视图·危机工单审查】")
    review = ProfessionalView(pipeline=backend.pipeline, engine=backend.engine)
    report = review.case_review(crisis.case_id)
    print("  真实人际接力完成:", report["relay_complete"],
          "｜时限内:", report["timely"], "｜接触方式:", report["human_contact"]["channel"])
    print("  判断依据:\n    " + report["assessment_explanation"].replace("\n", "\n    "))

    print("\n【去标识化趋势】（无个人标识）")
    for row in TrendView(backend.trends.points()).rows():
        print(" ", row)

    # 规则换版 v2
    v2 = RuleSet.load(__import__("pathlib").Path("fixtures/rules_v2.json"))
    backend.load_rules(v2, actor="principal-admin")
    print("\n【规则换版】当前规则集:", backend.engine.version)

    # 跨校转介
    referral = backend.refer_student(
        "stu-2026-0117", to_school_id="school-heping-02", reason="家庭搬迁转学")
    print("【跨校转介证书】", referral.cert_id,
          "转出内容:", referral.payload["transferred_content"],
          "｜未闭环工单数:", len(referral.payload["open_cases"]))

    # 授权撤回：立即删除并出证
    revoked = backend.revoke_consent("stu-2026-0117", reason="监护人书面要求停止使用AI陪伴")
    print("【授权撤回证书】", revoked.cert_id, "删除信号数:", revoked.payload["signals_deleted"])
    try:
        backend.ingest(
            student_id="stu-2026-0117", raw_text="撤回后不应被处理",
            features=frozenset(), signal_id="sig-after-revoke",
            observed_at=clock["now"] + timedelta(hours=1),
        )
    except PermissionError as exc:
        print("撤回后信号处理被拒绝:", exc)

    # 到期删除
    certs = backend.run_retention(today=date(2027, 4, 1))
    print("【到期删除证书】共", len(certs), "份；示例:",
          certs[0].payload["category"], certs[0].payload["policy"])

    # 全链路证明校验
    print("\n【可核验性】审计链完整且全部证书校验通过:", backend.verify_proofs())
    print("审计事件数:", len(backend.audit.events), "｜证书数:", len(backend.audit.certificates))


if __name__ == "__main__":
    main()
