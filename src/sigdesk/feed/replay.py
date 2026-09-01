"""历史回放 Feed：从 Parquet 读出已归档的 bar，按时间顺序重放。

三种 Feed（期货轮询 / 加密 WS / 历史回放）共用同一出口（ADR-0001），所以回测、
"昨天为什么没报"的复盘、以及 M2 的红线对拍，走的都是与实盘**完全同一条**引擎路径。

**多标的的归并顺序**：同一 close_ts 上多个标的的先后是任意的 —— 实盘里谁先到就谁先到。
本实现按 ``(close_ts, symbol)`` 排序，保证回放自身可复现；
但比对 replay 与 live 的信号时必须**按标的分组后再比**，
因为跨标的的先后本来就不是确定量（每个标的的状态互相独立，先后不影响各自的结论）。
"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from ..core.models import Bar, Timeframe
from ..store.parquet_io import read_range


@dataclass(frozen=True, slots=True)
class ReplayFeed:
    """把 ``data/`` 下某区间的历史 bar 当成一条实时流重放。"""

    root: pathlib.Path
    symbols: Sequence[str]
    start_ts: int
    end_ts: int
    timeframe: Timeframe = Timeframe.M1

    def bars(self) -> list[Bar]:
        """读取并归并，按 (close_ts, symbol) 升序。纯逻辑（除读盘外），便于直接喂给引擎。"""
        out: list[Bar] = []
        for symbol in self.symbols:
            out.extend(
                read_range(self.root, symbol, self.timeframe, self.start_ts, self.end_ts)
            )
        out.sort(key=lambda b: (b.close_ts, b.symbol))
        return out

    async def stream(self) -> AsyncIterator[Bar]:
        """按 ``feed.base.Feed`` 的契约产出。区间放完即结束（实时 Feed 永不结束）。"""
        for bar in self.bars():
            yield bar

    def span(self) -> tuple[int, int] | None:
        """实际覆盖到的区间，便于报告"要重放的和真读到的是不是一回事"。"""
        bars = self.bars()
        if not bars:
            return None
        return bars[0].close_ts, bars[-1].close_ts


__all__ = ["ReplayFeed"]
