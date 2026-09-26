"""未成年人AI陪伴治理后台领域内核。

模块边界：
- 系统只接收已脱敏的会话信号，不持有任何对话原文；
- 自动判断产出可展示依据，人工可否决；
- 升级以“真实人际接触”为完成点，全程留哈希链证据。
"""

from .audit import AuditEntry, AuditLog, TamperDetected, digest
from .clock import TZ, fixed, now_iso, parse
from .consent import ConsentBook, ConsentRecord
from .identity import AccessGrant, ContactLocked, Identity, IdentityRegistry
from .schedule import DutyRoster, add_workdays, is_night, next_workday_noon, within_window
from .signals import Signal, SignalHistory, TrendStore, redact_signal
from .rules import LEVELS, RulePack, RulePackBook, RuleEngine, higher
from .triage import Verdict
from .cases import CareCase, CaseBook, NoDutyAvailable, Outbox, Router, render_digest
from .referral import ReferralBook
from .models import ModelRegistry, ModelVersion
from .schools import SchoolBook, School
from .retention import RetentionLedger, RetentionPolicy, RetentionSweeper
from .visibility import authorize, RESOURCES, ROLES, student_access_report
from .engine import GovernanceEngine, IngestResult

__all__ = [
    "AuditEntry", "AuditLog", "TamperDetected", "digest",
    "TZ", "fixed", "now_iso", "parse",
    "ConsentBook", "ConsentRecord",
    "AccessGrant", "ContactLocked", "Identity", "IdentityRegistry",
    "DutyRoster", "add_workdays", "is_night", "next_workday_noon", "within_window",
    "Signal", "SignalHistory", "TrendStore", "redact_signal",
    "LEVELS", "RulePack", "RulePackBook", "RuleEngine", "higher",
    "Verdict",
    "CareCase", "CaseBook", "NoDutyAvailable", "Outbox", "Router", "render_digest",
    "ReferralBook",
    "ModelRegistry", "ModelVersion",
    "SchoolBook", "School",
    "RetentionLedger", "RetentionPolicy", "RetentionSweeper",
    "authorize", "RESOURCES", "ROLES", "student_access_report",
    "GovernanceEngine", "IngestResult",
]
