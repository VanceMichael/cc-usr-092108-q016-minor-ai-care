"""学校与服务时段登记。

每所学校登记AI陪伴服务窗口（默认07:00-22:00）与夜间值守是否开通；
窗口外不开展AI陪伴对话，治理侧的夜间处置规则见 schedule 与 engine。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class School:
    school_id: str
    name: str
    service_start: str   # HH:MM
    service_end: str
    night_coverage: bool


class SchoolBook:
    def __init__(self) -> None:
        self._schools: dict[str, School] = {}

    def register(self, school: School) -> School:
        self._schools[school.school_id] = school
        return school

    def get(self, school_id: str) -> School:
        return self._schools[school_id]

    def has(self, school_id: str) -> bool:
        return school_id in self._schools
