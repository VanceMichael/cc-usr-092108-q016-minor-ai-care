"""登记处：学籍、监护授权、服务时段、模型版本、夜间值守。

后台一切判断都先查登记处：

- 没有有效监护授权的学生不产生任何信号处理；
- 信号落在服务时段之外时，按夜间值守规则路由；
- 每次评估记录当时生效的模型版本与规则版本，保证可回溯。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from src.governance.models import (
    GuardianConsent,
    ModelVersion,
    ServiceWindow,
    StudentRecord,
)


class RegistryError(ValueError):
    pass


class Registry:
    def __init__(self, *, school_id: str, night_duty_school_id: Optional[str] = None):
        self.school_id = school_id
        self._students: dict[str, StudentRecord] = {}
        self._models: dict[str, ModelVersion] = {}
        self._night_duty_school_id = night_duty_school_id

    # ---- 模型版本 ----

    def register_model(self, version: ModelVersion) -> None:
        if version.model_version in self._models:
            raise RegistryError(f"模型版本已登记：{version.model_version}")
        self._models[version.model_version] = version

    def retire_model(self, model_version: str, at: datetime) -> None:
        current = self._require_model(model_version)
        self._models[model_version] = ModelVersion(
            model_version=current.model_version,
            deployed_at=current.deployed_at,
            retired_at=at,
            notes=current.notes,
        )

    def active_model(self) -> ModelVersion:
        actives = [m for m in self._models.values() if m.active]
        if not actives:
            raise RegistryError("没有在用的模型版本")
        return max(actives, key=lambda m: m.deployed_at)

    def _require_model(self, model_version: str) -> ModelVersion:
        try:
            return self._models[model_version]
        except KeyError:
            raise RegistryError(f"未登记的模型版本：{model_version}") from None

    # ---- 学籍与监护授权 ----

    def enroll(self, record: StudentRecord) -> None:
        if record.student_id in self._students:
            raise RegistryError(f"学生已登记：{record.student_id}")
        self._require_model(record.model_version)
        if not record.consent.active:
            raise RegistryError("登记时监护授权必须有效")
        self._students[record.student_id] = record

    def student(self, student_id: str) -> StudentRecord:
        try:
            return self._students[student_id]
        except KeyError:
            raise RegistryError(f"未登记的学生：{student_id}") from None

    def revoke_consent(self, student_id: str, *, at: datetime, reason: str) -> StudentRecord:
        """撤回监护授权：返回更新后的学籍（授权标记为已撤回）。

        撤回后该学生的信号不再进入常规处理；撤回本身由调用方记入审计链。
        """
        record = self.student(student_id)
        revoked = GuardianConsent(
            guardian_id=record.consent.guardian_id,
            student_id=record.consent.student_id,
            scope=record.consent.scope,
            granted_at=record.consent.granted_at,
            valid_from=record.consent.valid_from,
            valid_until=record.consent.valid_until,
            revoked_at=at,
            revoke_reason=reason,
        )
        updated = StudentRecord(
            student_id=record.student_id,
            school_id=record.school_id,
            age=record.age,
            display_name=record.display_name,
            consent=revoked,
            enrolled_at=record.enrolled_at,
            service_window=record.service_window,
            model_version=record.model_version,
        )
        self._students[student_id] = updated
        return updated

    def consent_active(self, student_id: str, on_date: date) -> bool:
        record = self.student(student_id)
        return record.consent.active and record.consent.valid_from <= on_date and (
            record.consent.valid_until is None or on_date <= record.consent.valid_until
        )

    # ---- 服务时段与夜间值守 ----

    def is_night(self, student_id: str, moment: datetime) -> bool:
        return self.student(student_id).service_window.is_night(moment)

    @property
    def night_duty_school_id(self) -> Optional[str]:
        return self._night_duty_school_id
