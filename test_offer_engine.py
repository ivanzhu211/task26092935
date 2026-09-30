"""offer_engine 自动化测试 (仅标准库 unittest)。"""

import random
import unittest
from datetime import datetime, timedelta, timezone

from offer_engine import (
    ACT_ACTIVATE,
    ACT_CONFIRM,
    ACT_ENQUEUE,
    ACT_EXPIRE,
    ACT_WITHDRAW,
    CONFIRM_WINDOW,
    CONFIRMED,
    EXPIRED,
    PENDING,
    WAITING,
    WITHDRAWN,
    Event,
    OfferEngine,
)

UTC = timezone.utc
T0 = datetime(2026, 2, 1, 0, 0, tzinfo=UTC)  # 2026 Q1 内部, 远离季度边界


def t(hours=0, days=0, base=T0):
    return base + timedelta(hours=hours, days=days)


def enqueue(ts, cid, dept="eng", rank=1):
    return Event(ts, cid, "enqueue", dept=dept, rank=rank)


def states(engine):
    return {cid: c["state"] for cid, c in engine.snapshot()["candidates"].items()}


def used(engine, dept, year, quarter):
    return engine.snapshot()["quotas"].get((dept, year, quarter), {}).get("used", 0)


class BasicFlowTest(unittest.TestCase):
    def test_immediate_activation_and_confirm(self):
        eng = OfferEngine(T0, {("eng", 2026, 1): 2})
        recs = eng.apply(enqueue(T0, "a", rank=1))
        self.assertEqual([r.action for r in recs], [ACT_ENQUEUE, ACT_ACTIVATE])
        self.assertEqual(recs[-1].quarter, (2026, 1))
        self.assertEqual(used(eng, "eng", 2026, 1), 1)

        recs = eng.apply(Event(t(hours=10), "a", "confirm"))
        self.assertEqual([r.action for r in recs], [ACT_CONFIRM])
        self.assertEqual(states(eng)["a"], CONFIRMED)
        # 确认后继续占用名额
        self.assertEqual(used(eng, "eng", 2026, 1), 1)
        # 已确认者不再超时
        eng.advance(t(days=30))
        self.assertEqual(states(eng)["a"], CONFIRMED)
        self.assertEqual(used(eng, "eng", 2026, 1), 1)

    def test_no_quota_means_waiting(self):
        eng = OfferEngine(T0, {})  # 未配置额度为零
        eng.apply(enqueue(T0, "a"))
        self.assertEqual(states(eng)["a"], WAITING)
        self.assertEqual(used(eng, "eng", 2026, 1), 0)

    def test_duplicate_enqueue_rejected(self):
        eng = OfferEngine(T0, {("eng", 2026, 1): 1})
        eng.apply(enqueue(T0, "a"))
        with self.assertRaises(ValueError):
            eng.apply(enqueue(t(hours=1), "a"))

    def test_time_cannot_go_backwards(self):
        eng = OfferEngine(T0, {})
        eng.advance(t(hours=5))
        with self.assertRaises(ValueError):
            eng.advance(t(hours=4))
        with self.assertRaises(ValueError):
            eng.apply(enqueue(t(hours=4), "x"))

    def test_rank_order_and_id_tiebreak(self):
        eng = OfferEngine(T0, {("eng", 2026, 1): 1})
        eng.apply(enqueue(T0, "b", rank=1))
        eng.apply(enqueue(T0, "a", rank=1))  # 同排名按编号字典序
        eng.apply(enqueue(T0, "c", rank=0))  # 数字越小越优先
        # 释放一个名额: 退出已激活的 b
        recs = eng.apply(Event(t(hours=1), "b", "withdraw"))
        acts = [r for r in recs if r.action == ACT_ACTIVATE]
        self.assertEqual([r.candidate for r in acts], ["c"])
        recs = eng.apply(Event(t(hours=2), "c", "withdraw"))
        acts = [r for r in recs if r.action == ACT_ACTIVATE]
        self.assertEqual([r.candidate for r in acts], ["a"])


