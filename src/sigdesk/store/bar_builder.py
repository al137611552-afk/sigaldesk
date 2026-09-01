"""由 1m bar 聚合出高周期 bar。纯逻辑，无 IO。

设计要点：
- 增量式：每来一根 1m bar 调用一次 ``push``，返回本次**新收盘**的高周期 bar（可能为空）。
  回测与实盘走同一条路径（ADR-0001）。
- 只消费 closed=True 的 1m bar；进行中的 1m bar 直接忽略，避免高周期 bar 重绘（INV-2）。
- **恰好落在桶边界的那根 1m 当场收盘**：墙钟分桶下，``close_ts == 桶边界`` 的 1m bar
  就是该桶最后一根（更晚的 bar 必然落进下一个桶），所以不必等下一根来"切换"。
  这对多级别规则是关键的：否则 5m 桶要等下一根 1m 才出现，每条多级别规则都会晚一根，
  连 role_bars 与去重键都会错位。
- 其余情况仍在桶切换时收盘（例：期货 12:00 那根 60m 桶只含 11:00-11:30，
  末根 1m 的 close_ts 不等于桶边界，只能等下午开盘的第一根来触发切换）。
- 因此"当前尚未确认收盘的桶"不会被吐出。需要盘中未完成值时用 ``pending``。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..core.models import Bar, Timeframe
from ..core.timeframes import bucket_close_ts, bucket_open_ts


@dataclass(slots=True)
class _Bucket:
    close_ts: int
    open_ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    money: float
    open_interest: float
    trading_day: str | None
    first_bar_open_ts: int

    def merge(self, bar: Bar) -> None:
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.close = bar.close
        self.volume += bar.volume
        self.money += bar.money
        self.open_interest = bar.open_interest  # 持仓量取桶内最后一根（时点量，非累计量）
        self.trading_day = bar.trading_day

    def to_bar(self, symbol: str, timeframe: Timeframe, *, closed: bool) -> Bar:
        return Bar(
            symbol=symbol,
            timeframe=timeframe,
            open_ts=self.open_ts,
            close_ts=self.close_ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            money=self.money,
            open_interest=self.open_interest,
            closed=closed,
            trading_day=self.trading_day,
        )


class BarBuilder:
    """单个 (symbol, timeframe) 的聚合器。"""

    def __init__(self, symbol: str, timeframe: Timeframe) -> None:
        if not timeframe.is_intraday:
            raise ValueError(f"BarBuilder 不支持 {timeframe}（非固定长度周期）")
        self.symbol = symbol
        self.timeframe = timeframe
        self._cur: _Bucket | None = None

    def push(self, bar: Bar) -> Bar | None:
        """喂入一根 1m bar。返回因此**收盘**的高周期 bar；若无则返回 None。"""
        if bar.timeframe is not Timeframe.M1:
            raise ValueError(f"BarBuilder 只接受 1m 输入，收到 {bar.timeframe}")
        if not bar.closed:
            return None  # 进行中的 1m bar 不参与聚合，防止高周期重绘

        target = bucket_close_ts(bar.close_ts, self.timeframe)
        emitted: Bar | None = None

        if self._cur is not None and self._cur.close_ts != target:
            if self._cur.close_ts > target:
                raise ValueError(
                    f"1m bar 时间倒流: 当前桶 {self._cur.close_ts} > 新 bar 桶 {target}"
                )
            emitted = self._cur.to_bar(self.symbol, self.timeframe, closed=True)
            self._cur = None

        if self._cur is None:
            self._cur = _Bucket(
                close_ts=target,
                open_ts=bucket_open_ts(target, self.timeframe),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                money=bar.money,
                open_interest=bar.open_interest,
                trading_day=bar.trading_day,
                first_bar_open_ts=bar.open_ts,
            )
        else:
            self._cur.merge(bar)

        if bar.close_ts == target:
            # 这根 1m 恰好落在桶边界 ⇒ 它就是本桶最后一根，当场收盘，不必等下一根来切换。
            # 一次 push 最多只会有一个桶收盘：能走到这里说明上面没有发生桶切换
            # （切换意味着新 bar 属于新桶，其 close_ts 不可能同时等于旧桶边界）。
            emitted = self._cur.to_bar(self.symbol, self.timeframe, closed=True)
            self._cur = None

        return emitted

    def pending(self) -> Bar | None:
        """当前尚未收盘的高周期 bar（closed=False）。仅供盘中展示，不得进入规则求值。"""
        if self._cur is None:
            return None
        return self._cur.to_bar(self.symbol, self.timeframe, closed=False)

    def flush(self) -> Bar | None:
        """收盘/回放结束时强制吐出当前桶。调用方需自行确认该桶确已收盘。"""
        if self._cur is None:
            return None
        out = self._cur.to_bar(self.symbol, self.timeframe, closed=True)
        self._cur = None
        return out


def bar_date(bar: Bar) -> dt.date:
    """这根 bar 归属的自然日期。

    **期货按交易日，不按自然日** —— 夜盘归属下一交易日（08-27 21:00 那根属于 08-28），
    按自然日切会把每个交易日劈成两半。加密没有 trading_day，退回 UTC 自然日，
    与 Parquet 分区键（``partition_key``）保持同一口径。
    """
    if bar.trading_day:
        return dt.date.fromisoformat(bar.trading_day)
    return dt.datetime.fromtimestamp(bar.close_ts, dt.UTC).date()


def period_key(bar: Bar, timeframe: Timeframe) -> str:
    """日/周/月的分桶键。

    **键必须字典序单调递增** —— 时间倒流检查直接比字符串。所以：
    - 周用 **ISO 年-周**（`isocalendar()` 的年，不是日历年）：12-31 可能属于次年第 1 周，
      用日历年会得到 `2026-W01` 排在 `2026-W52` 前面，凭空炸出"时间倒流"。
    - 月用 `YYYY-MM`，两位补零。
    """
    d = bar_date(bar)
    if timeframe is Timeframe.W1:
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    if timeframe is Timeframe.MON1:
        return f"{d.year:04d}-{d.month:02d}"
    return d.isoformat()


def day_key(bar: Bar) -> str:
    """日线分桶键。保留这个名字：外部（含测试）一直在用。"""
    return period_key(bar, Timeframe.D1)


class CalendarBuilder:
    """按自然日历切分的聚合器（日 / 周 / 月）。与 ``BarBuilder`` 并列而不是复用它，
    因为它们**没有墙钟桶边界**：

    - 高周期靠 ``ceil(close_ts/period)`` 算出桶边界，落在边界上的那根当场收盘；
    - 日/周/月的边界由**交易日归属**决定，而它们的长度都不固定
      （有无夜盘、节假日前后、月份天数都不同）。
      所以只能在**日期键变化时**收盘，比真实收盘晚一根 bar。

    这个滞后的实际影响：期货的下一根 bar 是 21:00 夜盘（已属下一交易日），
    所以上一日的日线在**下一交易日一开盘**就可用；空窗只落在 15:00 收盘到 21:00 开盘之间，
    那段时间本来就没有行情。周线月线同理，边界处滞后一根。
    **当期未走完的 bar 永远不会被吐出**（INV-2）。
    """

    def __init__(self, symbol: str, timeframe: Timeframe = Timeframe.D1) -> None:
        if not timeframe.is_calendar:
            raise ValueError(f"CalendarBuilder 只支持日历周期（日/周/月），收到 {timeframe}")
        self.symbol = symbol
        self.timeframe = timeframe
        self._cur: _Bucket | None = None
        self._key: str | None = None

    def push(self, bar: Bar) -> Bar | None:
        if bar.timeframe is not Timeframe.M1:
            raise ValueError(f"CalendarBuilder 只接受 1m 输入，收到 {bar.timeframe}")
        if not bar.closed:
            return None

        key = period_key(bar, self.timeframe)
        emitted: Bar | None = None
        if self._cur is not None and self._key != key:
            if key < (self._key or ""):
                raise ValueError(f"1m bar 时间倒流: 当前 {self._key} > 新 bar {key}")
            emitted = self._cur.to_bar(self.symbol, self.timeframe, closed=True)
            self._cur = None

        if self._cur is None:
            self._cur = _Bucket(
                close_ts=bar.close_ts,
                open_ts=bar.open_ts,
                open=bar.open, high=bar.high, low=bar.low, close=bar.close,
                volume=bar.volume, money=bar.money, open_interest=bar.open_interest,
                trading_day=bar.trading_day, first_bar_open_ts=bar.open_ts,
            )
            self._key = key
        else:
            self._cur.merge(bar)
            # 收盘时刻不是预先算出来的，而是"本期最后一根 1m 的收盘时刻"
            self._cur.close_ts = bar.close_ts
        return emitted

    def pending(self) -> Bar | None:
        if self._cur is None:
            return None
        return self._cur.to_bar(self.symbol, self.timeframe, closed=False)

    def flush(self) -> Bar | None:
        if self._cur is None:
            return None
        out = self._cur.to_bar(self.symbol, self.timeframe, closed=True)
        self._cur = None
        self._key = None
        return out


class DayBuilder(CalendarBuilder):
    """日线聚合器。保留这个名字：外部（含测试）一直在用。"""

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol, Timeframe.D1)


def make_builder(symbol: str, timeframe: Timeframe) -> BarBuilder | CalendarBuilder:
    """按周期挑聚合器。日/周/月走自然日历归属，其余走墙钟分桶。"""
    if timeframe.is_calendar:
        return CalendarBuilder(symbol, timeframe)
    return BarBuilder(symbol, timeframe)


def aggregate(symbol: str, bars_1m: list[Bar], timeframe: Timeframe) -> list[Bar]:
    """批量聚合（回测/对拍用）。只吐出**已确认收盘**的桶，不 flush ——
    若输入正好停在桶边界上，末桶也算已收盘、会被吐出；否则由调用方处理最后一个 pending。"""
    b = make_builder(symbol, timeframe)
    out: list[Bar] = []
    for bar in bars_1m:
        got = b.push(bar)
        if got is not None:
            out.append(got)
    return out


def aggregate_complete(symbol: str, bars_1m: list[Bar], timeframe: Timeframe) -> list[Bar]:
    """批量聚合并 flush 末桶。用于已知输入区间完整闭合的场景（如历史归档回补）。"""
    b = make_builder(symbol, timeframe)
    out: list[Bar] = []
    for bar in bars_1m:
        got = b.push(bar)
        if got is not None:
            out.append(got)
    tail = b.flush()
    if tail is not None:
        out.append(tail)
    return out


__all__ = [
    "BarBuilder",
    "CalendarBuilder",
    "DayBuilder",
    "bar_date",
    "period_key",
    "aggregate",
    "aggregate_complete",
    "day_key",
    "make_builder",
]
