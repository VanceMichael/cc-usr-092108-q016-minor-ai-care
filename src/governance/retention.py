"""留存策略：普通波动只留去标识化趋势，其余数据到期删除。

- 0级（普通波动）信号：不落个人明细，只按「学年周 × 年龄段 × 等级」聚合成
  TrendPoint，聚合完成后即丢弃信号本身；
- 1级及以上信号与工单：为完成人工接力而短期留存，到期必须删除，
  删除动作写审计链并签发可核验证书（谁、何时、删了什么类别、依据哪条策略）；
- 授权被撤回的学生：其留存数据立即进入删除队列，不等自然到期。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from src.governance.models import Signal, TrendPoint


def age_band(age: int) -> str:
    if age <= 9:
        return "6-9"
    if age <= 12:
        return "10-12"
    if age <= 15:
        return "13-15"
    return "16-17"


def week_bucket(moment: datetime) -> str:
    iso = moment.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@dataclass(frozen=True)
class RetentionPolicy:
    """各类数据的留存期限。"""

    signal_days: int = 30        # 1级及以上信号明细
    case_days: int = 180         # 已闭环工单
    trend_years: int = 3         # 去标识化趋势（聚合数据，无法还原到个人）


class TrendStore:
    """去标识化趋势：只进聚合，不出明细。"""

    def __init__(self) -> None:
        self._points: dict[tuple[str, str, int], int] = {}

    def absorb(self, *, moment: datetime, age: int, level: int) -> None:
        key = (week_bucket(moment), age_band(age), level)
        self._points[key] = self._points.get(key, 0) + 1

    def points(self) -> tuple[TrendPoint, ...]:
        return tuple(
            TrendPoint(bucket=b, age_band=a, level=lv, count=n)
            for (b, a, lv), n in sorted(self._points.items())
        )


@dataclass(frozen=True)
class DeletionRecord:
    """一次到期删除的事实记录（用于审计与证书）。"""

    category: str                # signal / case / trend
    subject: str                 # student_id 或 case_id
    deleted_at: datetime
    policy: str                  # 依据的留存策略描述
    count: int


class RetentionStore:
    """带到期时间的明细存储；到期即删，删除留痕。"""

    def __init__(self, policy: Optional[RetentionPolicy] = None):
        self.policy = policy or RetentionPolicy()
        self._signals: dict[str, tuple[Signal, date]] = {}   # signal_id -> (signal, 到期日)
        self._case_ids: dict[str, date] = {}                 # case_id -> 到期日
        self.deletions: list[DeletionRecord] = []

    # ---- 写入 ----

    def keep_signal(self, signal: Signal, *, stored_at: datetime) -> None:
        expiry = (stored_at + timedelta(days=self.policy.signal_days)).date()
        self._signals[signal.signal_id] = (signal, expiry)

    def keep_case(self, case_id: str, *, closed_at: datetime) -> None:
        self._case_ids[case_id] = (closed_at + timedelta(days=self.policy.case_days)).date()

    # ---- 读取 ----

    def signal(self, signal_id: str) -> Optional[Signal]:
        item = self._signals.get(signal_id)
        return item[0] if item else None

    def signals_of(self, student_id: str) -> tuple[Signal, ...]:
        return tuple(s for s, _ in self._signals.values() if s.student_id == student_id)

    def history_for(self, student_id: str, *, before: datetime, days: int) -> tuple[Signal, ...]:
        """规则引擎用的窗口内历史信号。"""
        cutoff = before - timedelta(days=days)
        kept = [
            s for s, _ in self._signals.values()
            if s.student_id == student_id and cutoff <= s.observed_at <= before
        ]
        return tuple(sorted(kept, key=lambda s: s.observed_at))

    @property
    def live_signal_ids(self) -> tuple[str, ...]:
        return tuple(self._signals.keys())

    @property
    def live_case_ids(self) -> tuple[str, ...]:
        return tuple(self._case_ids.keys())

    # ---- 到期删除 ----

    def purge_expired(self, *, today: date) -> list[DeletionRecord]:
        """删除所有到期数据，返回删除记录（调用方写审计 + 签证书）。"""
        records: list[DeletionRecord] = []

        expired_signals = [sid for sid, (_, exp) in self._signals.items() if exp <= today]
        by_student: dict[str, int] = {}
        for sid in expired_signals:
            signal, _ = self._signals.pop(sid)
            by_student[signal.student_id] = by_student.get(signal.student_id, 0) + 1
        for student_id, count in by_student.items():
            records.append(DeletionRecord(
                category="signal", subject=student_id, deleted_at=datetime.combine(today, datetime.min.time()),
                policy=f"信号明细留存{self.policy.signal_days}天", count=count,
            ))

        expired_cases = [cid for cid, exp in self._case_ids.items() if exp <= today]
        for cid in expired_cases:
            del self._case_ids[cid]
            records.append(DeletionRecord(
                category="case", subject=cid, deleted_at=datetime.combine(today, datetime.min.time()),
                policy=f"工单留存{self.policy.case_days}天", count=1,
            ))

        self.deletions.extend(records)
        return records

    def purge_student(self, student_id: str, *, at: datetime, reason: str) -> DeletionRecord:
        """授权撤回触发的立即删除。"""
        dropped = [sid for sid, (s, _) in self._signals.items() if s.student_id == student_id]
        for sid in dropped:
            del self._signals[sid]
        record = DeletionRecord(
            category="signal", subject=student_id, deleted_at=at,
            policy=f"授权撤回立即删除（{reason}）", count=len(dropped),
        )
        self.deletions.append(record)
        return record
