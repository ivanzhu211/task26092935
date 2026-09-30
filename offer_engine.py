"""In-process offer候补时间推进引擎.

重演部门季度配额下的录用确认与超时补位。时间只能前进。
仅依赖标准库。
"""

from __future__ import annotations

from bisect import insort
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

CONFIRM_WINDOW = timedelta(hours=48)

# 候选人状态
WAITING = "waiting"      # 排队等候
PENDING = "pending"      # 已补位, 待确认
CONFIRMED = "confirmed"  # 已确认, 继续占名额
EXPIRED = "expired"      # 超时终态
WITHDRAWN = "withdrawn"  # 退出终态
TERMINAL = (EXPIRED, WITHDRAWN)

# 流转记录动作
ACT_ENQUEUE = "enqueued"
ACT_ACTIVATE = "activated"
ACT_CONFIRM = "confirmed"
ACT_WITHDRAW = "withdrawn"
ACT_EXPIRE = "expired"

QuarterKey = Tuple[int, int]  # (year, quarter) quarter in 1..4
QuotaKey = Tuple[str, int, int]  # (dept, year, quarter)


def quarter_of(t: datetime) -> QuarterKey:
    """返回 UTC 时刻所属的 (year, quarter)。"""
    return (t.year, (t.month - 1) // 3 + 1)


def quarter_start(key: QuarterKey) -> datetime:
    """季度起始 UTC 时刻。"""
    year, quarter = key
    return datetime(year, quarter * 3 - 2, 1, tzinfo=timezone.utc)


def next_quarter_boundary(t: datetime) -> datetime:
    """严格大于 t 的最近季度边界。"""
    key = quarter_of(t)
    start = quarter_start(key)
    if t < start:
        return start
    year, quarter = key
    if quarter == 4:
        return datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(year, quarter * 3 + 1, 1, tzinfo=timezone.utc)


@dataclass
class Event:
    """输入事件: time 为 UTC 时刻, action 为 enqueue/confirm/withdraw。"""

    time: datetime
    candidate: str
    action: str  # "enqueue" | "confirm" | "withdraw"
    dept: Optional[str] = None   # 仅 enqueue
    rank: Optional[int] = None   # 仅 enqueue, 数字越小越优先


@dataclass
class Record:
    """一条有序流转记录。"""

    time: datetime
    candidate: str
    action: str
    dept: Optional[str] = None
    quarter: Optional[QuarterKey] = None


@dataclass
class _Candidate:
    candidate: str
    dept: str
    rank: int
    state: str = WAITING
    quarter: Optional[QuarterKey] = None  # 激活时所属季度
    deadline: Optional[datetime] = None   # 待确认期限
    act_seq: int = 0                      # 激活序号, 用于同时刻固定排序


class OfferEngine:
    """进程内录用候补时间推进引擎。"""

    def __init__(self, start: datetime, quotas: Dict[Tuple[str, int, int], int]):
        self._now = self._as_utc(start)
        self._quotas: Dict[QuotaKey, int] = {}
        for key, value in quotas.items():
            if not isinstance(value, int) or value < 0:
                raise ValueError("quota 必须为非负整数")
            dept, year, quarter = key
            if not 1 <= quarter <= 4:
                raise ValueError("quarter 必须在 1..4")
            self._quotas[(dept, year, quarter)] = value
        self._used: Dict[QuotaKey, int] = {}
        self._candidates: Dict[str, _Candidate] = {}
        self._waiters: Dict[str, List[Tuple[int, str]]] = {}
        self._records: List[Record] = []
        self._act_seq = 0

    @staticmethod
    def _as_utc(t: datetime) -> datetime:
        if not isinstance(t, datetime) or t.tzinfo is None:
            raise ValueError("时刻必须为带时区的 datetime")
        return t.astimezone(timezone.utc)

    def _check_time(self, t: datetime) -> datetime:
        t = self._as_utc(t)
        if t < self._now:
            raise ValueError("时间只能前进")
        return t

    # ---- 自动流转 ----

    def _next_event_time(self) -> Optional[datetime]:
        deadline = None
        for cand in self._candidates.values():
            if cand.state == PENDING and cand.deadline is not None:
                if deadline is None or cand.deadline < deadline:
                    deadline = cand.deadline
        boundary = None
        if any(self._waiters.values()):
            boundary = next_quarter_boundary(self._now)
        if deadline is None:
            return boundary
        if boundary is None:
            return deadline
        return min(deadline, boundary)

    def _expire_due(self, t: datetime, out: List[Record]) -> None:
        due = [
            cand
            for cand in self._candidates.values()
            if cand.state == PENDING and cand.deadline is not None and cand.deadline <= t
        ]
        # 固定顺序: 先按期限, 再按激活序号, 再按候选人编号
        due.sort(key=lambda c: (c.deadline, c.act_seq, c.candidate))
        for cand in due:
            cand.state = EXPIRED
            key = (cand.dept, cand.quarter[0], cand.quarter[1])
            self._used[key] -= 1
            cand.deadline = None
            out.append(Record(t, cand.candidate, ACT_EXPIRE, cand.dept, cand.quarter))

    def _fill(self, t: datetime, out: List[Record]) -> None:
        # 固定顺序: 部门按字典序, 部门内按 (rank, 候选人编号)
        quarter = quarter_of(t)
        for dept in sorted(self._waiters):
            queue = self._waiters[dept]
            key = (dept, quarter[0], quarter[1])
            quota = self._quotas.get(key, 0)
            while queue and self._used.get(key, 0) < quota:
                _, cid = queue.pop(0)
                cand = self._candidates[cid]
                cand.state = PENDING
                cand.quarter = quarter
                cand.deadline = t + CONFIRM_WINDOW
                cand.act_seq = self._act_seq
                self._act_seq += 1
                self._used[key] = self._used.get(key, 0) + 1
                out.append(Record(t, cid, ACT_ACTIVATE, dept, quarter))

    def _run_auto(self, t: datetime, out: List[Record]) -> None:
        # 同一时刻先处理全部到期, 再按该时刻所属季度补位
        self._expire_due(t, out)
        self._fill(t, out)

    # ---- 对外操作 ----

    def apply(self, event: Event) -> List[Record]:
        """先推进到事件时刻, 再处理输入动作, 返回本次新增流转记录。"""
        auto = self.advance(event.time)
        new = list(auto)
        t = self._now
        if event.action == "enqueue":
            if event.candidate in self._candidates:
                raise ValueError("候选人编号全局唯一, 每人仅入队一次")
            if event.dept is None or event.rank is None:
                raise ValueError("enqueue 需要 dept 与 rank")
            cand = _Candidate(event.candidate, event.dept, event.rank)
            self._candidates[event.candidate] = cand
            insort(self._waiters.setdefault(event.dept, []), (event.rank, event.candidate))
            new.append(Record(t, event.candidate, ACT_ENQUEUE, event.dept))
        elif event.action == "confirm":
            cand = self._candidates.get(event.candidate)
            if cand is not None and cand.state == PENDING:
                cand.state = CONFIRMED
                cand.deadline = None
                new.append(Record(t, event.candidate, ACT_CONFIRM, cand.dept, cand.quarter))
        elif event.action == "withdraw":
            cand = self._candidates.get(event.candidate)
            if cand is not None and cand.state not in TERMINAL:
                if cand.state == WAITING:
                    self._waiters[cand.dept].remove((cand.rank, cand.candidate))
                else:
                    key = (cand.dept, cand.quarter[0], cand.quarter[1])
                    self._used[key] -= 1
                    cand.deadline = None
                cand.state = WITHDRAWN
                new.append(Record(t, event.candidate, ACT_WITHDRAW, cand.dept, cand.quarter))
        else:
            raise ValueError("未知动作: %r" % (event.action,))
        # 每次动作后立即补位
        self._fill(t, new)
        self._records.extend(new[len(auto):])
        return new

    def advance(self, to: datetime) -> List[Record]:
        """执行截至 to 的全部自动流转, 返回本次新增流转记录。"""
        to = self._check_time(to)
        new: List[Record] = []
        while True:
            t_next = self._next_event_time()
            if t_next is None or t_next > to:
                break
            self._now = t_next
            self._run_auto(t_next, new)
        self._now = to
        self._records.extend(new)
        return new

    def snapshot(self) -> dict:
        """返回当前候选人状态及配额占用。"""
        keys = set(self._quotas) | set(self._used)
        return {
            "now": self._now,
            "candidates": {
                cid: {
                    "state": cand.state,
                    "dept": cand.dept,
                    "rank": cand.rank,
                    "quarter": cand.quarter,
                    "deadline": cand.deadline,
                }
                for cid, cand in self._candidates.items()
            },
            "quotas": {
                key: {
                    "quota": self._quotas.get(key, 0),
                    "used": self._used.get(key, 0),
                }
                for key in sorted(keys)
            },
        }