class ExpiryChainTest(unittest.TestCase):
    def test_multi_round_backfill_preserved(self):
        # 额度 1, 三个候选人: 跨多轮 48 小时窗口, 每轮激活与超时都必须保留
        eng = OfferEngine(T0, {("eng", 2026, 1): 1})
        for cid in ("a", "b", "c"):
            eng.apply(enqueue(T0, cid, rank=1))
        recs = eng.advance(t(hours=96 + 47))
        acts = [(r.time, r.candidate) for r in recs if r.action == ACT_ACTIVATE]
        exps = [(r.time, r.candidate) for r in recs if r.action == ACT_EXPIRE]
        self.assertEqual(
            acts,
            [(t(hours=48), "b"), (t(hours=96), "c")],
        )
        self.assertEqual(
            exps,
            [(t(hours=48), "a"), (t(hours=96), "b")],
        )
        self.assertEqual(states(eng)["c"], PENDING)
        self.assertEqual(used(eng, "eng", 2026, 1), 1)

    def test_confirm_at_exact_deadline_is_expired(self):
        eng = OfferEngine(T0, {("eng", 2026, 1): 1})
        eng.apply(enqueue(T0, "a", rank=1))
        eng.apply(enqueue(T0, "b", rank=2))
        deadline = T0 + CONFIRM_WINDOW
        recs = eng.apply(Event(deadline, "a", "confirm"))
        actions = [r.action for r in recs]
        self.assertNotIn(ACT_CONFIRM, actions)
        self.assertIn(ACT_EXPIRE, actions)
        # 同一时刻先到期再补位: b 在该时刻被激活
        self.assertIn(ACT_ACTIVATE, actions)
        self.assertEqual(states(eng)["a"], EXPIRED)
        self.assertEqual(states(eng)["b"], PENDING)

    def test_confirm_one_second_before_deadline_ok(self):
        eng = OfferEngine(T0, {("eng", 2026, 1): 1})
        eng.apply(enqueue(T0, "a"))
        deadline = T0 + CONFIRM_WINDOW
        recs = eng.apply(Event(deadline - timedelta(seconds=1), "a", "confirm"))
        self.assertEqual([r.action for r in recs], [ACT_CONFIRM])
        self.assertEqual(states(eng)["a"], CONFIRMED)


