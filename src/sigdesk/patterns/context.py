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

    def bars_at(self, timeframe: Timeframe) -> tuple[Bar, ...]: ...

    def cache_at(self, timeframe: Timeframe) -> IndicatorCache: ...

# 可作为表达式变量名的 bar 字段
FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "money", "open_interest")


@dataclass(slots=True)
class _State:
    """一个指标实例及其喂养进度。"""

    indicator: Any
    last_ts: int
    cur: Any = None
    prev: Any = None


@dataclass(slots=True)
class IndicatorCache:
    """按 (symbol, timeframe) 持有一组指标状态。跨 bar 存活，是增量性的载体。"""

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
            state.last_ts = ts
        return state

    def reset(self) -> None:
        """清空。换月、换标的或回测重跑时用 —— 残留状态会污染新序列。"""
        self.states.clear()


@dataclass(slots=True)
class EvalContext:
    """一次求值所需的全部输入。无网络、无磁盘、无当前时间依赖。"""

    symbol: str
    timeframe: Timeframe
    bars: tuple[Bar, ...]  # 已按 as-of 截断的已收盘 bar，升序
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
        return Series(field_name, tuple(getattr(b, field_name) for b in self.bars))

    def stamps(self) -> tuple[int, ...]:
        return tuple(b.close_ts for b in self.bars)

    def level(self, key: tuple[Any, ...], factory: Callable[[], Any], src: Series) -> Level:
        """标量型指标（SMA/EMA/RSI/...）的取值入口。"""
        state = self.cache.level((self.symbol, self.timeframe, *key), factory, src.values,
                                 self.stamps())
        return Level(cur=state.cur, prev=state.prev)

    def bar_level(
        self, key: tuple[Any, ...], factory: Callable[[], Any], pick: Callable[[Any], float | None]
    ) -> Level:
        """需要 OHLC 的指标（ATR/KDJ）的取值入口：喂 Bar，用 ``pick`` 取出所需分量。"""
        state = self.cache.level((self.symbol, self.timeframe, *key), factory, self.bars,
                                 self.stamps())
        return Level(cur=pick(state.cur), prev=pick(state.prev))


__all__ = ["FIELDS", "EvalContext", "IndicatorCache", "TimeframeSource"]
