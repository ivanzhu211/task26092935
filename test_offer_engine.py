"""offer_engine 自动化测试。"""
import random
import unittest
from datetime import datetime, timedelta, timezone

from offer_engine import (
    ACT_ACTIVATE, ACT_CONFIRM, ACT_ENQUEUE, ACT_EXPIRE, ACT_WITHDRAW,
    CONFIRM_WINDOW, CONFIRMED, EXPIRED, PENDING, WAITING, WITHDRAWN,
    Event, OfferEngine, quarter_of,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)  # Q1 中部
H = timedelta(hours=1)


def ev(t, cid, action, dept=None, rank=None):
    return Event(time=t, candidate=cid, action=action, dept=dept, rank=rank)


def enq(t, cid, dept, rank):
    return ev(t, cid, ACT_ENQUEUE, dept, rank)


def actions(records):
    return [(r.time, r.action, r.candidate) for r in records]


class TestBasicFlow(unittest.TestCase):
    def test_immediate_activation_and_confirm(self):
        e = OfferEngine(T0, {"eng": {(2026, 1): 2}})
        recs = e.apply(enq(T0, "a", "eng", 1))
        self.assertEqual(actions(recs),
                         [(T0, ACT_ENQUEUE, "a"), (T0, ACT_ACTIVATE, "a")])
        c = e.snapshot()["candidates"]["a"]
        self.assertEqual(c["state"], PENDING)
        self.assertEqual(c["deadline"], T0 + CONFIRM_WINDOW)
        self.assertEqual(e.snapshot()["occupied"], {"eng": {(2026, 1): 1}})

        recs = e.apply(ev(T0 + H, "a", ACT_CONFIRM))
        self.assertEqual(actions(recs), [(T0 + H, ACT_CONFIRM, "a")])
        self.assertEqual(e.snapshot()["candidates"]["a"]["state"], CONFIRMED)
        # 确认后继续占用名额
        self.assertEqual(e.snapshot()["occupied"], {"eng": {(2026, 1): 1}})

    def test_rank_order_and_no_preemption(self):
        e = OfferEngine(T0, {"eng": {(2026, 1): 1}})
        e.apply(enq(T0, "low", "eng", 5))
        recs = e.apply(enq(T0 + H, "high", "eng", 1))
        # 名额已发出，新来者不抢占，只能等候
        self.assertEqual(actions(recs), [(T0 + H, ACT_ENQUEUE, "high")])
        self.assertEqual(e.snapshot()["candidates"]["high"]["state"], WAITING)
        # 同排名按编号字典序
        e2 = OfferEngine(T0, {"eng": {(2026, 1): 2}})
        e2.apply(enq(T0, "b", "eng", 1))
        recs = e2.apply(enq(T0, "a", "eng", 1))
        self.assertEqual(actions(recs),
                         [(T0, ACT_ENQUEUE, "a"), (T0, ACT_ACTIVATE, "a")])

    def test_expire_and_backfill(self):
        e = OfferEngine(T0, {"eng": {(2026, 1): 1}})
        e.apply(enq(T0, "a", "eng", 1))
        e.apply(enq(T0, "b", "eng", 2))
        recs = e.advance(T0 + CONFIRM_WINDOW)
        # a 在期限时刻超时，b 立即补位，窗口从同一逻辑时刻起算
        self.assertEqual(actions(recs), [
            (T0 + CONFIRM_WINDOW, ACT_EXPIRE, "a"),
            (T0 + CONFIRM_WINDOW, ACT_ACTIVATE, "b"),
        ])
        self.assertEqual(e.snapshot()["candidates"]["b"]["deadline"],
                         T0 + 2 * CONFIRM_WINDOW)

    def test_confirm_at_deadline_is_expired(self):
        e = OfferEngine(T0, {"eng": {(2026, 1): 1}})
        e.apply(enq(T0, "a", "eng", 1))
        recs = e.apply(ev(T0 + CONFIRM_WINDOW, "a", ACT_CONFIRM))
        # 恰在期限时刻：先超时，确认无效
        self.assertEqual(actions(recs), [(T0 + CONFIRM_WINDOW, ACT_EXPIRE, "a")])
        self.assertEqual(e.snapshot()["candidates"]["a"]["state"], EXPIRED)

    def test_withdraw_releases_and_backfills(self):
        e = OfferEngine(T0, {"eng": {(2026, 1): 1}})
        e.apply(enq(T0, "a", "eng", 1))
        e.apply(enq(T0, "b", "eng", 2))
        recs = e.apply(ev(T0 + H, "a", ACT_WITHDRAW))
        self.assertEqual(actions(recs), [
            (T0 + H, ACT_WITHDRAW, "a"),
            (T0 + H, ACT_ACTIVATE, "b"),
        ])
        self.assertEqual(e.snapshot()["candidates"]["b"]["state"], PENDING)

    def test_withdraw_waiting_and_terminal_states(self):
        e = OfferEngine(T0, {"eng": {(2026, 1): 0}})
        e.apply(enq(T0, "a", "eng", 1))
        e.apply(ev(T0 + H, "a", ACT_WITHDRAW))
        self.assertEqual(e.snapshot()["candidates"]["a"]["state"], WITHDRAWN)
        # 终态操作与重复入队均为无操作
        self.assertEqual(e.apply(ev(T0 + 2 * H, "a", ACT_WITHDRAW)), [])
        self.assertEqual(e.apply(enq(T0 + 3 * H, "a", "eng", 1)), [])

    def test_time_cannot_go_backward(self):
        e = OfferEngine(T0, {})
        with self.assertRaises(ValueError):
            e.advance(T0 - H)
        with self.assertRaises(ValueError):
            e.apply(ev(T0 - H, "a", ACT_CONFIRM))

    def test_unconfigured_quota_is_zero(self):
        e = OfferEngine(T0, {})
        e.apply(enq(T0, "a", "eng", 1))
        self.assertEqual(e.snapshot()["candidates"]["a"]["state"], WAITING)
        self.assertEqual(e.snapshot()["occupied"], {})



