"""组装一个接好线的治理引擎，供测试与回放共用。"""

from src.governance import (
    AuditLog, ConsentBook, DutyRoster, GovernanceEngine, Identity,
    IdentityRegistry, Outbox, CaseBook, ReferralBook, RetentionLedger,
    RulePackBook, SignalHistory, TrendStore,
)
from src.governance.models import ModelRegistry
from src.governance.schools import SchoolBook, School
from src.governance.rules import default_pack, relaxed_pack
from src.governance.schedule import DutySlot


SCHOOL = "school-fenghe"
SCHOOL_NAME = "丰和中学（虚构）"
SCHOOL2 = "school-linhai"
GUARDIAN_A = "guardian-7001"
GUARDIAN_B = "guardian-7002"
TEACHER_WANG = DutySlot("staff-2001", "王老师", "teacher", can_unmask=False)
COUNSELOR_CHEN = DutySlot("staff-3001", "陈心理老师", "counselor", can_unmask=True)
PSYCH_ZHOU = DutySlot("staff-4001", "周专职心理教师", "psychologist", can_unmask=True)
NIGHT_DUTY = DutySlot("staff-3002", "夜班心理老师", "counselor", can_unmask=True)
NIGHT_PSYCH = DutySlot("staff-4002", "夜间专职心理教师", "psychologist", can_unmask=True)
MODEL_72 = "companion-7.2"
MODEL_73 = "companion-7.3"


def build_engine(k_anonymity: int = 5, night_psych: bool = False,
                 register_models: bool = True):
    audit = AuditLog()
    identities = IdentityRegistry()
    consents = ConsentBook()
    history = SignalHistory()
    trends = TrendStore(k=k_anonymity)
    packs = RulePackBook()
    packs.publish(default_pack())
    roster = DutyRoster()
    roster.set_day(SCHOOL, COUNSELOR_CHEN)
    roster.set_day(SCHOOL, PSYCH_ZHOU)
    roster.set_day(SCHOOL, TEACHER_WANG)
    roster.set_night(SCHOOL, NIGHT_DUTY)
    if night_psych:
        roster.set_night(SCHOOL, NIGHT_PSYCH)
    roster.set_day(SCHOOL2, TEACHER_WANG)
    cases = CaseBook()
    outbox = Outbox()
    referrals = ReferralBook()
    retention = RetentionLedger()
    models = ModelRegistry()
    if register_models:
        models.register(MODEL_72, "extractor-3.1", "2026-08-01T00:00+08:00")
        models.register(MODEL_73, "extractor-3.2", "2026-09-15T00:00+08:00")
    schools = SchoolBook()
    schools.register(School(SCHOOL, SCHOOL_NAME, "07:00", "22:00", night_coverage=True))
    schools.register(School(SCHOOL2, "临海中学（虚构）", "07:00", "22:00", night_coverage=False))
    engine = GovernanceEngine(
        audit=audit, identities=identities, consents=consents,
        history=history, trends=trends, packs=packs,
        active_pack="risk-rules-v1", roster=roster, cases=cases,
        outbox=outbox, referrals=referrals, retention=retention,
        models=models, schools=schools,
    )
    return engine


def register_student(engine, pseudo="pseudo-lin-13", age=13, grade="初一",
                     guardian=GUARDIAN_A, school=SCHOOL, at="2026-09-01T09:00+08:00"):
    engine.identities.register(Identity(pseudo, school, age, grade, guardian))
    engine.consents.grant(pseudo, guardian, at)
    engine.audit.record(at, guardian, "consent-granted", pseudo,
                        {"scope": "ai-companion-care", "school": school})
    return pseudo
