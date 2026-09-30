"""进程内录用候补时间推进引擎。

重演部门季度配额下的录用确认与超时补位。仅依赖标准库，时间只能前进。

内部流转的固定顺序（同一逻辑时刻内）：
1. 先处理全部到期超时：按 (期限, 候选人编号) 升序逐个超时并释放名额；
2. 再按该时刻所属季度补位：部门按名称字典序，部门内按 (排名, 候选人编号) 升序，
   逐个激活，直到额度用尽或无人等候。
补位发生在逻辑事件时刻，与调用 advance 的时机无关。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

CONFIRM_WINDOW = timedelta(hours=48)

WAITING = "waiting"      # 等候中
PENDING = "pending"      # 待确认（已占名额，48 小时窗口内）
CONFIRMED = "confirmed"  # 已确认（继续占名额，不再超时）
WITHDRAWN = "withdrawn"  # 已退出（终态）
EXPIRED = "expired"      # 已超时（终态）

ACT_ENQUEUE = "enqueue"
ACT_ACTIVATE = "activate"
ACT_CONFIRM = "confirm"
ACT_WITHDRAW = "withdraw"
ACT_EXPIRE = "expire"

Quarter = Tuple[int, int]


def quarter_of(t: datetime) -> Quarter:
    """返回 UTC 时刻所属的 (年, 季度)，季度为 1..4。"""
    return (t.year, (t.month - 1) // 3 + 1)


def next_quarter_boundary(t: datetime) -> datetime:
    """返回严格大于 t 的下一个季度边界（UTC）。"""
    year, q = quarter_of(t)
    if q == 4:
        return datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(year, q * 3 + 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Event:
    """输入事件：时刻、候选人编号、动作及可选参数（入队需部门与排名）。"""
    time: datetime
    candidate: str
    action: str  # "enqueue" | "confirm" | "withdraw"
    dept: Optional[str] = None
    rank: Optional[int] = None


@dataclass(frozen=True)
class Record:
    """一条有序流转记录。"""
    time: datetime
    action: str
    candidate: str
    dept: str
    quarter: Quarter


@dataclass
class _Candidate:
    cid: str
    dept: str
    rank: int
    state: str = WAITING
    quarter: Optional[Quarter] = None  # 占用名额所属季度
    deadline: Optional[datetime] = None  # 待确认期限


class OfferEngine:
    """部门季度配额下的录用确认与超时补位引擎。时间只能前进。"""

    def __init__(self, start: datetime,
                 quotas: Optional[Dict[str, Dict[Quarter, int]]] = None):
        if start.tzinfo is None:
            raise ValueError("start 必须是带时区的 UTC 时刻")
        self._now = start
        self._quotas = quotas or {}
        self._candidates: Dict[str, _Candidate] = {}
        self._occupied: Dict[Tuple[str, Quarter], int] = {}
        self._records: List[Record] = []
        self._cursor = 0  # 已返回给调用方的记录条数

    # ---- 对外操作 ----

    def apply(self, event: Event) -> List[Record]:
        """先推进到事件时刻，再处理输入动作，随后立即补位。返回本次新增流转记录。

        无效动作（如对已超时者确认、重复入队、对终态操作）为无操作。
        """
        self._check_time(event.time)
        self._run_until(event.time)
        c = self._candidates.get(event.candidate)
        if event.action == ACT_ENQUEUE:
            if c is None:
                c = _Candidate(event.candidate, event.dept, event.rank)
                self._candidates[event.candidate] = c
                self._emit(event.time, ACT_ENQUEUE, c)
        elif event.action == ACT_CONFIRM:
            if c is not None and c.state == PENDING:
                c.state = CONFIRMED
                c.deadline = None
                self._emit(event.time, ACT_CONFIRM, c)
        elif event.action == ACT_WITHDRAW:
            if c is not None and c.state in (WAITING, PENDING, CONFIRMED):
                if c.state in (PENDING, CONFIRMED):
                    self._release(c)
                c.state = WITHDRAWN
                self._emit(event.time, ACT_WITHDRAW, c)
        else:
            raise ValueError(f"未知动作: {event.action}")
        self._fill(event.time)
        return self._drain()

    def advance(self, to: datetime) -> List[Record]:
        """执行截至 to 的全部自动流转（含补位引发的后续超时）。返回新增记录。"""
        self._check_time(to)
        self._run_until(to)
        return self._drain()

    def snapshot(self) -> dict:
        """返回当前候选人状态及配额占用（可比较的纯数据结构）。"""
        return {
            "now": self._now,
            "candidates": {
                cid: {
                    "state": c.state,
                    "dept": c.dept,
                    "rank": c.rank,
                    "quarter": c.quarter,
                    "deadline": c.deadline,
                }
                for cid, c in sorted(self._candidates.items())
            },
            "occupied": self._occupied_snapshot(),
        }

    def _occupied_snapshot(self) -> dict:
        out: Dict[str, Dict[Quarter, int]] = {}
        for (dept, q), n in sorted(self._occupied.items()):
            if n:
                out.setdefault(dept, {})[q] = n
        return out

    # ---- 内部机制 ----

    def _check_time(self, t: datetime) -> None:
        if t.tzinfo is None:
            raise ValueError("时刻必须带时区（UTC）")
        if t < self._now:
            raise ValueError("时间只能前进")

    def _quota(self, dept: str, q: Quarter) -> int:
        return self._quotas.get(dept, {}).get(q, 0)

    def _used(self, dept: str, q: Quarter) -> int:
        return self._occupied.get((dept, q), 0)

    def _emit(self, t: datetime, action: str, c: _Candidate) -> None:
        self._records.append(Record(t, action, c.cid, c.dept,
                                    c.quarter or quarter_of(t)))

    def _release(self, c: _Candidate) -> None:
        key = (c.dept, c.quarter)
        self._occupied[key] -= 1
        if self._occupied[key] == 0:
            del self._occupied[key]

    def _run_until(self, t: datetime) -> None:
        """处理所有逻辑事件时刻 <= t 的自动流转。"""
        while True:
            expiries = [c.deadline for c in self._candidates.values()
                        if c.state == PENDING and c.deadline <= t]
            boundary = next_quarter_boundary(self._now)
            candidates = []
            if expiries:
                candidates.append(min(expiries))
            if boundary <= t:
                candidates.append(boundary)
            if not candidates:
                self._now = t
                return
            te = min(candidates)
            self._now = te
            # 1. 先处理该时刻全部到期（按期限、编号升序）
            due = sorted(
                (c for c in self._candidates.values()
                 if c.state == PENDING and c.deadline <= te),
                key=lambda c: (c.deadline, c.cid))
            for c in due:
                self._release(c)
                c.state = EXPIRED
                c.deadline = None
                self._emit(te, ACT_EXPIRE, c)
            # 2. 再按该时刻所属季度补位
            self._fill(te)

    def _fill(self, t: datetime) -> None:
        """按 t 所属季度，用该季度剩余额度补位（部门名字典序，部门内排名优先）。"""
        q = quarter_of(t)
        for dept in sorted({c.dept for c in self._candidates.values()
                            if c.state == WAITING}):
            while self._used(dept, q) < self._quota(dept, q):
                waiting = [c for c in self._candidates.values()
                           if c.state == WAITING and c.dept == dept]
                if not waiting:
                    break
                c = min(waiting, key=lambda c: (c.rank, c.cid))
                c.state = PENDING
                c.quarter = q
                c.deadline = t + CONFIRM_WINDOW
                self._occupied[(dept, q)] = self._used(dept, q) + 1
                self._emit(t, ACT_ACTIVATE, c)

    def _drain(self) -> List[Record]:
        out = self._records[self._cursor:]
        self._cursor = len(self._records)
        return out