Q2 = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)  # Q2 起点


class TestQuarterBoundary(unittest.TestCase):
    def test_boundary_expiry_backfill_and_deadline_confirm(self):
        # 旧名额恰在季度边界超时 + 新季度补位 + 期限确认 同时发生
        quotas = {"eng": {(2026, 1): 1, (2026, 2): 1}}
        start = Q2 - CONFIRM_WINDOW  # a 的期限恰为 Q2 起点
        e = OfferEngine(start, quotas)
        e.apply(enq(start, "a", "eng", 1))           # 占 Q1 名额
        e.apply(enq(start, "b", "eng", 2))           # 等候
        recs = e.apply(ev(Q2, "a", ACT_CONFIRM))     # 恰在期限确认 -> 已超时
        # 同一时刻：a 超时释放 Q1 名额；b 用 Q2 新额度补位
        self.assertEqual(actions(recs), [
            (Q2, ACT_EXPIRE, "a"),
            (Q2, ACT_ACTIVATE, "b"),
        ])
        snap = e.snapshot()
        self.assertEqual(snap["candidates"]["a"]["state"], EXPIRED)
        self.assertEqual(snap["candidates"]["b"]["state"], PENDING)
        self.assertEqual(snap["candidates"]["b"]["quarter"], (2026, 2))
        # Q1 名额已释放，Q2 占用 1
        self.assertEqual(snap["occupied"], {"eng": {(2026, 2): 1}})

    def test_cross_quarter_withdraw_returns_to_original_quarter(self):
        quotas = {"eng": {(2026, 1): 1, (2026, 2): 1}}
        t1 = Q2 - 2 * H                          # Q1 末尾
        e = OfferEngine(t1, quotas)
        e.apply(enq(t1, "a", "eng", 1))          # 占 Q1
        e.apply(ev(t1 + H, "a", ACT_CONFIRM))    # 确认，存活跨季
        e.apply(enq(t1 + H, "b", "eng", 2))      # 等候
        e.advance(Q2)                            # 跨季：b 用 Q2 额度补位
        snap = e.snapshot()
        self.assertEqual(snap["occupied"], {"eng": {(2026, 1): 1, (2026, 2): 1}})
        # 跨季退出已确认的 a：只归还 Q1 名额，Q2 占用不变
        recs = e.apply(ev(Q2 + H, "a", ACT_WITHDRAW))
        self.assertEqual(actions(recs), [(Q2 + H, ACT_WITHDRAW, "a")])
        snap = e.snapshot()
        self.assertEqual(snap["occupied"], {"eng": {(2026, 2): 1}})
        # Q1 回收的名额不得在 Q2 发放：c 入队后占用的是 Q2 还是 Q1？
        e.apply(enq(Q2 + 2 * H, "c", "eng", 1))
        snap = e.snapshot()
        # Q2 额度已用完（b 占用），c 只能等候；Q1 回收名额不可用于 Q2
        self.assertEqual(snap["candidates"]["c"]["state"], WAITING)
        self.assertEqual(snap["occupied"], {"eng": {(2026, 2): 1}})

    def test_old_occupancy_does_not_block_new_quarter(self):
        quotas = {"eng": {(2026, 1): 1, (2026, 2): 1}}
        t1 = Q2 - 3 * H                          # Q1 末尾
        e = OfferEngine(t1, quotas)
        e.apply(enq(t1, "a", "eng", 1))
        e.apply(ev(t1 + H, "a", ACT_CONFIRM))    # a 确认，永久占 Q1
        e.apply(enq(t1 + 2 * H, "b", "eng", 2))
        e.advance(Q2)
        # 旧季度占用不挤占新额度：b 在 Q2 起点补位
        self.assertEqual(e.snapshot()["candidates"]["b"]["quarter"], (2026, 2))
        self.assertEqual(e.snapshot()["occupied"],
                         {"eng": {(2026, 1): 1, (2026, 2): 1}})

    def test_waiting_can_use_new_quarter_quota(self):
        quotas = {"eng": {(2026, 1): 0, (2026, 2): 2}}
        e = OfferEngine(T0, quotas)
        e.apply(enq(T0, "a", "eng", 1))
        e.apply(enq(T0, "b", "eng", 2))
        recs = e.advance(Q2)
        self.assertEqual(actions(recs), [
            (Q2, ACT_ACTIVATE, "a"),
            (Q2, ACT_ACTIVATE, "b"),
        ])



