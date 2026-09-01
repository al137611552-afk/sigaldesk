"""分时图数据：当日价格线 + 均价线。纯逻辑，无 IO。

**为什么算在服务端**：均价要用注册表里的 `multiplier`，"当日"要看 `trading_day`
（期货夜盘归属下一交易日）。这两样写在前端 JS 里既拿不到、也测不到 ——
与 K 线标注同一条原则（CLAUDE.md：能算在服务端的就算在服务端）。

**分时不是一个"周期"，是当日 1m 的一种画法**，所以它不进 `Timeframe` 枚举 ——
进了就会污染聚合、规则、回测那几层，而它们跟"怎么画"毫无关系。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from ..core.models import Bar


@dataclass(frozen=True, slots=True)
class IntradayPoint:
    ts: int  # bar 收盘时刻
    price: float
    avg: float | None  # 当日累计均价；算不出来就是 None，**不是 0**（ADR-0006）

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "price": self.price, "avg": self.avg}


def session_day(bar: Bar) -> str:
    """这根 bar 属于哪个"交易日"。期货看 trading_day（夜盘归属下一交易日），
    其余退回 UTC 自然日 —— 与 Parquet 分区键同一口径。"""
    if bar.trading_day:
        return bar.trading_day
    return dt.datetime.fromtimestamp(bar.close_ts, dt.UTC).date().isoformat()


def build_intraday(bars: list[Bar], *, multiplier: float = 1.0) -> tuple[str, list[IntradayPoint]]:
    """取**数据里最后一个交易日**的 1m，算出价格线与均价线。

    刻意用"数据里最后一个交易日"而不是墙钟今天：数据可能是陈的（进程没跑、周末），
    那时画出上一个交易日、并把日期如实标出来，比画一张空图诚实得多。

    均价 = 累计成交额 / (累计成交量 × 合约乘数)。
    **乘数不能漏**：rb 每手 10 吨，money/volume 得到的是 31320 而不是 3132。
    成交额缺失（数据源没给）时返回 None，不用收盘价冒充。
    """
    if not bars:
        return "", []
    day = session_day(bars[-1])
    today = [b for b in bars if b.closed and session_day(b) == day]
    mult = multiplier if multiplier > 0 else 1.0

    out: list[IntradayPoint] = []
    cum_money = 0.0
    cum_vol = 0.0
    for b in today:
        cum_money += b.money
        cum_vol += b.volume
        denom = cum_vol * mult
        avg = cum_money / denom if cum_money > 0 and denom > 0 else None
        out.append(IntradayPoint(ts=b.close_ts, price=b.close, avg=avg))
    return day, out


__all__ = ["IntradayPoint", "build_intraday", "session_day"]
