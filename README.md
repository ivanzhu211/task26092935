# task26092935

## 录用候补时间推进引擎 (offer_engine)

进程内重演部门季度配额下的录用确认与超时补位。仅依赖 Python 标准库, 时间只能前进。

### 快速开始

```python
from datetime import datetime, timezone
from offer_engine import Event, OfferEngine

# 起始 UTC 时刻 + 各部门各季度额度: (部门, 年, 季度) -> 非负整数, 未配置为零
eng = OfferEngine(
    datetime(2026, 1, 5, tzinfo=timezone.utc),
    {("eng", 2026, 1): 2, ("eng", 2026, 2): 1},
)

# 入队: 额外需要 dept 与整数 rank (数字越小越优先, 同排名按编号字典序)
eng.apply(Event(datetime(2026, 1, 6, tzinfo=timezone.utc), "cand1", "enqueue",
                dept="eng", rank=1))
# 确认 / 退出
eng.apply(Event(datetime(2026, 1, 7, tzinfo=timezone.utc), "cand1", "confirm"))
# 推进到某时刻, 执行全部自动流转 (超时、季度边界补位及其连锁)
records = eng.advance(datetime(2026, 5, 1, tzinfo=timezone.utc))
# 当前候选人状态及配额占用
snap = eng.snapshot()
```

### 语义要点

- `apply` 先把自动流转推进到事件时刻再处理动作; `apply`/`advance` 都返回本次新增的流转记录 (`Record`), 补位记录的时间是其逻辑事件时刻。
- 有名额时立即按 `(rank, 编号)` 补位; 补位者进入待确认, 自激活时刻起 48 小时确认窗口。确认后继续占名额且不再超时; 到期未确认则超时并释放名额。退出/超时为终态。
- 每次激活消耗其激活时刻所属季度的额度; 退出/超时只归还原季度名额, 旧季度回收的名额不得在当前季度发放。新季度开始时等候者使用新季度额度。
- 同一时刻先处理全部到期, 再按该时刻所属季度补位; 恰在期限时刻的确认判为已超时。相同时间戳的输入事件按调用顺序处理, 每次动作后立即补位。
- 同时发生的内部流转采用固定顺序: 到期按 `(期限, 激活序号, 候选人编号)` 排序; 补位按部门字典序、部门内按 `(rank, 候选人编号)`。
- 无效动作 (如对非待确认者确认、对终态操作) 为无操作; 重复入队、时间倒退、非法额度抛出 `ValueError`。
- 等价性: 无新输入时, 直接 `advance(T)` 与在每个超时点和季度边界逐步推进到 `T`, 产生完全相同的候选人状态、配额占用与有序流转记录。

### 运行测试

```sh
python3 -m unittest test_offer_engine -v
```
