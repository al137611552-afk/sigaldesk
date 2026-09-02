"""M0-A 核心验收：1m 聚合出的高周期 bar 与接口原生高周期线逐根对拍。

夹具是 rb2610（真实合约）实盘数据，1m/5m/15m/1h 同批拉取、同源。

**归档区间要求逐根精确一致（价格 + 成交量，零容差）** —— 归档数据在数据源内部自洽，
任何差异都说明我们的分桶或聚合逻辑写错了。

当日盘中数据不参与精确对拍：数据源自身在当日就不自洽（见 test_intraday_source_is_provisional），
这也是"归档以 by-timerange 为准、次日回填校正"这条设计规则的实测依据。
"""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import pytest

from sigdesk.core.models import CST, Bar, Timeframe
from sigdesk.feed.quote_api import normalize_klines
from sigdesk.store.bar_builder import BarBuilder, CalendarBuilder, aggregate

SYMBOL = "CN.SHFE.rb2610"
FUTURE = 4_102_444_800  # 2100 年：把夹具里所有 bar 都判为已收盘
HIGHER = [Timeframe.M5, Timeframe.M15, Timeframe.H1]


def _bars(raw: list[dict[str, Any]], tf: Timeframe) -> list[Bar]:
    return normalize_klines(raw, symbol=SYMBOL, timeframe=tf, now_ts=FUTURE)


def _hhmm(t: int) -> str:
    return dt.datetime.fromtimestamp(t, CST).strftime("%m-%d %H:%M")


def _complete(built: list[Bar], m1: list[Bar]) -> list[Bar]:
    """只保留成分 1m bar 全部落在夹具区间内的桶。

    夹具首根 1m 落在某个高周期桶中间，该桶必然不完整，拿它对拍没有意义。
    这是对拍边界的正当排除，不是放宽断言。
    """
    return [b for b in built if b.open_ts >= m1[0].open_ts]


@pytest.mark.parametrize("tf", HIGHER)
def test_archived_aggregation_is_bit_exact(rb2610_archived: dict[str, Any], tf: Timeframe) -> None:
    m1 = _bars(rb2610_archived["1m"], Timeframe.M1)
    native = {b.close_ts: b for b in _bars(rb2610_archived[tf.value], tf)}
    built = {b.close_ts: b for b in _complete(aggregate(SYMBOL, m1, tf), m1)}

    common = sorted(set(native) & set(built))
    assert len(common) >= 10, f"{tf} 可比对的 bar 太少（{len(common)}），夹具区间有问题"

    bad = [
        f"{_hhmm(t)} {fld}: 接口={getattr(native[t], fld)} 聚合={getattr(built[t], fld)}"
        for t in common
        for fld in ("open", "high", "low", "close", "volume")
        if getattr(native[t], fld) != getattr(built[t], fld)
    ]
    assert not bad, f"{tf} 归档区间不一致 {len(bad)} 处 / {len(common)} 根:\n" + "\n".join(bad[:10])


@pytest.mark.parametrize("tf", HIGHER)
def test_no_missing_or_extra_buckets(rb2610_archived: dict[str, Any], tf: Timeframe) -> None:
    """「跳过空桶」的规则若写错，会表现为整根 bar 的多产出或漏产出。

    这一条覆盖了 10:15 休盘、11:30-13:30 长休、以及 60m 的 12:00/13:00 特殊桶。
    """
    m1 = _bars(rb2610_archived["1m"], Timeframe.M1)
    native = {b.close_ts for b in _bars(rb2610_archived[tf.value], tf)}
    built = {b.close_ts for b in _complete(aggregate(SYMBOL, m1, tf), m1)}
    lo, hi = min(built), max(built)

    missing = sorted({t for t in native if lo <= t <= hi} - built)
    extra = sorted(built - native)
    assert not missing, f"{tf} 漏产出: {[_hhmm(t) for t in missing]}"
    assert not extra, f"{tf} 多产出: {[_hhmm(t) for t in extra]}"


def test_hourly_covers_the_odd_buckets(rb2610_archived: dict[str, Any]) -> None:
    """点名验证实测出来的 60m 特殊桶：存在 12:00，不存在 13:00。"""
    m1 = _bars(rb2610_archived["1m"], Timeframe.M1)
    times = {_hhmm(b.close_ts)[6:] for b in aggregate(SYMBOL, m1, Timeframe.H1)}
    assert "12:00" in times, "11:00-11:30 的半根应聚成收于 12:00 的 60m bar"
    assert "13:00" not in times, "12:00-13:00 无交易，不应产出 13:00 的 60m bar"
    assert {"10:00", "11:00", "14:00", "15:00"} <= times


def test_intraday_source_is_provisional(rb2610_intraday: dict[str, Any]) -> None:
    """留证：数据源**当日**数据自身不自洽，故当日不做精确对拍。

    此测试不是在验证我们的代码，而是把数据源的这个性质钉住 —— 若哪天数据源修好了，
    这个测试会失败，提醒我们可以收紧当日的处理策略。
    """
    m1 = _bars(rb2610_intraday["1m"], Timeframe.M1)
    native = {b.close_ts: b for b in _bars(rb2610_intraday["5m"], Timeframe.M5)}
    built = {b.close_ts: b for b in _complete(aggregate(SYMBOL, m1, Timeframe.M5), m1)}
    common = sorted(set(native) & set(built))

    mismatches = [
        t
        for t in common
        if (native[t].close, native[t].volume) != (built[t].close, built[t].volume)
    ]
    assert mismatches, "数据源当日数据已变得自洽 —— 请重新评估当日回填策略并更新本测试"
    assert len(mismatches) / len(common) < 0.1, (
        f"当日不一致比例 {len(mismatches)}/{len(common)} 异常偏高，可能不只是数据源噪声"
    )
    for t in mismatches:  # 差异幅度必须是"边界一笔成交"级别
        assert abs(native[t].close - built[t].close) <= 2.0
        assert abs(native[t].volume - built[t].volume) <= 10


