"""跨校转介：随学生流动移交关怀关系，并留下双方可核验记录。

转介不搬运任何会话内容，只移交：化名、必要的安全结论、
未闭合接力段状态与规则版本；接收校须重新确认监护授权后方可继续。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

REQUESTED = "requested"
ACCEPTED = "accepted"
COMPLETED = "completed"


@dataclass(frozen=True)
class Referral:
    referral_id: str
    pseudo: str
    from_school_id: str
    to_school_id: str
    requested_at: str
    open_level: str
    rule_pack_version: str
    safety_summary: str
    consent_reconfirmed: bool
    accepted_at: str | None = None
    completed_at: str | None = None
    new_case_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "referral_id": self.referral_id,
            "pseudo": self.pseudo,
            "from_school_id": self.from_school_id,
            "to_school_id": self.to_school_id,
            "requested_at": self.requested_at,
            "open_level": self.open_level,
            "rule_pack_version": self.rule_pack_version,
            "safety_summary": self.safety_summary,
            "consent_reconfirmed": self.consent_reconfirmed,
            "accepted_at": self.accepted_at,
            "completed_at": self.completed_at,
            "new_case_id": self.new_case_id,
            "status": self.status,
        }

    @property
    def status(self) -> str:
        if self.completed_at:
            return COMPLETED
        if self.accepted_at:
            return ACCEPTED
        return REQUESTED


class ReferralBook:
    def __init__(self) -> None:
        self._rows: dict[str, Referral] = {}

    def request(self, referral: Referral) -> Referral:
        if not referral.safety_summary:
            raise ValueError("转介必须携带安全结论摘要")
        self._rows[referral.referral_id] = referral
        return referral

    def accept(self, referral_id: str, at: str) -> Referral:
        record = self._rows[referral_id]
        if not record.consent_reconfirmed:
            raise RuntimeError("接收校须先重新确认监护授权")
        accepted = Referral(
            **{**record.__dict__, "accepted_at": at}
        )
        self._rows[referral_id] = accepted
        return accepted

    def mark_consent_reconfirmed(self, referral_id: str) -> Referral:
        record = self._rows[referral_id]
        updated = Referral(**{**record.__dict__, "consent_reconfirmed": True})
        self._rows[referral_id] = updated
        return updated

    def complete(self, referral_id: str, at: str, new_case_id: str) -> Referral:
        record = self._rows[referral_id]
        if record.accepted_at is None:
            raise RuntimeError("转介尚未被接收校接受")
        completed = Referral(
            **{**record.__dict__, "completed_at": at, "new_case_id": new_case_id}
        )
        self._rows[referral_id] = completed
        return completed

    def get(self, referral_id: str) -> Referral:
        return self._rows[referral_id]
