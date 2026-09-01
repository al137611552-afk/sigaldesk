"""日线聚合。**按交易日，不按自然日** —— 夜盘归属下一交易日。

按自然日切会把每个期货交易日劈成两半（21:00-23:00 归前一天，09:00-15:00 归后一天），
这类错误不会报错，只会让日线指标全错。
"""

from __future__ import annotations

import datetime as dt

import pytest

from sigdesk.core.models import CST, Bar, Timeframe
from sigdesk.rules.engine import RuleEngine
from sigdesk.rules.loader import load_rule
from sigdesk.rules.model import store_timeframes
from sigdesk.store.bar_builder import DayBuilder, aggregate, aggregate_complete, day_key
from sigdesk.store.bar_store import BarStore

UID = "CN.SHFE.rb2610"


def fut(day: str, hh: int, mm: int, close: float, *, offset_days: int = 0) -> Bar:
    """一根期货 1m bar。``day`` 是交易日，墙钟时间由 hh:mm 给出。"""
    date = dt.date.fromisoformat(day) + dt.timedelta(days=offset_days)
    ts = int(dt.datetime(date.year, date.month, date.day, hh, mm, tzinfo=CST).timestamp())
    return Bar(UID, Timeframe.M1, ts - 60, ts, close, close + 1, close - 1, close, 10.0,
               trading_day=day)


def test_day_key_uses_trading_day_when_present() -> None:
    """夜盘那根的自然日是 08-27，交易日是 08-28 —— 必须按后者归组。"""
    night = fut("2026-08-28", 21, 30, 100.0, offset_days=-1)
    assert day_key(night) == "2026-08-28"
    assert dt.datetime.fromtimestamp(night.close_ts, CST).date().isoformat() == "2026-08-27"


def test_day_key_falls_back_to_utc_date_for_crypto() -> None:
    """加密没有 trading_day，退回 UTC 自然日 —— 与 Parquet 分区键同一口径。"""
    b = Bar("CRYPTO.X", Timeframe.M1, 0, 3600, 1, 2, 0.5, 1, 1.0)
    assert day_key(b) == "1970-01-01"


def test_night_session_belongs_to_the_next_trading_day() -> None:
    """一个完整交易日 = 前一晚夜盘 + 当日日盘，合成**一根**日线。"""
    bars = [
        fut("2026-08-28", 21, 30, 100.0, offset_days=-1),  # 8/27 晚，属 8/28
        fut("2026-08-28", 22, 30, 108.0, offset_days=-1),
        fut("2026-08-28", 9, 30, 104.0),                    # 8/28 日盘
        fut("2026-08-28", 14, 59, 106.0),
        fut("2026-08-31", 21, 30, 90.0, offset_days=-3),    # 下一交易日的夜盘 -> 触发收盘
    ]
    daily = aggregate(UID, bars, Timeframe.D1)
    assert len(daily) == 1
    d = daily[0]
    assert d.trading_day == "2026-08-28"
    assert (d.open, d.high, d.low, d.close) == (100.0, 109.0, 99.0, 106.0)
    assert d.volume == 40.0
    # 开在前一晚 21:29，收在当日 14:59 —— 横跨自然日
    assert dt.datetime.fromtimestamp(d.open_ts, CST).date().isoformat() == "2026-08-27"
    assert dt.datetime.fromtimestamp(d.close_ts, CST).date().isoformat() == "2026-08-28"


def test_unfinished_day_is_never_emitted() -> None:
    """当日未走完的日线绝不吐出（INV-2）。"""
    bars = [fut("2026-08-28", 9, 30, 100.0), fut("2026-08-28", 14, 59, 106.0)]
    assert aggregate(UID, bars, Timeframe.D1) == []
    assert len(aggregate_complete(UID, bars, Timeframe.D1)) == 1  # flush 才吐


def test_pending_shows_the_running_day_but_marks_it_open() -> None:
    b = DayBuilder(UID)
    b.push(fut("2026-08-28", 9, 30, 100.0))
    pending = b.pending()
    assert pending is not None and pending.closed is False


def test_daily_closes_at_the_first_bar_of_the_next_trading_day() -> None:
    """滞后一根是设计上的：日线没有墙钟边界，只能靠"日期键变了"来收盘。
    期货的下一根是 21:00 夜盘（已属下一交易日），所以上一日的日线在
    **下一交易日一开盘**就可用。"""
    b = DayBuilder(UID)
    assert b.push(fut("2026-08-28", 14, 59, 106.0)) is None
    emitted = b.push(fut("2026-08-31", 21, 0, 90.0, offset_days=-3))
    assert emitted is not None and emitted.trading_day == "2026-08-28"


def test_time_travel_is_refused() -> None:
    b = DayBuilder(UID)
    b.push(fut("2026-08-31", 9, 30, 100.0))
    with pytest.raises(ValueError, match="时间倒流"):
        b.push(fut("2026-08-28", 9, 30, 100.0))


