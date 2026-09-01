"""运行健康：连接状态、数据新鲜度、缺口。纯逻辑 —— 当前时间由外部传入（CONVENTIONS）。

M3 验收之一是"数据缺口/断连在健康面板可见"。要让它**可见**，得先让它**被记下来**：
Feed 层已经在记 ``reconnects`` / ``gaps_detected`` / ``backfilled``，
这里把它们连同"每个标的最后一根 bar 有多旧"汇成一个快照。

新鲜度判定要按市场分开：加密 7×24，超过一个周期没数据就是异常；
期货有午休、夜盘和休市，非交易时段没数据是**正常**的，拿同一把尺子量会满屏假告警。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.calendar import MarketCalendar
from ..core.models import Bar, Market, Timeframe

# 超过几个周期没有新 bar 就算滞后
STALE_PERIODS = 2.5


@dataclass(slots=True)
class FeedHealth:
    """一路 Feed 的状态。名字与 ``scripts/watch.py`` 里的 pump 标签对应。"""

    name: str
    connected: bool = True
    reconnects: int = 0
    gaps: int = 0
    backfills: int = 0
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "reconnects": self.reconnects,
            "gaps": self.gaps,
            "backfills": self.backfills,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class SymbolHealth:
    symbol: str
    last_close_ts: int = 0
    bars_seen: int = 0
    gaps: list[tuple[int, int]] = field(default_factory=list)

    def as_dict(self, now_ts: int, *, stale: bool, in_session: bool) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "last_close_ts": self.last_close_ts,
            "lag_s": max(0, now_ts - self.last_close_ts) if self.last_close_ts else None,
            "bars_seen": self.bars_seen,
            "in_session": in_session,
            "stale": stale,
            "gaps": [{"from": a, "to": b} for a, b in self.gaps[-20:]],
        }


class HealthMonitor:
    """收集运行健康。喂 bar、记事件，按需出快照。"""

    def __init__(
        self,
        calendars: dict[str, MarketCalendar] | None = None,
        symbol_calendars: dict[str, str] | None = None,
        timeframe: Timeframe = Timeframe.M1,
    ) -> None:
        self._calendars = calendars or {}
        self._symbol_calendars = symbol_calendars or {}
        self._timeframe = timeframe
        self.feeds: dict[str, FeedHealth] = {}
        self.symbols: dict[str, SymbolHealth] = {}
        self.started_at = 0
        self.signals_fired = 0
        self.bar_errors = 0

    # ------------------------------------------------------------ 记录

    def feed(self, name: str) -> FeedHealth:
        return self.feeds.setdefault(name, FeedHealth(name=name))

    def on_bar(self, bar: Bar) -> None:
        if bar.timeframe is not self._timeframe:
            return
        health = self.symbols.setdefault(bar.symbol, SymbolHealth(symbol=bar.symbol))
        period = self._timeframe.seconds
        if health.last_close_ts and bar.close_ts - health.last_close_ts > period:
            # 期货的休盘间断也会命中；面板上用 in_session 区分"正常休市"与"真缺口"
            health.gaps.append((health.last_close_ts, bar.close_ts))
        health.last_close_ts = max(health.last_close_ts, bar.close_ts)
        health.bars_seen += 1

    def observe_feed(self, name: str, feed: object, *, connected: bool = True) -> None:
        """从 Feed 对象上把计数抄过来。

        放在这里而不是 ``scripts/watch.py`` 里，是为了让"缺口/断连能不能到达面板"
        这件事**可测** —— 写在脚本里就只能靠肉眼看日志。
        Feed 们各自暴露 ``reconnects`` / ``gaps_detected`` / ``backfilled``（M0-B/M2 已有）。
        """
        self.on_feed_event(
            name,
            connected=connected,
            reconnects=getattr(feed, "reconnects", None),
            gaps=len(getattr(feed, "gaps_detected", ()) or ()),
            backfills=len(getattr(feed, "backfilled", ()) or ()),
        )

    def on_feed_event(
        self, name: str, *, connected: bool | None = None, reconnects: int | None = None,
        gaps: int | None = None, backfills: int | None = None, error: str | None = None,
    ) -> None:
        f = self.feed(name)
        if connected is not None:
            f.connected = connected
        if reconnects is not None:
            f.reconnects = reconnects
        if gaps is not None:
            f.gaps = gaps
        if backfills is not None:
            f.backfills = backfills
        if error is not None:
            f.last_error = error

    # ------------------------------------------------------------ 判定

    def in_session(self, symbol: str, now_ts: int) -> bool:
        """该标的此刻是否在交易时段内。没有日历信息时按 7×24 处理。"""
        cal_id = self._symbol_calendars.get(symbol)
        cal = self._calendars.get(cal_id) if cal_id else None
        if cal is None:
            return True
        return cal.in_session(now_ts)

    def is_stale(self, symbol: str, now_ts: int) -> bool:
        """数据是否滞后。**非交易时段一律不算滞后** —— 期货午休两小时没数据是正常的，
        用同一把尺子量会满屏假告警。"""
        health = self.symbols.get(symbol)
        if health is None or not health.last_close_ts:
            return False
        if not self.in_session(symbol, now_ts):
            return False
        return now_ts - health.last_close_ts > self._timeframe.seconds * STALE_PERIODS

    def snapshot(self, now_ts: int) -> dict[str, Any]:
        symbols = [
            self.symbols[s].as_dict(
                now_ts, stale=self.is_stale(s, now_ts), in_session=self.in_session(s, now_ts)
            )
            for s in sorted(self.symbols)
        ]
        feeds = [self.feeds[f].as_dict() for f in sorted(self.feeds)]
        problems = [s["symbol"] for s in symbols if s["stale"]]
        problems += [f["name"] for f in feeds if not f["connected"]]
        return {
            "now_ts": now_ts,
            "started_at": self.started_at,
            "uptime_s": max(0, now_ts - self.started_at) if self.started_at else 0,
            "healthy": not problems,
            "problems": problems,
            "signals_fired": self.signals_fired,
            "bar_errors": self.bar_errors,
            "feeds": feeds,
            "symbols": symbols,
            "total_gaps": sum(len(self.symbols[s].gaps) for s in self.symbols),
        }


def market_of(symbol: str) -> Market:
    return Market.CRYPTO if symbol.startswith(Market.CRYPTO.value) else Market.CN_FUTURES


__all__ = ["STALE_PERIODS", "FeedHealth", "HealthMonitor", "SymbolHealth", "market_of"]