class SplitEquivalenceTest(unittest.TestCase):
    """长区间随机拆分推进, 与直接 advance(T) 的完整流转记录及最终状态必须一致。"""

    def _build_events(self, rng):
        # 跨两个季度、多轮 48 小时窗口的随机输入
        quotas = {
            ("eng", 2026, 1): 2,
            ("eng", 2026, 2): 1,
            ("ops", 2026, 1): 1,
            ("ops", 2026, 2): 3,
        }
        events = []
        ts = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
        end = datetime(2026, 6, 20, 0, 0, tzinfo=UTC)
        cid = 0
        while ts < end:
            ts += timedelta(hours=rng.randint(1, 30))
            cid += 1
            action = rng.choice(["enqueue", "enqueue", "enqueue", "confirm", "withdraw"])
            if action == "enqueue":
                events.append(
                    Event(ts, "cand%03d" % cid, "enqueue",
                          dept=rng.choice(["eng", "ops"]), rank=rng.randint(0, 5))
                )
            else:
                target = "cand%03d" % rng.randint(1, cid)
                events.append(Event(ts, target, action))
        return quotas, events, end

    def _run(self, quotas, events, splits):
        eng = OfferEngine(datetime(2026, 1, 5, 0, 0, tzinfo=UTC), quotas)
        records = []
        for ev in events:
            for point in splits:
                if eng._now < point <= ev.time:
                    records += eng.advance(point)
            records += eng.apply(ev)
        rest = [p for p in splits if p > eng._now]
        for point in sorted(rest):
            records += eng.advance(point)
        return eng, records

    def test_random_split_equivalence(self):
        rng = random.Random(20260929)
        quotas, events, end = self._build_events(rng)

        # A: 无中间拆分, 事件后一次性推进到终点
        eng_a, recs_a = self._run(quotas, events, [end])

        # B: 在每个超时点与季度边界拆分, 再加随机中间点
        split_points = {end}
        for r in recs_a:
            if r.action == ACT_ACTIVATE and r.time + CONFIRM_WINDOW < end:
                split_points.add(r.time + CONFIRM_WINDOW)
        q = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        while q <= end:
            split_points.add(q)
            q = datetime(q.year + (q.month == 10), (q.month - 1) // 3 * 3 + 4 if q.month != 10 else 1, 1, tzinfo=UTC)
        for _ in range(40):
            point = datetime(2026, 1, 5, tzinfo=UTC) + timedelta(hours=rng.randint(0, 4000))
            if point < end:
                split_points.add(point)
        eng_b, recs_b = self._run(quotas, events, sorted(split_points))

        self.assertEqual(recs_a, recs_b)
        self.assertEqual(eng_a.snapshot(), eng_b.snapshot())

    def test_split_preserves_intermediate_rounds(self):
        # 跨多轮 48 小时窗口: 中间候选人的激活与超时全部保留在记录中
        quotas = {("eng", 2026, 1): 1}
        start = datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
        eng = OfferEngine(start, quotas)
        for i in range(5):
            eng.apply(Event(start, "c%d" % i, "enqueue", dept="eng", rank=i))
        recs = eng.advance(start + timedelta(hours=48 * 4 + 1))
        acts = [(r.time, r.candidate) for r in recs if r.action == ACT_ACTIVATE]
        exps = [(r.time, r.candidate) for r in recs if r.action == ACT_EXPIRE]
        for i in range(1, 5):
            self.assertIn((start + timedelta(hours=48 * i), "c%d" % i), acts)
            self.assertIn((start + timedelta(hours=48 * i), "c%d" % (i - 1)), exps)


class QuarterBoundaryTest(unittest.TestCase):
    # 2026-03-30 00:00 激活, 期限恰好是 2026-04-01 00:00 (Q2 边界)
    BOUNDARY = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    START = BOUNDARY - CONFIRM_WINDOW

    def _engine(self):
        return OfferEngine(self.START, {("eng", 2026, 1): 1, ("eng", 2026, 2): 1})

    def test_boundary_expiry_activation_and_deadline_confirm(self):
        eng = self._engine()
        eng.apply(Event(self.START, "old", "enqueue", dept="eng", rank=1))
        eng.apply(Event(self.START, "new", "enqueue", dept="eng", rank=2))
        self.assertEqual(states(eng)["old"], PENDING)
        self.assertEqual(states(eng)["new"], WAITING)

        # 恰在期限(=季度边界)提交确认: 必须判为已超时
        recs = eng.apply(Event(self.BOUNDARY, "old", "confirm"))
        actions = [(r.action, r.candidate, r.quarter) for r in recs]
        self.assertIn((ACT_EXPIRE, "old", (2026, 1)), actions)
        self.assertNotIn(ACT_CONFIRM, [a for a, _, _ in actions])
        # 同一时刻先到期再按新季度补位: new 消耗 Q2 额度
        self.assertIn((ACT_ACTIVATE, "new", (2026, 2)), actions)
        # 到期释放的是 Q1 名额, 不能当季回收再用
        self.assertEqual(used(eng, "eng", 2026, 1), 0)
        self.assertEqual(used(eng, "eng", 2026, 2), 1)

    def test_cross_quarter_withdraw_returns_original_quarter(self):
        eng = self._engine()
        eng.apply(Event(self.START, "old", "enqueue", dept="eng", rank=1))
        eng.apply(Event(self.START + timedelta(hours=1), "old", "confirm"))
        # 旧季度确认名额存活进入 Q2, 不挤占新额度
        eng.apply(Event(self.BOUNDARY, "new", "enqueue", dept="eng", rank=1))
        self.assertEqual(states(eng)["new"], PENDING)
        self.assertEqual(used(eng, "eng", 2026, 1), 1)
        self.assertEqual(used(eng, "eng", 2026, 2), 1)
        eng.apply(Event(self.BOUNDARY + timedelta(hours=1), "new", "confirm"))
        # 跨季退出只归还原季度(Q1)额度
        recs = eng.apply(Event(self.BOUNDARY + timedelta(days=10), "old", "withdraw"))
        self.assertEqual([r.action for r in recs], [ACT_WITHDRAW])
        self.assertEqual(recs[0].quarter, (2026, 1))
        self.assertEqual(used(eng, "eng", 2026, 1), 0)
        self.assertEqual(used(eng, "eng", 2026, 2), 1)

    def test_reclaimed_old_quarter_slot_not_reusable_now(self):
        # Q1 回收的名额不得在 Q2 发放
        eng = OfferEngine(self.START, {("eng", 2026, 1): 1, ("eng", 2026, 2): 1})
        eng.apply(Event(self.START, "q1", "enqueue", dept="eng", rank=1))
        eng.apply(Event(self.START + timedelta(hours=1), "q1", "confirm"))
        eng.apply(Event(self.BOUNDARY, "q2", "enqueue", dept="eng", rank=1))
        eng.apply(Event(self.BOUNDARY, "w", "enqueue", dept="eng", rank=2))
        self.assertEqual(states(eng)["w"], WAITING)  # Q2 额度已用尽
        # Q1 确认者退出, 释放 Q1 名额; w 仍不能使用旧季度名额
        eng.apply(Event(self.BOUNDARY + timedelta(hours=1), "q1", "withdraw"))
        self.assertEqual(states(eng)["w"], WAITING)
        self.assertEqual(used(eng, "eng", 2026, 1), 0)
        self.assertEqual(used(eng, "eng", 2026, 2), 1)


class SameTimestampTest(unittest.TestCase):
    def test_input_events_processed_in_call_order(self):
        eng = OfferEngine(T0, {("eng", 2026, 1): 1})
        # 同一时刻: 先入队 a(rank 2), 立即补位; 再入队 b(rank 1) 只能等待
        recs1 = eng.apply(Event(T0, "a", "enqueue", dept="eng", rank=2))
        recs2 = eng.apply(Event(T0, "b", "enqueue", dept="eng", rank=1))
        self.assertEqual([r.action for r in recs1], [ACT_ENQUEUE, ACT_ACTIVATE])
        self.assertEqual([r.action for r in recs2], [ACT_ENQUEUE])
        self.assertEqual(states(eng)["a"], PENDING)
        self.assertEqual(states(eng)["b"], WAITING)
        # 新来者不抢占已发出的名额
        recs3 = eng.apply(Event(T0, "a", "withdraw"))
        self.assertEqual(
            [r.action for r in recs3], [ACT_WITHDRAW, ACT_ACTIVATE])
        self.assertEqual(states(eng)["b"], PENDING)

    def test_withdraw_waiting_removes_from_queue(self):
        eng = OfferEngine(T0, {("eng", 2026, 1): 1})
        eng.apply(Event(T0, "a", "enqueue", dept="eng", rank=1))
        eng.apply(Event(T0, "b", "enqueue", dept="eng", rank=2))
        recs = eng.apply(Event(T0, "b", "withdraw"))
        self.assertEqual([r.action for r in recs], [ACT_WITHDRAW])
        self.assertEqual(states(eng)["b"], WITHDRAWN)
        # 终态不会重新入队, 退出与超时后的操作无效
        recs = eng.apply(Event(t(hours=1), "b", "confirm"))
        self.assertEqual(recs, [])
        self.assertEqual(states(eng)["b"], WITHDRAWN)


class InvariantTest(unittest.TestCase):
    def test_quota_never_exceeded_and_single_hold(self):
        rng = random.Random(7)
        quotas = {
            ("eng", 2026, 1): 2, ("eng", 2026, 2): 1,
            ("ops", 2026, 1): 1, ("ops", 2026, 2): 2,
        }
        eng = OfferEngine(datetime(2026, 1, 10, tzinfo=UTC), quotas)
        ts = datetime(2026, 1, 10, tzinfo=UTC)
        end = datetime(2026, 6, 30, tzinfo=UTC)
        cid = 0
        while ts < end:
            ts += timedelta(hours=rng.randint(1, 20))
            cid += 1
            roll = rng.random()
            if roll < 0.6:
                eng.apply(Event(ts, "x%03d" % cid, "enqueue",
                                dept=rng.choice(["eng", "ops"]), rank=rng.randint(0, 4)))
            elif roll < 0.8:
                eng.apply(Event(ts, "x%03d" % rng.randint(1, cid), "confirm"))
            else:
                eng.apply(Event(ts, "x%03d" % rng.randint(1, cid), "withdraw"))
            snap = eng.snapshot()
            holds = {}
            for c in snap["candidates"].values():
                if c["state"] in (PENDING, CONFIRMED):
                    key = (c["dept"], c["quarter"][0], c["quarter"][1])
                    holds[key] = holds.get(key, 0) + 1
            for key, info in snap["quotas"].items():
                self.assertLessEqual(info["used"], info["quota"])
                self.assertEqual(info["used"], holds.get(key, 0))


if __name__ == "__main__":
    unittest.main()
