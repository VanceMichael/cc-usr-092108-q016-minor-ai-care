"""固定时区与可注入时钟，保证样例回放可复现。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))


def parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def now_iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat(timespec="minutes")


class FixedClock:
    """测试与回放用时钟，只显式前进。"""

    def __init__(self, value: str | datetime):
        self.now = parse(value) if isinstance(value, str) else value

    def tick(self, minutes: int = 0, days: int = 0) -> datetime:
        self.now += timedelta(minutes=minutes, days=days)
        return self.now

    def iso(self) -> str:
        return now_iso(self.now)


def fixed(value: str) -> FixedClock:
    return FixedClock(value)
