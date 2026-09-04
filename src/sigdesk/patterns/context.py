"""表达式求值上下文：as-of 视图 + 增量指标缓存。

ARCHITECTURE §4.1 要求指标 O(1) 增量更新、禁止每根 bar 全量重算，但表达式里的
``sma(close, 20)`` 是**求值时才被发现**的 —— 规则是 YAML 写的，编译期不知道要建哪些指标。

解法：懒建 + 断点续喂。某个指标第一次被用到时，用 as-of 窗口里的历史**回放一次**完成预热
（一次性 O(n)），之后每根新 bar 只喂增量（O(1)）。缓存按 (symbol, timeframe) 持有，
跨 bar 存活；同一根 bar 内被同一表达式引用多次也只喂一次。

**只喂已收盘 bar**（INV-2）：数据来自 BarView，它本身就只含已收盘 bar。
"""

from __future__ import annotations

import bisect
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.models import Bar, Timeframe
from .values import Level, Series


class TimeframeSource(Protocol):
    """同一标的、同一 as-of 时刻下，按周期取数与取缓存。

    跨级别引用（``at('1h', ...)``）要用它。**as-of 由外层视图定死**：
    5m 那根收在 10:05 时，1h 侧只看得到 10:00 收盘的那根 —— 是最近一根**已收盘**的
    1h bar，不是正在走的那根。这既满足 INV-1（不看未来），也正是"大级别定方向"该有的语义。
    """

    def bars_at(self, timeframe: Timeframe) -> Sequence[Bar]: ...

    def cache_at(self, timeframe: Timeframe) -> IndicatorCache: ...

# 可作为表达式变量名的 bar 字段
FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "money", "open_interest")


# 指标历史的回看上限。给 prev(x, n) 用；n 超过它是配置错误，当场报错。
MAX_LOOKBACK = 250


@dataclass(slots=True)
class _State:
    """一个指标实例及其喂养进度。

    ``history`` 是**有界**的近期取值（含当前），供 ``prev(x, n)`` 回看多根。
    有界是关键：回测里一个标的能喂几十万根，无界就是把整条指标序列留在内存里。
    上限取 MAX_LOOKBACK —— 比它更远的回看会报错，而不是悄悄返回 None
    （静默降级会让规则永远不成立且毫无提示，这个项目已经踩过太多次）。
    """

    indicator: Any
    last_ts: int
    cur: Any = None
    prev: Any = None
    history: deque[Any] = field(default_factory=lambda: deque(maxlen=MAX_LOOKBACK + 1))
    # 物化过的历史 + 它对应的 last_ts。**每根 bar 只物化一次**：
    # 原来每次求值都 `tuple(state.history)`，251 个元素 × 每根几次求值，
    # profile 里这一条占了 398 万次调用，是最大的单项开销。
    _frozen: tuple[Any, ...] = ()
    _frozen_ts: int = -1
    _picked: tuple[Any, ...] = ()
    _picked_ts: int = -1

    def frozen_history(self) -> tuple[Any, ...]:
        if self._frozen_ts != self.last_ts:
            self._frozen = tuple(self.history)
            self._frozen_ts = self.last_ts
        return self._frozen

    def picked_history(self, pick: Callable[[Any], Any]) -> tuple[Any, ...]:
        """``bar_level`` 用：历史里存的是整根 Bar/指标对象，还要 ``pick`` 出分量。
        同一个 state 对应的 ``pick`` 语义恒定（key 里已经含了指标身份），所以
        只按 last_ts 失效即可；调用方每次传的是新 lambda，不能拿它当缓存键。
        """
        if self._picked_ts != self.last_ts:
            self._picked = tuple(pick(v) for v in self.history)
            self._picked_ts = self.last_ts
        return self._picked


