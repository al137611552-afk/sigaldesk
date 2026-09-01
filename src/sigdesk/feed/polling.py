"""期货实时 Feed：轮询 Quote API 的 by-count 接口。

Quote API 没有推送（CLAUDE.md 坑#6），且只有 by-count 能取到当日盘中数据（坑#10）。
因此实时链路是：每根 1m bar 收盘后稍等再拉，带重叠窗口，去重后产出。

去重与补缺的纯逻辑放在 ``BarCursor`` 里，可脱环境单测；
``PollingFeed`` 只负责调度与 IO。
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Sequence

from ..core.calendar import MarketCalendar
from ..core.models import Bar, Symbol, Timeframe
from .quote_api import QuoteApiClient, normalize_klines

# 每次多要这么多根，用重叠区间发现并补上漏掉的 bar
OVERLAP_BARS = 20
# bar 收盘后等这么久再拉，给数据源留出成交归集时间
FETCH_DELAY_S = 1.5


class BarCursor:
    """按 symbol 记录已产出的最新 close_ts，做去重与缺口检测。纯逻辑。"""

    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def last_ts(self, symbol: str) -> int | None:
        return self._last.get(symbol)

    def seed(self, positions: dict[str, int]) -> None:
        """用已落盘的最新位置播种。

        启动时不播种的话，游标为空 ⇒ 第一根到达时判不出"停机期间漏了什么"，
        缺口只能靠人工回补。播种后重启与断线走的是同一条回补路径。
        """
        for symbol, close_ts in positions.items():
            prev = self._last.get(symbol)
            if prev is None or close_ts > prev:
                self._last[symbol] = close_ts

    def accept(self, bars: Sequence[Bar]) -> list[Bar]:
        """过滤出真正的新 bar：只保留已收盘、且晚于已产出位置的，按时间升序。"""
        out: list[Bar] = []
        for bar in sorted(bars, key=lambda b: b.close_ts):
            if not bar.closed:
                continue
            last = self._last.get(bar.symbol)
            if last is not None and bar.close_ts <= last:
                continue
            self._last[bar.symbol] = bar.close_ts
            out.append(bar)
        return out

    def missing_span(self, symbol: str, first_new_ts: int, period: int) -> tuple[int, int] | None:
        """本批最早的新 bar 与上次产出位置之间是否有空档。

        期货的交易时段间断也会命中，调用方需结合日历判断是否为真缺口。
        """
        last = self._last.get(symbol)
        if last is None or first_new_ts <= last + period:
            return None
        return (last, first_new_ts)


def next_fetch_delay(now_ts: float, period: int = 60, delay_s: float = FETCH_DELAY_S) -> float:
    """距离下一次拉取还有多久：下一个 bar 收盘时刻 + 固定延迟。"""
    next_close = (int(now_ts) // period + 1) * period
    return max(0.1, next_close + delay_s - now_ts)


class PollingFeed:
    """一个 QuoteApiClient 服务多个标的；每轮为每个标的拉一次最近 N 根 1m。

    ``resume_from``（uid -> 已入库的最新 close_ts）用于承接预热/重启：
    见 ``BarStore.resume_map``。
    """

    def __init__(
        self,
        client: QuoteApiClient,
        symbols: Sequence[Symbol],
        calendars: dict[str, MarketCalendar],
        *,
        fetch_bars: int = OVERLAP_BARS,
        resume_from: dict[str, int] | None = None,
    ) -> None:
        for sym in symbols:
            if not sym.quote_code:
                raise ValueError(f"{sym.uid} 缺少 quote_code，无法通过 Quote API 订阅")
            if sym.is_continuous:
                raise ValueError(f"{sym.uid} 是主连序列，不得用于实时预警（CLAUDE.md 坑#9）")
        self._client = client
        self._symbols = list(symbols)
        self._calendars = calendars
        self._fetch_bars = fetch_bars
        self._cursor = BarCursor()
        if resume_from:
            # 用已入库的最新位置播种。不播种的话，首轮轮询会把整个重叠窗口当成新数据发出去
            # —— 重叠窗口本是用来查缺口的，没有游标就变成了重发历史。
            self._cursor.seed(resume_from)
        self.gaps_detected: list[tuple[str, int, int]] = []

    def _active(self, now_ts: int) -> list[Symbol]:
        """只轮询处于交易时段内的标的，避免非交易时段空转。"""
        return [s for s in self._symbols if self._calendars[s.calendar].in_session(now_ts)]

    async def poll_once(self, now_ts: int) -> list[Bar]:
        """拉一轮，返回本轮新产生的已收盘 bar（已去重、已按时间排序）。"""
        out: list[Bar] = []
        for sym in self._active(now_ts):
            assert sym.quote_code is not None
            rows = await self._client.kline_by_count(sym.quote_code, Timeframe.M1, self._fetch_bars)
            bars = normalize_klines(
                rows,
                symbol=sym.uid,
                timeframe=Timeframe.M1,
                now_ts=now_ts,
                calendar=self._calendars[sym.calendar],
            )
            fresh = self._cursor.accept(bars)
            if fresh:
                span = self._cursor.missing_span(sym.uid, fresh[0].close_ts, 60)
                if span is not None:
                    self.gaps_detected.append((sym.uid, *span))
            out.extend(fresh)
        out.sort(key=lambda b: b.close_ts)
        return out

    async def stream(self) -> AsyncIterator[Bar]:
        while True:
            now = dt.datetime.now(dt.UTC).timestamp()
            await asyncio.sleep(next_fetch_delay(now))
            for bar in await self.poll_once(int(dt.datetime.now(dt.UTC).timestamp())):
                yield bar


__all__ = ["OVERLAP_BARS", "BarCursor", "PollingFeed", "next_fetch_delay"]
