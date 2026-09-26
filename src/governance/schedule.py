"""服务时段与夜间值守名册。

服务窗口之外不提供AI陪伴；夜间（22:00-次日7:00）信号不直接进入
常规对话，而是进入夜间值守队列，由值班心理老师按规则应答。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .clock import TZ, parse

NIGHT_START = time(22, 0)
NIGHT_END = time(7, 0)


def is_night(at: str | datetime) -> bool:
    dt = at if isinstance(at, datetime) else parse(at)
    t = dt.astimezone(TZ).time()
    return t >= NIGHT_START or t < NIGHT_END


def within_window(at: str | datetime, start: str = "07:00", end: str = "22:00") -> bool:
    dt = at if isinstance(at, datetime) else parse(at)
    t = dt.astimezone(TZ).time()
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    return time(sh, sm) <= t < time(eh, em)


def next_workday_noon(at: str | datetime, holidays: frozenset[str] = frozenset()) -> str:
    dt = (at if isinstance(at, datetime) else parse(at)).astimezone(TZ)
    candidate = (dt + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    while candidate.weekday() >= 5 or candidate.date().isoformat() in holidays:
        candidate += timedelta(days=1)
    return candidate.isoformat(timespec="minutes")


def add_workdays(at: str | datetime, days: int, holidays: frozenset[str] = frozenset()) -> str:
    dt = (at if isinstance(at, datetime) else parse(at)).astimezone(TZ)
    remaining = days
    while remaining > 0:
        dt += timedelta(days=1)
        if dt.weekday() < 5 and dt.date().isoformat() not in holidays:
            remaining -= 1
    return dt.isoformat(timespec="minutes")


@dataclass(frozen=True)
class DutySlot:
    staff_id: str
    name: str
    qualification: str
    can_unmask: bool


class DutyRoster:
    """学校 -> 时段 -> 值班人员列表（同校可同时有教师/心理老师/专职心理教师）。"""

    def __init__(self) -> None:
        self._day: dict[str, list[DutySlot]] = {}
        self._night: dict[str, list[DutySlot]] = {}

    def set_day(self, school_id: str, slot: DutySlot) -> None:
        self._day.setdefault(school_id, []).append(slot)

    def set_night(self, school_id: str, slot: DutySlot) -> None:
        self._night.setdefault(school_id, []).append(slot)

    def slots_on_duty(self, school_id: str, at: str | datetime) -> tuple[DutySlot, ...]:
        table = self._night if is_night(at) else self._day
        return tuple(table.get(school_id, ()))
