"""轮询 Feed 的纯逻辑单测（不触网）。"""

from __future__ import annotations

import datetime as dt

import pytest

from sigdesk.core.calendar import MarketCalendar
from sigdesk.core.models import CST, Bar, Market, Symbol, Timeframe
from sigdesk.feed.polling import BarCursor, PollingFeed, next_fetch_delay

RB_CAL = MarketCalendar.from_config(
    "cn_night_23", ["09:00-10:15", "10:30-11:30", "13:30-15:00", "21:00-23:00"], []
)
CALS = {"cn_night_23": RB_CAL}


def ts(text: str) -> int:
    return int(dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=CST).timestamp())


def bar(symbol: str, close_ts: int, *, closed: bool = True) -> Bar:
    return Bar(symbol, Timeframe.M1, close_ts - 60, close_ts, 1, 1, 1, 1, 1, closed=closed)


def sym(uid: str, *, quote_code: str | None = "rb2610", continuous: bool = False) -> Symbol:
    return Symbol(
        uid=uid,
        market=Market.CN_FUTURES,
        exchange="SHFE",
        code="rb2610",
        calendar="cn_night_23",
        quote_code=quote_code,
        is_continuous=continuous,
    )


def test_cursor_dedups_overlapping_fetches() -> None:
    """重叠窗口是补缺手段，但绝不能造成重复产出。"""
    c = BarCursor()
    first = [bar("S", 60), bar("S", 120), bar("S", 180)]
    assert [b.close_ts for b in c.accept(first)] == [60, 120, 180]
    # 下一轮重叠拉取，只有 240 是新的
    again = [bar("S", 120), bar("S", 180), bar("S", 240)]
    assert [b.close_ts for b in c.accept(again)] == [240]


def test_cursor_drops_in_progress_bar() -> None:
    """INV-2：末根进行中 bar 不得产出，否则下游会收到会变的值。"""
    c = BarCursor()
    got = c.accept([bar("S", 60), bar("S", 120, closed=False)])
    assert [b.close_ts for b in got] == [60]
    # 该 bar 收盘后应能正常产出
    assert [b.close_ts for b in c.accept([bar("S", 120)])] == [120]


def test_cursor_is_per_symbol() -> None:
    c = BarCursor()
    c.accept([bar("A", 120)])
    assert [b.close_ts for b in c.accept([bar("B", 60)])] == [60]


def test_cursor_sorts_output() -> None:
    c = BarCursor()
    assert [b.close_ts for b in c.accept([bar("S", 180), bar("S", 60)])] == [60, 180]


def test_missing_span_flags_hole() -> None:
    c = BarCursor()
    c.accept([bar("S", 60)])
    assert c.missing_span("S", 300, 60) == (60, 300)
    assert c.missing_span("S", 120, 60) is None


@pytest.mark.parametrize(
    ("now_offset", "expected"),
    [(0.0, 61.5), (30.0, 31.5), (59.0, 2.5), (59.9, 1.6)],
)
def test_next_fetch_delay_targets_bar_close_plus_delay(now_offset: float, expected: float) -> None:
    """轮询时机对齐到「下一根 bar 收盘 + 1.5s」，给数据源留成交归集时间。"""
    base = 1_787_000_040  # 60 的整数倍（1_787_000_000 不是，余 20）
    assert next_fetch_delay(base + now_offset) == pytest.approx(expected, abs=0.01)


def test_feed_rejects_continuous_symbol() -> None:
    with pytest.raises(ValueError, match="主连序列"):
        PollingFeed(None, [sym("CN.SHFE.rb.CONT", continuous=True)], CALS)  # type: ignore[arg-type]


def test_feed_rejects_symbol_without_quote_code() -> None:
    with pytest.raises(ValueError, match="缺少 quote_code"):
        PollingFeed(None, [sym("CN.SHFE.x", quote_code=None)], CALS)  # type: ignore[arg-type]


def test_feed_only_polls_symbols_in_session() -> None:
    """非交易时段不轮询，避免空转和无谓的接口压力。"""
    feed = PollingFeed(None, [sym("CN.SHFE.rb2610")], CALS)  # type: ignore[arg-type]
    assert feed._active(ts("2026-08-27 10:00")) != []
    assert feed._active(ts("2026-08-27 12:00")) == []  # 午休
    assert feed._active(ts("2026-08-27 23:30")) == []  # rb 夜盘 23:00 收


def test_cursor_seed_sets_resume_position() -> None:
    """播种后，早于播种位置的 bar 被当作已产出过而丢弃（重启回补的前提）。"""
    cursor = BarCursor()
    cursor.seed({"CN.SHFE.rb2610": 1000})
    assert cursor.last_ts("CN.SHFE.rb2610") == 1000
    assert cursor.accept([bar("CN.SHFE.rb2610", 940)]) == []
    assert [b.close_ts for b in cursor.accept([bar("CN.SHFE.rb2610", 1060)])] == [1060]


def test_cursor_seed_never_goes_backwards() -> None:
    """播种值比已产出位置旧时必须忽略，否则会重复产出已发过的 bar。"""
    cursor = BarCursor()
    cursor.accept([bar("CN.SHFE.rb2610", 2000)])
    cursor.seed({"CN.SHFE.rb2610": 1000})
    assert cursor.last_ts("CN.SHFE.rb2610") == 2000


def test_polling_feed_seeded_cursor_ignores_the_overlap_window() -> None:
    """预热后必须播种游标，否则首轮轮询会把整个重叠窗口当新数据重发一遍。

    留证：真跑期货夜盘时首轮重发了 17 根，全部撞上 BarStore 的时间倒流校验。
    重叠窗口本是用来查缺口的，没有游标就变成了重发历史。
    """
    cursor = BarCursor()
    cursor.seed({"CN.SHFE.rb2610": 1000})
    overlap = [bar("CN.SHFE.rb2610", t) for t in range(700, 1121, 60)]
    fresh = cursor.accept(overlap)
    assert [b.close_ts for b in fresh] == [1060, 1120], "重叠窗口里的历史被当成了新数据"
