"""哈希链审计日志与可核验证书。

后台里每一件需要“留下可核验证明”的事——授权撤回、跨校转介、规则换版、
夜间值守交接、数据到期删除——都会：

1. 追加一条审计事件，事件包含前一条事件的哈希，形成不可篡改的链；
2. 需要对外证明时，签发一份证书（certificate），证书内容可独立校验：
   任何人拿到证书 + 审计链，都能重算哈希确认未被改动。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    kind: str                    # 事件类型，如 consent.revoked / rules.switched
    at: datetime
    actor: str                   # 操作者（人或系统组件）
    subject: str                 # 作用对象，如 student_id 或 rule_set id
    detail: dict[str, Any]
    prev_hash: str
    event_hash: str


@dataclass(frozen=True)
class Certificate:
    """对外可核验证明。verify 时重算 payload 哈希并与链上事件比对。"""

    cert_id: str
    kind: str
    issued_at: datetime
    payload: dict[str, Any]
    payload_hash: str
    chain_head: str              # 签发时审计链头哈希，证明证书落在哪条链上

    def verify(self, events: Iterable[AuditEvent]) -> bool:
        """校验：证书载荷哈希正确，且签发点之前（含）的审计链完整、确有对应事件。

        证书签发后链允许继续增长，因此只校验到 ``chain_head`` 为止的链前缀。
        """
        if hash_payload(self.payload) != self.payload_hash:
            return False
        prefix: list[AuditEvent] = []
        found = False
        for ev in events:
            prefix.append(ev)
            if ev.event_hash == self.chain_head:
                found = True
                break
        if not found:
            return False
        chain_ok, head = verify_chain(prefix)
        if not chain_ok or head != self.chain_head:
            return False
        return any(
            ev.kind == self.kind and hash_payload(ev.detail) == self.payload_hash
            for ev in prefix
        )


def verify_chain(events: Iterable[AuditEvent]) -> tuple[bool, str]:
    """校验整条链：序号连续、prev_hash 衔接、每条哈希自洽。返回 (是否完整, 链头哈希)。"""
    prev_hash = "GENESIS"
    head = "GENESIS"
    for expected_seq, ev in enumerate(events, start=1):
        if ev.seq != expected_seq:
            return False, head
        if ev.prev_hash != prev_hash:
            return False, head
        recomputed = hash_payload({
            "seq": ev.seq,
            "kind": ev.kind,
            "at": ev.at.isoformat(),
            "actor": ev.actor,
            "subject": ev.subject,
            "detail": ev.detail,
            "prev_hash": ev.prev_hash,
        })
        if recomputed != ev.event_hash:
            return False, head
        prev_hash = ev.event_hash
        head = ev.event_hash
    return True, head


class AuditLog:
    """只增不改的审计链。"""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._certs: list[Certificate] = []
        self._cert_seq = 0

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    @property
    def certificates(self) -> tuple[Certificate, ...]:
        return tuple(self._certs)

    @property
    def head(self) -> str:
        return self._events[-1].event_hash if self._events else "GENESIS"

    def append(self, *, kind: str, at: datetime, actor: str, subject: str,
               detail: dict[str, Any]) -> AuditEvent:
        seq = len(self._events) + 1
        prev = self.head
        event_hash = hash_payload({
            "seq": seq,
            "kind": kind,
            "at": at.isoformat(),
            "actor": actor,
            "subject": subject,
            "detail": detail,
            "prev_hash": prev,
        })
        event = AuditEvent(
            seq=seq, kind=kind, at=at, actor=actor, subject=subject,
            detail=dict(detail), prev_hash=prev, event_hash=event_hash,
        )
        self._events.append(event)
        return event

    def issue_certificate(self, *, kind: str, issued_at: datetime,
                          payload: dict[str, Any]) -> Certificate:
        """基于最近一次同类审计事件签发证书。"""
        self._cert_seq += 1
        cert = Certificate(
            cert_id=f"cert-{self._cert_seq:06d}",
            kind=kind,
            issued_at=issued_at,
            payload=dict(payload),
            payload_hash=hash_payload(payload),
            chain_head=self.head,
        )
        self._certs.append(cert)
        return cert

    def verify(self) -> bool:
        ok, _ = verify_chain(self._events)
        return ok

    def find(self, *, kind: Optional[str] = None,
             subject: Optional[str] = None) -> list[AuditEvent]:
        return [
            ev for ev in self._events
            if (kind is None or ev.kind == kind)
            and (subject is None or ev.subject == subject)
        ]
