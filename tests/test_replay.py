"""ReplayFeed 与 **M2 红线**：同一时段 replay 与 live 产出信号逐条一致。

这里的"live"是把 bar 逐根喂进引擎（实盘链路除了数据来源之外完全一样）；
"replay"是把同一批 bar **落进 Parquet 再读回来**，用 ReplayFeed 重放。
所以这条测试同时覆盖三件事：Parquet 往返不失真、回放顺序正确、引擎本身确定。
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from sigdesk.core.models import Bar, Timeframe
from sigdesk.feed.okx import normalize_candles
from sigdesk.feed.replay import ReplayFeed
from sigdesk.rules.engine import RuleEngine
from sigdesk.rules.loader import load_rule
from sigdesk.rules.model import Signal
from sigdesk.store.bar_store import BarStore
from sigdesk.store.parquet_io import write_bars

BTC = "CRYPTO.OKX.BTCUSDT.PERP"
ETH = "CRYPTO.OKX.ETHUSDT.PERP"

MULTI_LEVEL: dict[str, Any] = {
    "id": "redline",
    "universe": [BTC, ETH],
    "timeframes": {"trend": "15m", "setup": "5m", "trigger": "1m"},
    "conditions": [
        {"on": "trend", "mode": "state", "when": "close > ema(close,5)"},
        {"on": "setup", "mode": "window", "within": 4, "when": "rsi(6) < 60"},
        {"on": "trigger", "mode": "event", "when": "cross_up(close, ema(close,10))"},
    ],
    "context": {"atr14": "atr(14)"},
    "context_by_role": {"trend": {"ema5": "ema(close,5)"}, "setup": {"rsi6": "rsi(6)"}},
    "emit": {
        "direction": "long",
        "ttl": "6 bars",
        "cooldown": "10m",
        "dedup_key": "{symbol}:{rule}:{trend_bar_close_ts}",
    },
}


@pytest.fixture(scope="module")
def two_symbol_bars(btc_swap_okx: dict[str, Any]) -> list[Bar]:
    """同一段行情复制成两个标的，用来验证跨标的互不干扰。"""
    btc = normalize_candles(btc_swap_okx["1m"], symbol=BTC, timeframe=Timeframe.M1)
    eth = [
        Bar(ETH, b.timeframe, b.open_ts, b.close_ts, b.open / 30, b.high / 30, b.low / 30,
            b.close / 30, b.volume * 7, b.money, b.open_interest, b.closed, b.trading_day)
        for b in btc
    ]
    merged = btc + eth
    merged.sort(key=lambda b: (b.close_ts, b.symbol))
    return merged


def run_engine(bars: list[Bar]) -> list[Signal]:
    store = BarStore(timeframes=[Timeframe.M5, Timeframe.M15])
    engine = RuleEngine([load_rule(MULTI_LEVEL)], store)
    return [s for b in bars for s in engine.on_bars(store.push(b))]


def by_symbol(signals: list[Signal]) -> dict[str, list[dict[str, Any]]]:
    """按标的分组后再比：跨标的的到达先后本来就不是确定量。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for s in signals:
        out.setdefault(s.symbol, []).append(s.as_dict())
    return out


# ---------------------------------------------------------------- ReplayFeed


def test_replay_feed_reads_back_what_was_written(
    tmp_path: pathlib.Path, two_symbol_bars: list[Bar]
) -> None:
    write_bars(tmp_path, two_symbol_bars)
    feed = ReplayFeed(tmp_path, [BTC, ETH], 0, 2**31)

    bars = feed.bars()

    assert len(bars) == len(two_symbol_bars)
    assert bars == sorted(two_symbol_bars, key=lambda b: (b.close_ts, b.symbol))
    assert feed.span() == (bars[0].close_ts, bars[-1].close_ts)


