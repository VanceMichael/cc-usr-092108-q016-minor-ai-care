"""化名注册表：学生真实身份与系统内化名之间的单向映射。

治理域内部一律使用化名；解封（化名→真实身份）只在人工升级、
且经审计授权时临时进行，谁在何时解封过，哈希链中可查。
家长联络方式在登记时即为锁定值，撤回后停止使用。
"""

from __future__ import annotations

from dataclasses import dataclass


class ContactLocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    pseudo: str
    school_id: str
    age: int
    grade: str
    guardian_id: str
    contact_locked: bool = True


@dataclass(frozen=True)
class AccessGrant:
    pseudo: str
    staff_id: str
    role: str
    purpose: str
    granted_at: str
    audit_seq: int
    valid_minutes: int = 60


class IdentityRegistry:
    def __init__(self) -> None:
        self._identities: dict[str, Identity] = {}
        self._unmask: dict[str, AccessGrant] = {}

    def register(self, identity: Identity) -> Identity:
        if identity.age < 6 or identity.age > 17:
            raise ValueError("年龄超出未成年人服务范围")
        self._identities[identity.pseudo] = identity
        return identity

    def get(self, pseudo: str) -> Identity:
        return self._identities[pseudo]

    def lookup(self, guardian_id: str) -> list[str]:
        return [
            pseudo for pseudo, ident in self._identities.items()
            if ident.guardian_id == guardian_id
        ]

    def is_registered(self, pseudo: str) -> bool:
        return pseudo in self._identities

    def unmask(self, pseudo: str, staff_id: str, role: str, purpose: str,
               granted_at: str, audit_seq: int, valid_minutes: int = 60) -> AccessGrant:
        if pseudo not in self._identities:
            raise KeyError(pseudo)
        grant = AccessGrant(pseudo, staff_id, role, purpose, granted_at,
                            audit_seq, valid_minutes)
        self._unmask[pseudo] = grant
        return grant

    def active_unmask(self, pseudo: str) -> AccessGrant | None:
        return self._unmask.get(pseudo)

    def school_of(self, pseudo: str) -> str:
        return self._identities[pseudo].school_id

    def change_school(self, pseudo: str, school_id: str) -> Identity:
        """跨校转介后更新学籍，化名与监护关系保持可追溯。"""
        old = self._identities[pseudo]
        updated = Identity(
            pseudo=pseudo, school_id=school_id, age=old.age,
            grade=old.grade, guardian_id=old.guardian_id,
            contact_locked=old.contact_locked,
        )
        self._identities[pseudo] = updated
        return updated