def make_script():
    """确定性事件脚本：跨 Q1->Q2，多轮 48 小时补位。"""
    quotas = {"eng": {(2026, 1): 2, (2026, 2): 1},
              "ops": {(2026, 1): 1, (2026, 2): 2}}
    events = []
    t = T0
    # 初始入队：eng 5 人、ops 3 人
    for i in range(5):
        events.append(enq(t + i * H, f"e{i}", "eng", (i * 7) % 3))
    for i in range(3):
        events.append(enq(t + i * H, f"o{i}", "ops", i))
    # 散布确认与退出
    events.append(ev(t + 30 * H, "e0", ACT_CONFIRM))
    events.append(ev(t + 60 * H, "o0", ACT_WITHDRAW))
    events.append(ev(t + 70 * H, "e1", ACT_CONFIRM))
    events.append(enq(t + 80 * H, "e5", "eng", 0))
    events.append(ev(t + 90 * H, "e5", ACT_WITHDRAW))
    events.append(enq(Q2 + 5 * H, "e6", "eng", 1))
    events.append(enq(Q2 + 6 * H, "o3", "ops", 0))
    events.append(ev(Q2 + 100 * H, "e4", ACT_CONFIRM))
    events.append(ev(Q2 + 120 * H, "o1", ACT_WITHDRAW))
    end = Q2 + 200 * H
    events.sort(key=lambda e: e.time)  # 稳定排序：同时刻保持调用顺序
    return quotas, events, end


