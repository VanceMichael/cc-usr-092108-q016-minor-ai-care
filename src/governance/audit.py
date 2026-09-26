"""只追加、可校验的审计哈希链。

每个状态变更动作都在此留痕；证据中只出现脱敏特征编号与结论，
不出现会话原文。任何对历史条目的改动都会在 verify() 暴露。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

GENESIS = "GENESIS"


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    at: str
    actor: str
    action: str
    subject: str
    evidence: dict[str, Any]
    prev_hash: str
    hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "actor": self.actor,
            "action": self.action,
            "subject": self.subject,
            "evidence": self.evidence,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


class TamperDetected(RuntimeError):
    pass


class AuditLog:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(
        self,
        at: str,
        actor: str,
        action: str,
        subject: str,
        evidence: dict[str, Any] | None = None,
    ) -> AuditEntry:
        prev_hash = self.entries[-1].hash if self.entries else GENESIS
        seq = len(self.entries) + 1
        body = {
            "seq": seq,
            "at": at,
            "actor": actor,
            "action": action,
            "subject": subject,
            "evidence": evidence or {},
            "prev_hash": prev_hash,
        }
        entry = AuditEntry(
            seq=seq,
            at=at,
            actor=actor,
            action=action,
            subject=subject,
            evidence=body["evidence"],
            prev_hash=prev_hash,
            hash=digest(body),
        )
        self.entries.append(entry)
        return entry

    def verify(self) -> None:
        prev_hash = GENESIS
        for entry in self.entries:
            if entry.prev_hash != prev_hash:
                raise TamperDetected(f"审计链在第 {entry.seq} 条断裂")
            body = {
                "seq": entry.seq,
                "at": entry.at,
                "actor": entry.actor,
                "action": entry.action,
                "subject": entry.subject,
                "evidence": entry.evidence,
                "prev_hash": entry.prev_hash,
            }
            if digest(body) != entry.hash:
                raise TamperDetected(f"审计链第 {entry.seq} 条内容被改动")
            prev_hash = entry.hash

    def export(self) -> list[dict[str, Any]]:
        return [entry.as_dict() for entry in self.entries]

    @classmethod
    def load(cls, rows: list[dict[str, Any]]) -> "AuditLog":
        log = cls()
        for row in rows:
            log.entries.append(AuditEntry(**row))
        log.verify()
        return log