def test_in_progress_1m_bar_is_ignored(rb2610_archived: dict[str, Any]) -> None:
    """INV-2：未收盘的 1m bar 不得参与聚合，否则高周期 bar 会重绘。"""
    m1 = _bars(rb2610_archived["1m"], Timeframe.M1)
    b = BarBuilder(SYMBOL, Timeframe.M5)
    for bar in m1[:3]:
        b.push(bar)
    before = b.pending()
    assert before is not None

    src = m1[3]
    tentative = Bar(
        symbol=SYMBOL,
        timeframe=Timeframe.M1,
        open_ts=src.open_ts,
        close_ts=src.close_ts,
        open=src.open,
        high=src.high + 999,
        low=src.low,
        close=src.close,
        volume=src.volume,
        closed=False,
    )
    assert b.push(tentative) is None
    assert b.pending() == before


def test_incremental_equals_batch(rb2610_archived: dict[str, Any]) -> None:
    """ADR-0001：增量喂入（实盘）与批量聚合（回测）必须产出完全相同的结果。"""
    m1 = _bars(rb2610_archived["1m"], Timeframe.M1)
    batch = aggregate(SYMBOL, m1, Timeframe.M15)
    b = BarBuilder(SYMBOL, Timeframe.M15)
    streamed = [got for bar in m1 if (got := b.push(bar)) is not None]
    assert streamed == batch


def test_pending_is_never_closed(rb2610_archived: dict[str, Any]) -> None:
    m1 = _bars(rb2610_archived["1m"], Timeframe.M1)
    b = BarBuilder(SYMBOL, Timeframe.H1)
    for bar in m1[:20]:
        b.push(bar)
    pending = b.pending()
    assert pending is not None and pending.closed is False


def test_rejects_time_travel() -> None:
    b = BarBuilder("X", Timeframe.M5)

    def mk(ts_: int) -> Bar:
        return Bar("X", Timeframe.M1, ts_ - 60, ts_, 1, 1, 1, 1, 1)

    b.push(mk(1_000_000_000))
    with pytest.raises(ValueError, match="时间倒流"):
        b.push(mk(1_000_000_000 - 3600))


def test_rejects_non_1m_input() -> None:
    b = BarBuilder("X", Timeframe.M5)
    with pytest.raises(ValueError, match="只接受 1m 输入"):
        b.push(Bar("X", Timeframe.M5, 0, 300, 1, 1, 1, 1, 1))


def test_calendar_builder_accepts_any_smaller_timeframe() -> None:
    """日线可以由 1m 聚合，**也可以直接从接口拉**（interval_range=101）；
    后者再聚合出周线月线时，输入就是 1d。

    原来硬性要求 1m 输入，于是"直接拉日线"这条路走不通 —— 而它正是让
    日/周/月线不再受制于回补了多长 1m 的关键（回补三个月 1m 只得到
    45 根日线、10 根周线、3 根月线）。
    """
    day = Bar(symbol="X", timeframe=Timeframe.D1, open_ts=0, close_ts=86400,
              open=1, high=2, low=0.5, close=1.5, volume=10, closed=True,
              trading_day="2026-09-01")
    out = CalendarBuilder("X", Timeframe.W1).push(day)
    assert out is None, "第一根不该立刻吐出周线"


def test_calendar_builder_still_rejects_equal_or_larger() -> None:
    """保护还在：不能把周线喂进日线聚合器，也不能自己喂自己。"""
    week = Bar(symbol="X", timeframe=Timeframe.W1, open_ts=0, close_ts=86400,
               open=1, high=2, low=0.5, close=1.5, volume=10, closed=True,
               trading_day="2026-09-01")
    for tf in (Timeframe.D1, Timeframe.W1):
        with pytest.raises(ValueError, match="只接受更小的周期"):
            CalendarBuilder("X", tf).push(week)


def test_backfill_only_aggregates_larger_timeframes() -> None:
    """拉日线时不该去聚合 5m/15m —— 无从聚合。

    **必须用 rank 而不是 seconds 比较**：日历周期（日/周/月）的 `seconds` 是 0，
    拿它比较会把 5m~4h 全判成"更大"，然后在 BarBuilder 里炸掉（第一版就是这么错的）。
    """
    assert Timeframe.D1.seconds == 0 and Timeframe.M5.seconds > 0, "前提：日历周期 seconds 为 0"
    assert Timeframe.D1.rank > Timeframe.M5.rank, "rank 才跨墙钟/日历单调"
    src = pathlib.Path("scripts/backfill.py").read_text(encoding="utf-8")
    assert "t.rank > timeframe.rank" in src, "过滤条件用错字段会在拉日线时直接崩"


def test_backfill_documents_not_mixing_sources_for_the_same_range() -> None:
    """接口日线是交易所口径，1m 聚合出的日线是本项目的交易日归属（夜盘归下一交易日），
    对夜盘品种可能不同。同一分区后写覆盖先写，混用会得到一份来源不明的日线。"""
    src = pathlib.Path("scripts/backfill.py").read_text(encoding="utf-8")
    assert "别对同一区间都跑" in src
    assert "--timeframe" in src
