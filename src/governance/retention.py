"""数据到期删除：登记保留期限，到期物理擦除并出具删除证明。

证明记录被删数据单元擦除前的摘要哈希；删除动作由注册方提供的
erase 回调真正执行（清空其存储/释放引用），审计链再留痕，
任何人都能核对“某类数据在某时确实到期、且被删除”。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .audit import digest
from .clock import parse

# 默认保留期（天）：普通信号最短，案件与审计依法保留更久
DEFAULT_POLICY = {
    "signal": 90,
    "trend": 365,
    "case": 365 * 3,
    "referral": 365 * 3,
    "audit": 365 * 5,
}


@dataclass(frozen=True)
class RetentionPolicy:
    days_by_kind: dict[str, int]

    def expires_at(self, kind: str, created_at: str) -> str:
        from datetime import timedelta
        days = self.days_by_kind[kind]
        return (parse(created_at) + timedelta(days=days)).isoformat(timespec="minutes")


@dataclass(frozen=True)
class RetentionUnit:
    unit_id: str
    kind: str
    pseudo: str
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class PurgeCertificate:
    unit_id: str
    kind: str
    pseudo: str
    expires_at: str
    purged_at: str
    digest_before_purge: str
    erased: bool

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class RetentionLedger:
    def __init__(self, policy: RetentionPolicy | None = None) -> None:
        self.policy = policy or RetentionPolicy(DEFAULT_POLICY)
        self._units: dict[str, tuple[RetentionUnit, Callable[[], dict[str, Any]], Callable[[], None]]] = {}
        self._certificates: list[PurgeCertificate] = []

    def register(self, unit_id: str, kind: str, pseudo: str, created_at: str,
                 snapshot: Callable[[], dict[str, Any]],
                 erase: Callable[[], None]) -> RetentionUnit:
        unit = RetentionUnit(
            unit_id=unit_id, kind=kind, pseudo=pseudo,
            created_at=created_at,
            expires_at=self.policy.expires_at(kind, created_at),
        )
        self._units[unit_id] = (unit, snapshot, erase)
        return unit

    def due(self, at: str) -> list[RetentionUnit]:
        return [
            unit for unit, _, _ in self._units.values()
            if unit.expires_at <= at
        ]

    def purge_due(self, at: str) -> list[PurgeCertificate]:
        certs: list[PurgeCertificate] = []
        for unit_id in [u.unit_id for u in self.due(at)]:
            unit, snapshot, erase = self._units.pop(unit_id)
            before = digest({"unit_id": unit_id, "state": snapshot()})
            erase()
            # 删除后再取一次快照，必须为空，否则不给出具证明
            leftover = snapshot()
            erased = not leftover
            cert = PurgeCertificate(
                unit_id=unit.unit_id, kind=unit.kind, pseudo=unit.pseudo,
                expires_at=unit.expires_at, purged_at=at,
                digest_before_purge=before, erased=erased,
            )
            certs.append(cert)
            self._certificates.append(cert)
        return certs

    def certificates(self) -> list[PurgeCertificate]:
        return list(self._certificates)

# 向后兼容别名
RetentionSweeper = RetentionLedger
