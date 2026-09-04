"""BarStore：多周期 bar 的内存序列 + as-of 视图（INV-1 的落地）。

期货轮询、加密 WS、历史回放三种 Feed 产出的 1m bar 都喂进这里，由它增量派生
高周期序列；规则求值**只能**通过 ``view(symbol, as_of)`` 取数 ——
视图在构造时就按 as_of 物理截断，下游拿不到未来数据，从结构上杜绝未来函数。

纯逻辑、无 IO：落盘由 ``store.parquet_io`` 负责，装载历史由调用方喂进 ``load``。
两个市场共用同一实现 —— 加密无 trading_day、无休市，期货有夜盘和跳空，
但 as-of 截断只看 close_ts，因此行为完全一致。
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Iterator, Sequence
from itertools import islice
from typing import Any, Final

from ..core.models import Bar, Timeframe
from .bar_builder import BarBuilder, CalendarBuilder, make_builder

# 默认派生的高周期。1m 恒存，不必列出。
# 面板的九宫格要同时看 1m 5m 15m 30m 1h 4h 1d 1w 1mon，所以默认档要**含日历周期**。
# 少一个的后果不是报错，是那一格静默停更（踩过：watch.py 只按规则派生，5m 停了半天）。
DEFAULT_TIMEFRAMES: Final[tuple[Timeframe, ...]] = (
    Timeframe.M5,
    Timeframe.M15,
    Timeframe.M30,
    Timeframe.H1,
    Timeframe.H4,
    Timeframe.D1,
    Timeframe.W1,
    Timeframe.MON1,
)
# 每个 (symbol, timeframe) 内存里最多留多少根；超出后按批裁剪，避免每根都搬数组
MAX_BARS: Final = 5000
_TRIM_SLACK: Final = 512


def _as_timeframe(tf: Timeframe | str) -> Timeframe:
    return tf if isinstance(tf, Timeframe) else Timeframe(tf)


class _Prefix(Sequence[Bar]):
    """某条 bar 列表的前 ``n`` 根的只读视图。**不拷贝**。

    为什么不直接 ``tuple(seq[:n])``：回测里每根 bar 都要为每个角色建一个 BarView，
    一拷就是最多 MAX_BARS 根，于是"每根 bar O(总根数)"——整体退化成平方。
    1m 当扳机时 bar 数是 1h 的 60 倍，两边同时放大，表现就是"跑不完"。

    截断语义（INV-1）照旧成立：越过 ``n`` 的下标一律 IndexError，切片也夹在 ``n`` 内，
    看不到未来。底层 list 只在尾部追加，裁剪走的是"换一条新 list"（见 ``_append``），
    所以已经发出去的视图不会被就地改动。
    """

    __slots__ = ("_n", "_seq")

    def __init__(self, seq: list[Bar], n: int) -> None:
        self._seq = seq
        self._n = n

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            start, stop, step = index.indices(self._n)
            if step == 1:
                return tuple(self._seq[start:stop])
            return tuple(self._seq[i] for i in range(start, stop, step))
        i = index + self._n if index < 0 else index
        if not 0 <= i < self._n:
            raise IndexError(index)
        return self._seq[i]

    def __iter__(self) -> Iterator[Bar]:
        return islice(iter(self._seq), self._n)

    def __eq__(self, other: object) -> bool:
        """能和元组/列表直接比较 —— 它对下游就是"那一段 bar"，
        换成不拷贝的实现不该让调用方改写比较方式。"""
        if isinstance(other, _Prefix):
            other = tuple(other)
        if isinstance(other, (tuple, list)):
            return self._n == len(other) and all(a == b for a, b in zip(self, other, strict=True))
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]  # 可变底层，不给哈希

    def __repr__(self) -> str:
        return f"<bars n={self._n}>"


class BarView:
    """某个 symbol 在某个时刻的只读切面。构造时即完成截断，之后不会再变。

    ``bars()`` 返回的一定满足 ``close_ts <= as_of`` 且 ``closed=True``。
    """

    __slots__ = ("_cut", "_series", "as_of", "symbol")

    def __init__(self, symbol: str, as_of: int, series: dict[Timeframe, list[Bar]]) -> None:
        self.symbol = symbol
        self.as_of = as_of
        # **构造时就把各周期的 list 对象定死**（只是拷一份引用，O(周期数)）。
        # 裁剪时 store 会把 series[tf] 换成新 list，这里捕获早了就不会跟着换。
        self._series = dict(series)
        # 截断位置**按需**算：原来在这里给每个周期都算一遍，而且是先物化
        # `[b.close_ts for b in bars]` 再二分——为了 O(log n) 的二分先付 O(n)，
        # 且规则用不到的周期也照付。现在用 key= 直接二分，且只算问到的周期。
        self._cut: dict[Timeframe, tuple[list[Bar], int]] = {}

    def _cut_of(self, tf: Timeframe) -> tuple[list[Bar], int]:
        """定死"哪条 list 的前几根"。**list 对象和截断位置必须一起捕获、一起缓存**：
        裁剪时 store 会把 ``series[tf]`` 换成一条新 list（见 ``_append``），
        如果这里每次重新 ``self._series.get(tf)``，就会拿新 list 配旧 cut ——
        长度对得上、内容全错，还不报错。有回归测试钉住（test_bar_store）。
        """
        got = self._cut.get(tf)
        if got is None:
            seq = self._series.get(tf, [])
            got = self._cut[tf] = (
                seq, bisect.bisect_right(seq, self.as_of, key=lambda b: b.close_ts))
        return got

    def bars(self, timeframe: Timeframe | str) -> Sequence[Bar]:
        """该周期截至 as_of 的全部已收盘 bar，按时间升序。"""
        return _Prefix(*self._cut_of(_as_timeframe(timeframe)))

    def last(self, timeframe: Timeframe | str) -> Bar | None:
        """最近一根已收盘 bar；无数据返回 None。"""
        bars = self.bars(timeframe)
        return bars[-1] if bars else None

    def closes(self, timeframe: Timeframe | str) -> list[float]:
        """收盘价序列，指标计算的常用入口。"""
        return [b.close for b in self.bars(timeframe)]

    def __repr__(self) -> str:
        got = {tf.value: len(self.bars(tf)) for tf in self._series}
        return f"BarView({self.symbol} as_of={self.as_of} {got})"


class BarStore:
    """喂 1m、自动派生高周期、按 as-of 供数。"""

    def __init__(
        self,
        timeframes: Sequence[Timeframe] = DEFAULT_TIMEFRAMES,
        *,
        max_bars: int = MAX_BARS,
    ) -> None:
        for tf in timeframes:
            if tf is Timeframe.M1:
                raise ValueError("1m 是输入本身，不需要也不能作为派生周期")
        self._timeframes = tuple(timeframes)
        self._max_bars = max_bars
        self._series: dict[str, dict[Timeframe, list[Bar]]] = {}
        self._builders: dict[str, dict[Timeframe, BarBuilder | CalendarBuilder]] = {}
        self._last_1m: dict[str, int] = {}

    @property
    def timeframes(self) -> tuple[Timeframe, ...]:
        """派生的高周期（不含恒存的 1m）。"""
        return self._timeframes

    def symbols(self) -> list[str]:
        return sorted(self._series)

    def push(self, bar: Bar) -> list[Bar]:
        """喂入一根 1m bar，返回本次**新收盘**的 bar（含这根 1m 自身），按周期从小到大。

        重复投递（同一 close_ts）被忽略；时间倒流抛错 —— 那说明上游 Feed 排序坏了，不该吞。
        """
        if bar.timeframe is not Timeframe.M1:
            raise ValueError(f"BarStore.push 只接受 1m，收到 {bar.timeframe}；历史装载请用 load()")
        if not bar.closed:
            return []  # 进行中的 bar 不入库（INV-2）
        last = self._last_1m.get(bar.symbol)
        if last is not None:
            if bar.close_ts == last:
                return []  # 重叠窗口/重连回补造成的重复投递，忽略即可
            if bar.close_ts < last:
                # Feed 的契约是 close_ts 单调不减；倒流说明上游排序坏了，是真 bug
                raise ValueError(
                    f"{bar.symbol} 的 1m bar 时间倒流: 已到 {last}，又收到 {bar.close_ts}"
                )
        self._last_1m[bar.symbol] = bar.close_ts

        series = self._series.setdefault(bar.symbol, {})
        builders = self._builders.setdefault(bar.symbol, {})
        self._append(series, Timeframe.M1, bar)

        emitted = [bar]
        for tf in self._timeframes:
            builder = builders.get(tf)
            if builder is None:
                builder = builders[tf] = make_builder(bar.symbol, tf)
            done = builder.push(bar)
            if done is not None:
                self._append(series, tf, done)
                emitted.append(done)
        return emitted

    def load(self, bars: Iterable[Bar]) -> None:
        """批量装载历史（如从 Parquet 读出的既有序列）。

        与 ``push`` 的分工：``load`` 直接把各周期序列摆进去，**不**重新派生 ——
        因此装载高周期历史后再 push 1m，高周期会从新的桶边界继续，不会污染历史。
        """
        for bar in sorted(bars, key=lambda b: (b.symbol, b.timeframe.value, b.close_ts)):
            if not bar.closed:
                continue
            series = self._series.setdefault(bar.symbol, {})
            self._append(series, bar.timeframe, bar)
            if bar.timeframe is Timeframe.M1:
                prev = self._last_1m.get(bar.symbol)
                self._last_1m[bar.symbol] = max(prev or 0, bar.close_ts)

    def last_close_ts(self, symbol: str, timeframe: Timeframe = Timeframe.M1) -> int | None:
        """该标的该周期已入库的最新 close_ts。

        用途：预热完成后给 Feed 的游标播种。不播种的话，Feed 会把重叠窗口里的历史
        再发一遍，撞上下面 ``push`` 的时间倒流校验（真跑踩过）。
        """
        seq = self._series.get(symbol, {}).get(timeframe)
        return seq[-1].close_ts if seq else None

    def resume_map(self, timeframe: Timeframe = Timeframe.M1) -> dict[str, int]:
        """所有标的的续播位置，直接喂给 ``Feed(resume_from=...)``。"""
        out: dict[str, int] = {}
        for symbol in self._series:
            ts = self.last_close_ts(symbol, timeframe)
            if ts is not None:
                out[symbol] = ts
        return out

    def view(self, symbol: str, as_of: int) -> BarView:
        """INV-1：规则求值的唯一取数入口。未知 symbol 返回空视图而非报错 ——
        规则可能引用尚未收到数据的标的，那是"暂无数据"，不是配置错误。"""
        return BarView(symbol, as_of, self._series.get(symbol, {}))

    def _append(self, series: dict[Timeframe, list[Bar]], tf: Timeframe, bar: Bar) -> None:
        seq = series.setdefault(tf, [])
        if not seq or bar.close_ts > seq[-1].close_ts:
            seq.append(bar)  # 常态：追加
        else:
            # 乱序或回填装载：同一 close_ts 覆盖（次日权威校正），否则按位插入
            idx = bisect.bisect_left(seq, bar.close_ts, key=lambda b: b.close_ts)
            if idx < len(seq) and seq[idx].close_ts == bar.close_ts:
                seq[idx] = bar
            else:
                seq.insert(idx, bar)
        if len(seq) > self._max_bars + _TRIM_SLACK:
            # **换一条新 list，不就地 del** —— 已经发出去的 _Prefix 持有旧 list，
            # 就地从头删会让它们的下标整体错位（悄悄读到错的 bar，不报错）。
            series[tf] = seq[len(seq) - self._max_bars:]


__all__ = ["DEFAULT_TIMEFRAMES", "MAX_BARS", "BarStore", "BarView"]