def test_replay_feed_respects_the_requested_window(
    tmp_path: pathlib.Path, two_symbol_bars: list[Bar]
) -> None:
    write_bars(tmp_path, two_symbol_bars)
    stamps = sorted({b.close_ts for b in two_symbol_bars})
    lo, hi = stamps[10], stamps[20]

    bars = ReplayFeed(tmp_path, [BTC], lo, hi).bars()

    assert [b.close_ts for b in bars] == stamps[11:21], "区间约定是左开右闭"


def test_replay_feed_on_empty_range_is_empty(tmp_path: pathlib.Path) -> None:
    feed = ReplayFeed(tmp_path, [BTC], 0, 100)
    assert feed.bars() == []
    assert feed.span() is None


async def test_replay_feed_stream_matches_bars(
    tmp_path: pathlib.Path, two_symbol_bars: list[Bar]
) -> None:
    write_bars(tmp_path, two_symbol_bars[:50])
    feed = ReplayFeed(tmp_path, [BTC, ETH], 0, 2**31)
    streamed = [b async for b in feed.stream()]
    assert streamed == feed.bars()


# ---------------------------------------------------------------- 红线


def test_replay_and_live_produce_identical_signals(
    tmp_path: pathlib.Path, two_symbol_bars: list[Bar]
) -> None:
    """**M2 红线**：同一时段 replay 与 live 产出的信号逐条一致
    （数量 / 时间戳 / 触发价 / 去重键 / 各级别快照）。

    live：bar 逐根进引擎。
    replay：同一批 bar 先落 Parquet，再由 ReplayFeed 读回来喂同一套规则。
    """
    live = run_engine(two_symbol_bars)
    assert live, "整段行情一次都没触发，这条红线测试就没有说服力"

    write_bars(tmp_path, two_symbol_bars)
    replayed = run_engine(ReplayFeed(tmp_path, [BTC, ETH], 0, 2**31).bars())

    assert by_symbol(replayed) == by_symbol(live)
    assert len(replayed) == len(live)
    assert {s.symbol for s in live} == {BTC, ETH}, "两个标的都该有信号，否则覆盖不足"


def test_replay_is_deterministic_across_runs(
    tmp_path: pathlib.Path, two_symbol_bars: list[Bar]
) -> None:
    """同一条规则跑两次回放，结果必须完全可复现（M3 统计的前提）。"""
    write_bars(tmp_path, two_symbol_bars)
    feed = ReplayFeed(tmp_path, [BTC, ETH], 0, 2**31)
    first = run_engine(feed.bars())
    second = run_engine(feed.bars())
    assert [s.as_dict() for s in first] == [s.as_dict() for s in second]


def test_engine_never_reads_the_wall_clock(
    tmp_path: pathlib.Path, two_symbol_bars: list[Bar], monkeypatch: pytest.MonkeyPatch
) -> None:
    """红线成立的根因：引擎的全部时间都取自 ``bar.close_ts``。

    把 time.time / datetime.now 全部换成会爆炸的桩，整条链路仍要跑通。
    """
    import datetime as dt
    import time

    def boom(*a: object, **k: object) -> float:
        raise AssertionError("引擎读了系统时钟 —— replay 与 live 将不再逐条一致")

    monkeypatch.setattr(time, "time", boom)
    monkeypatch.setattr(time, "monotonic", boom)

    class _NoNow(dt.datetime):
        @classmethod
        def now(cls, tz: object = None) -> dt.datetime:
            raise AssertionError("引擎读了系统时钟")

    monkeypatch.setattr(dt, "datetime", _NoNow)

    assert run_engine(two_symbol_bars)


def test_signals_are_bound_to_bar_time_not_run_time(two_symbol_bars: list[Bar]) -> None:
    """信号的 fired_at 必须落在它那根 bar 的收盘时刻上。"""
    for sig in run_engine(two_symbol_bars):
        assert sig.fired_at % 60 == 0
        assert sig.role_bars["trigger"] == sig.fired_at
        assert sig.role_bars["setup"] % 300 == 0
        assert sig.role_bars["trend"] % 900 == 0