def run(quotas, events, end, split_points):
    e = OfferEngine(T0, quotas)
    all_records = []
    splits = sorted(split_points)
    for event in events:
        while splits and splits[0] <= event.time:
            all_records += e.advance(splits.pop(0))
        all_records += e.apply(event)
    for s in splits:
        all_records += e.advance(s)
    all_records += e.advance(end)
    return all_records, e.snapshot()


class TestSplitEquivalence(unittest.TestCase):
    def test_random_splits_match_one_shot(self):
        quotas, events, end = make_script()
        ref_records, ref_snap = run(quotas, events, end, [])
        rng = random.Random(20260930)
        for trial in range(20):
            # 在 [T0, end] 内随机取拆分点，含季度边界与 48h 倍数附近
            span = int((end - T0).total_seconds() // 3600)
            points = [T0 + rng.randrange(0, span) * H
                      for _ in range(rng.randrange(1, 15))]
            points += [Q2, Q2 - CONFIRM_WINDOW, T0 + CONFIRM_WINDOW]
            records, snap = run(quotas, events, end, points)
            self.assertEqual(actions(records), actions(ref_records),
                             f"trial {trial}: 流转记录不一致")
            self.assertEqual(snap, ref_snap, f"trial {trial}: 最终状态不一致")

    def test_multi_round_backfill_preserved(self):
        # 长区间跨多轮 48h 窗口：中间激活与超时全部保留
        quotas = {"eng": {(2026, 1): 1}}
        events = [enq(T0 + i * timedelta(minutes=1), f"c{i}", "eng", i)
                  for i in range(4)]
        records, _ = run(quotas, events, T0 + 10 * CONFIRM_WINDOW, [])
        acts = [r for r in records if r.action == ACT_ACTIVATE]
        exps = [r for r in records if r.action == ACT_EXPIRE]
        self.assertEqual(len(acts), 4)
        self.assertEqual(len(exps), 4)
        # 每轮激活间隔恰为 48h，激活时刻即补位逻辑时刻
        for i, r in enumerate(acts):
            self.assertEqual(r.time, T0 + i * CONFIRM_WINDOW)
            self.assertEqual(r.candidate, f"c{i}")


class TestInvariants(unittest.TestCase):
    def test_quota_never_exceeded_and_single_slot(self):
        quotas, events, end = make_script()
        e = OfferEngine(T0, quotas)
        rng = random.Random(7)
        points = sorted(T0 + rng.randrange(0, 900) * H for _ in range(30))
        checkpoints = []
        for event in events:
            while points and points[0] <= event.time:
                checkpoints.append(points.pop(0))
            checkpoints.append(event)
        checkpoints += points
        checkpoints.append(end)
        for cp in checkpoints:
            e.advance(cp) if isinstance(cp, datetime) else e.apply(cp)
            snap = e.snapshot()
            # 每个部门季度占用不超过额度
            for dept, per_q in snap["occupied"].items():
                for q, n in per_q.items():
                    self.assertLessEqual(n, quotas.get(dept, {}).get(q, 0))
            # 每个候选人最多占一个名额：占用总数 == 待确认+已确认人数
            holders = [c for c in snap["candidates"].values()
                       if c["state"] in (PENDING, CONFIRMED)]
            total = sum(n for per_q in snap["occupied"].values()
                        for n in per_q.values())
            self.assertEqual(len(holders), total)
            # 占名额者必有归属季度
            for c in holders:
                self.assertIsNotNone(c["quarter"])

if __name__ == "__main__":
    unittest.main()