@dataclass(slots=True)
class IndicatorCache:
    """按 (symbol, timeframe) 持有一组指标状态。跨 bar 存活，是增量性的载体。"""

    # 逐列缓存（close/volume/close_ts…）。见 column() —— 没有它，
    # 每次求值都要重建整条序列，细周期上直接退化成平方复杂度。
    _cols: dict[tuple[Any, ...], list[Any]] = field(default_factory=dict)

    states: dict[tuple[Any, ...], _State] = field(default_factory=dict)

    def level(
        self,
        key: tuple[Any, ...],
        factory: Callable[[], Any],
        samples: Sequence[Any],
        stamps: Sequence[int],
    ) -> _State:
        """取（必要时新建）指标状态，并把尚未喂过的样本补喂进去。

        ``samples`` 与 ``stamps`` 一一对应（值与其所属 bar 的 close_ts，后者升序）。
        重复求值不会重复喂 —— 判据是 close_ts，不是调用次数。
        """
        state = self.states.get(key)
        if state is None:
            state = _State(indicator=factory(), last_ts=-1)
            self.states[key] = state
        # 用二分定位第一根没喂过的，**不要**从头扫一遍再逐个跳过 ——
        # 那样每次求值都是 O(窗口长度)，增量指标就白做了（回测里直接退化成平方复杂度）。
        start = bisect.bisect_right(stamps, state.last_ts)
        for value, ts in zip(samples[start:], stamps[start:], strict=True):
            state.prev = state.cur
            state.cur = state.indicator.update(value)
            state.history.append(state.cur)
            state.last_ts = ts
        return state

    def column(self, key: tuple[Any, ...], bars: Sequence[Bar], field: str) -> Sequence[Any]:
        """按 (symbol, timeframe, 字段) 缓存一列，**只追加不重建**。

        原来 `stamps()`/`series()` 每次求值都 `tuple(... for b in self.bars)`，
        即每次 O(n)。在 1h 上只有几百根，感觉不到；**放到 1m 上就是 O(n²)** ——
        实测 N 翻倍耗时翻 3~4 倍，2 万根的标的单跑就要几十秒，
        66 个标的合起来看着像"卡死"（用户报的正是这个）。

        bar 序列在回放里是**只追加**的，所以按长度差扩展即可；
        长度变短（换标的/重跑）就重建，不能把旧值留着当新的用。
        """
        cur = self._cols.get(key)
        if cur is None or len(cur) > len(bars):
            cur = self._cols[key] = []
        if len(cur) < len(bars):
            cur.extend(getattr(b, field) for b in bars[len(cur):])
        # **直接返回列表，不要 tuple(cur)** —— 拷贝是 O(n)，每次求值拷一遍
        # 就等于缓存白做（第一版就是这么错的，profile 里 column 仍占大头）。
        # 下游只做切片与二分，不改它；视为只读。
        return cur

    def reset(self) -> None:
        """清空。换月、换标的或回测重跑时用 —— 残留状态会污染新序列。"""
        self.states.clear()
        self._cols.clear()


@dataclass(slots=True)
class EvalContext:
    """一次求值所需的全部输入。无网络、无磁盘、无当前时间依赖。"""

    symbol: str
    timeframe: Timeframe
    bars: Sequence[Bar]  # 已按 as-of 截断的已收盘 bar，升序
    cache: IndicatorCache
    # 跨级别引用的取数口。为 None 时 at() 会明确报错，而不是静默给空序列 ——
    # 空序列会让指标恒为 None、条件恒"不成立"，是最难查的那种失败。
    source: TimeframeSource | None = None

    def at(self, timeframe: Timeframe) -> EvalContext:
        """切到同一标的、同一 as-of 时刻的另一个周期。"""
        if self.source is None:
            raise ValueError(
                f"这里不支持跨级别引用（at('{timeframe.value}', ...)）："
                f"求值上下文没有接周期取数口"
            )
        return EvalContext(
            symbol=self.symbol,
            timeframe=timeframe,
            bars=self.source.bars_at(timeframe),
            cache=self.source.cache_at(timeframe),
            source=self.source,
        )

    @property
    def bar(self) -> Bar | None:
        """当前（最近一根已收盘）bar。"""
        return self.bars[-1] if self.bars else None

    def series(self, field_name: str) -> Series:
        if field_name not in FIELDS:
            raise KeyError(f"未知的 bar 字段 {field_name}；可用: {', '.join(FIELDS)}")
        return Series(field_name, self.cache.column(
            (self.symbol, self.timeframe, field_name), self.bars, field_name))

    def stamps(self) -> Sequence[int]:
        return self.cache.column(
            (self.symbol, self.timeframe, "close_ts"), self.bars, "close_ts")

    def level(self, key: tuple[Any, ...], factory: Callable[[], Any], src: Series) -> Level:
        """标量型指标（SMA/EMA/RSI/...）的取值入口。"""
        state = self.cache.level((self.symbol, self.timeframe, *key), factory, src.values,
                                 self.stamps())
        return Level(cur=state.cur, prev=state.prev, history=state.frozen_history())

    def bar_level(
        self, key: tuple[Any, ...], factory: Callable[[], Any], pick: Callable[[Any], float | None]
    ) -> Level:
        """需要 OHLC 的指标（ATR/KDJ）的取值入口：喂 Bar，用 ``pick`` 取出所需分量。"""
        state = self.cache.level((self.symbol, self.timeframe, *key), factory, self.bars,
                                 self.stamps())
        return Level(cur=pick(state.cur), prev=pick(state.prev),
                     history=state.picked_history(pick))


__all__ = ["FIELDS", "EvalContext", "IndicatorCache", "TimeframeSource"]