def test_store_derives_daily_alongside_intraday() -> None:
    store = BarStore(timeframes=[Timeframe.M5, Timeframe.D1])
    days = ["2026-08-26", "2026-08-27", "2026-08-28"]
    for i, day in enumerate(days):
        for m in range(0, 55, 5):
            store.push(fut(day, 9, 5 + m, 100.0 + i))
    view = store.view(UID, 2**31)
    daily = view.bars(Timeframe.D1)
    assert [b.trading_day for b in daily] == days[:-1], "最后一天还没走完，不该吐出"
    assert view.bars(Timeframe.M5), "日线与分钟线可以并存"


def test_rule_can_reference_the_daily_moving_average() -> None:
    """用户要的「价格贴近日线均线」—— 现在写得出来了。"""
    rule = load_rule({
        "id": "r", "universe": [UID],
        "timeframes": {"trend": "1d", "trigger": "5m"},
        "conditions": [
            {"on": "trend", "mode": "state", "when": "close > ema(close, 5)"},
            {"on": "trigger", "mode": "state",
             "when": "abs(close - at('1d', ema(close, 5))) / close < 0.02"},
        ],
        "emit": {"direction": "long"},
    })
    assert rule.required_timeframes == {Timeframe.D1, Timeframe.M5}
    assert store_timeframes([rule]) == [Timeframe.M5, Timeframe.D1], "日线排在最后，不是最前"

    store = BarStore(timeframes=store_timeframes([rule]))
    engine = RuleEngine([rule], store)
    base = dt.date.fromisoformat("2026-06-01")
    for i in range(20):
        day = (base + dt.timedelta(days=i)).isoformat()
        for m in range(0, 55, 5):
            engine.on_bars(store.push(fut(day, 9, 5 + m, 100.0 + i)))
    view = store.view(UID, 2**31)
    assert len(view.bars(Timeframe.D1)) == 19
    ctx = engine._context(UID, Timeframe.M5, view.bars(Timeframe.M5)[-1].close_ts)  # noqa: SLF001
    assert rule.conditions[1].when.evaluate(ctx) is not None, "日线均线取不到值"


# ---- 周线 / 月线 ---------------------------------------------------------


def test_week_key_uses_iso_year_not_calendar_year() -> None:
    """**跨年那几天是真陷阱**：12-29 属于次年第 1 周。
    用日历年会得到 `2025-W01`，字典序排在 `2025-W52` 前面 —— 凭空炸出"时间倒流"。
    """
    from sigdesk.store.bar_builder import period_key

    keys = [period_key(fut(d, 10, 0, 100.0), Timeframe.W1)
            for d in ("2025-12-26", "2025-12-29", "2025-12-31", "2026-01-05")]
    assert keys == ["2025-W52", "2026-W01", "2026-W01", "2026-W02"]
    assert keys == sorted(keys), "分桶键必须字典序单调，时间倒流检查直接比字符串"


def test_month_key_is_zero_padded() -> None:
    from sigdesk.store.bar_builder import period_key

    keys = [period_key(fut(d, 10, 0, 100.0), Timeframe.MON1)
            for d in ("2026-02-10", "2026-09-10", "2026-10-10")]
    assert keys == ["2026-02", "2026-09", "2026-10"]
    assert keys == sorted(keys), "不补零的话 2026-9 会排在 2026-10 后面"


def test_week_bar_spans_the_whole_trading_week() -> None:
    """一周的 OHLC = 周内首根开、最高、最低、末根收。"""
    bars = []
    for day, close in [("2026-03-02", 100.0), ("2026-03-04", 110.0), ("2026-03-06", 104.0)]:
        bars.append(fut(day, 9, 5, close))
    bars.append(fut("2026-03-09", 9, 5, 90.0))  # 下一周，触发上一周收盘
    week = aggregate(UID, bars, Timeframe.W1)
    assert len(week) == 1
    w = week[0]
    assert (w.open, w.close) == (100.0, 104.0)
    assert w.high == 111.0 and w.low == 99.0


def test_unfinished_week_and_month_are_never_emitted() -> None:
    """当期未走完绝不吐出（INV-2），与日线同一条规则。"""
    bars = [fut("2026-03-02", 9, 5, 100.0), fut("2026-03-03", 9, 5, 101.0)]
    assert aggregate(UID, bars, Timeframe.W1) == []
    assert aggregate(UID, bars, Timeframe.MON1) == []
    assert len(aggregate_complete(UID, bars, Timeframe.W1)) == 1


def test_calendar_builder_refuses_intraday() -> None:
    from sigdesk.store.bar_builder import CalendarBuilder

    with pytest.raises(ValueError, match="日历周期"):
        CalendarBuilder(UID, Timeframe.M5)


def test_timeframes_sort_by_rank_not_seconds() -> None:
    """日/周/月的 seconds 都是 0，按 seconds 排会把它们全排到 1m 前面。"""
    order = [t.value for t in sorted(Timeframe, key=lambda t: t.rank)]
    assert order == ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1mon"]
    assert [t.value for t in Timeframe if t.is_calendar] == ["1d", "1w", "1mon"]


def test_store_derives_everything_that_can_be_derived() -> None:
    """默认档必须含日历周期 —— 少一个不会报错，只会让那一格静默停更（踩过）。

    唯一的例外是 `1m` —— 它是输入本身，不是派生出来的。
    """
    from sigdesk.store.bar_store import DEFAULT_TIMEFRAMES

    assert set(DEFAULT_TIMEFRAMES) == set(Timeframe) - {Timeframe.M1}
