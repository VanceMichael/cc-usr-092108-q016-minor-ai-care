"""监护授权登记。

授权与撤回都是带时间戳的记录；任一时点的授权状态由历史推导，
撤回后新信号一律拒绝入域（见 engine.ingest）。
"""

from __future__ import annotations

from dataclasses import dataclass

GRANTED = "granted"
WITHDRAWN = "withdrawn"
NONE = "none"


@dataclass(frozen=True)
class ConsentRecord:
    pseudo: str
    guardian_id: str
    scope: str
    state: str
    at: str
    method: str
    reason: str | None = None


class ConsentBook:
    def __init__(self) -> None:
        self._history: dict[str, list[ConsentRecord]] = {}

    def _append(self, record: ConsentRecord) -> ConsentRecord:
        self._history.setdefault(record.pseudo, []).append(record)
        return record

    def grant(
        self, pseudo: str, guardian_id: str, at: str,
        scope: str = "ai-companion-care", method: str = "signed-form-v1",
    ) -> ConsentRecord:
        return self._append(ConsentRecord(
            pseudo, guardian_id, scope, GRANTED, at, method,
        ))

    def grant_transfer(self, pseudo: str, guardian_id: str, at: str, referral_id: str) -> ConsentRecord:
        return self._append(ConsentRecord(
            pseudo, guardian_id, "ai-companion-care", GRANTED, at,
            method=f"school-transfer:{referral_id}",
        ))

    def withdraw(self, pseudo: str, guardian_id: str, at: str, reason: str) -> ConsentRecord:
        return self._append(ConsentRecord(
            pseudo, guardian_id, "ai-companion-care", WITHDRAWN, at,
            method="guardian-request", reason=reason,
        ))

    def state_at(self, pseudo: str, at: str) -> str:
        current = NONE
        for record in self._history.get(pseudo, []):
            if record.at <= at:
                current = record.state
        return current

    def history(self, pseudo: str) -> list[ConsentRecord]:
        return list(self._history.get(pseudo, []))
