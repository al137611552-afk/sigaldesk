"""主连拼接。重点钉三件事：锚点选同一根、后复权只平移历史段、缺数据不静默跳过。"""

from __future__ import annotations

import json
import pathlib

import pytest

from sigdesk.core.models import Bar, Timeframe
from sigdesk.store.continuous import (
    AdjustMode,
    MainSegment,
    StitchError,
    contracts_needed,
    parse_main_segments,
    stitch,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
UID = "CN.SHFE.rb.CONT"


def bar(ts: int, close: float, day: str, symbol: str = "x", high: float | None = None) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=Timeframe.M1,
        open_ts=ts - 60,
        close_ts=ts,
        open=close,
        high=high if high is not None else close + 1,
        low=close - 1,
        close=close,
        volume=10.0,
        trading_day=day,
    )


# ---- main-by-date 响应解析 -------------------------------------------------


def test_parse_real_response_groups_by_product() -> None:
    """实测夹具：多品类是一个平铺列表，按 main_variety_code 分组。"""
    rows = json.loads((FIXTURES / "main_by_date_rb_i.json").read_text())["data"]
    rb = parse_main_segments(rows, "rb9999")
    assert [s.contract for s in rb] == ["rb2505", "rb2510", "rb2601", "rb2605", "rb2610"]
    assert rb[0].start_day == "2025-01-01" and rb[-1].end_day == "2026-08-31"
    assert [s.contract for s in parse_main_segments(rows, "i9999")][0] == "i2505"


def test_parse_rejects_empty_instead_of_returning_nothing() -> None:
    """传 rb / rb8888 时接口返回 code:0 + data:null —— 成功状态码配空数据。
    静默返回空列表就会拼出一个空序列，所以这里直接抛。"""
    rows = json.loads((FIXTURES / "main_by_date_rb_i.json").read_text())["data"]
    with pytest.raises(StitchError, match="9999"):
        parse_main_segments(rows, "rb")


def test_parse_rejects_overlapping_segments() -> None:
    rows = [
        {"main_variety_code": "x9999", "variety_code": "a", "start_date": "2026-01-01 00:00:00",
         "end_date": "2026-03-01 00:00:00"},
        {"main_variety_code": "x9999", "variety_code": "b", "start_date": "2026-02-01 00:00:00",
         "end_date": "2026-04-01 00:00:00"},
    ]
    with pytest.raises(StitchError, match="重叠"):
        parse_main_segments(rows, "x9999")


# ---- 拼接 -----------------------------------------------------------------

SEGS = [
    MainSegment("old", "2026-01-01", "2026-01-02"),
    MainSegment("new", "2026-01-03", "2026-01-04"),
]
# 换月前两个合约并行交易：new 恒比 old 贵 20（真实的跨期价差）
OLD = [bar(60, 100, "2026-01-01", "old"), bar(120, 102, "2026-01-02", "old"),
       bar(180, 104, "2026-01-03", "old")]
NEW = [bar(120, 122, "2026-01-02", "new"), bar(180, 124, "2026-01-03", "new"),
       bar(240, 126, "2026-01-04", "new")]


def test_back_diff_shifts_only_history_and_keeps_latest_real() -> None:
    """后复权：最新一段保留真实价格，历史段整体上移，换月处不留假跳空。"""
    res = stitch(SEGS, {"old": OLD, "new": NEW}, symbol=UID)
    assert [b.close for b in res.bars] == [120.0, 122.0, 124.0, 126.0]
    #                                       ^ old 段 +20      ^ new 段原值
    assert [b.trading_day for b in res.bars] == [
        "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"
    ]
    assert all(b.symbol == UID for b in res.bars)
    assert len(res.rollovers) == 1
    r = res.rollovers[0]
    assert (r.from_contract, r.to_contract, r.at_day) == ("old", "new", "2026-01-03")
    assert r.diff == 20.0 and r.cum_offset == 20.0


def test_anchor_uses_a_bar_both_contracts_traded() -> None:
    """锚点必须是同一根 bar。拿"旧合约最后一根"对"新合约第一根"会把
    隔夜跳空算进价差 —— 这里 old 最后一根是 104、new 第一根是 122，
    错误做法会得出 18，正确答案是同根的 122-102=20。"""
    res = stitch(SEGS, {"old": OLD, "new": NEW}, symbol=UID)
    assert res.rollovers[0].anchor_ts == 120  # 换月日之前、两者都有的最后一根
    assert res.rollovers[0].diff == 20.0


def test_ohlc_all_shifted_together() -> None:
    res = stitch(SEGS, {"old": OLD, "new": NEW}, symbol=UID)
    first = res.bars[0]
    assert (first.open, first.high, first.low, first.close) == (120.0, 121.0, 119.0, 120.0)


def test_volume_is_not_shifted() -> None:
    """量与持仓取源合约原值。money 与平移后价格对不上是价差复权的固有代价。"""
    res = stitch(SEGS, {"old": OLD, "new": NEW}, symbol=UID)
    assert all(b.volume == 10.0 for b in res.bars)


def test_adjust_none_keeps_the_real_gap() -> None:
    res = stitch(SEGS, {"old": OLD, "new": NEW}, symbol=UID, adjust=AdjustMode.NONE)
    assert [b.close for b in res.bars] == [100.0, 102.0, 124.0, 126.0]
    assert res.rollovers == []


def test_offsets_accumulate_across_three_contracts() -> None:
    """三段两次换月：最老那段要吃到两次平移的累计值。"""
    segs = [
        MainSegment("a", "2026-01-01", "2026-01-02"),
        MainSegment("b", "2026-01-03", "2026-01-04"),
        MainSegment("c", "2026-01-05", "2026-01-06"),
    ]
    a = [bar(60, 100, "2026-01-01", "a"), bar(120, 100, "2026-01-02", "a")]
    b = [bar(120, 110, "2026-01-02", "b"), bar(180, 110, "2026-01-03", "b"),
         bar(240, 110, "2026-01-04", "b")]
    c = [bar(240, 115, "2026-01-04", "c"), bar(300, 115, "2026-01-05", "c"),
         bar(360, 115, "2026-01-06", "c")]
    res = stitch(segs, {"a": a, "b": b, "c": c}, symbol=UID)
    assert [r.diff for r in res.rollovers] == [10.0, 5.0]
    assert [r.cum_offset for r in res.rollovers] == [15.0, 5.0]  # a 段 +15，b 段 +5
    assert [x.close for x in res.bars] == [115.0, 115.0, 115.0, 115.0, 115.0, 115.0]


def test_missing_contract_is_refused_not_skipped() -> None:
    """跳过缺失段等于凭空造一个大跳空 —— 必须拒绝。"""
    with pytest.raises(StitchError, match="缺少这些合约"):
        stitch(SEGS, {"old": OLD}, symbol=UID)


def test_no_overlap_between_contracts_is_refused() -> None:
    """没有共同 bar 就算不出价差，不能拿相邻两根凑。"""
    lonely = [bar(180, 124, "2026-01-03", "new"), bar(240, 126, "2026-01-04", "new")]
    with pytest.raises(StitchError, match="共同的 bar"):
        stitch(SEGS, {"old": OLD, "new": lonely}, symbol=UID)


def test_negative_prices_after_shift_are_refused() -> None:
    """价差复权在长历史上会击穿零点，那样的序列跑指标毫无意义。"""
    segs = [MainSegment("old", "2026-01-01", "2026-01-02"),
            MainSegment("new", "2026-01-03", "2026-01-04")]
    old = [bar(60, 100, "2026-01-01", "old"), bar(120, 100, "2026-01-02", "old")]
    tiny = dict(timeframe=Timeframe.M1, open=0.5, high=0.6, low=0.4, close=0.5, volume=1.0)
    new = [
        Bar(symbol="new", open_ts=60, close_ts=120, trading_day="2026-01-02", **tiny),
        Bar(symbol="new", open_ts=120, close_ts=180, trading_day="2026-01-03", **tiny),
    ]
    with pytest.raises(StitchError, match="非正数"):
        stitch(segs, {"old": old, "new": new}, symbol=UID)


def test_slicing_uses_trading_day_not_wall_clock_date() -> None:
    """夜盘归属下一交易日。按自然日切会把换月当晚的夜盘切到错误的一边。"""
    # ts=150 这根收在 1/2 晚上，但 trading_day 已经是 1/3（夜盘归属下一交易日）。
    # new 的主力区间从 1/3 起，所以它该被算进 new 段；按自然日切就会漏掉。
    night = Bar(symbol="new", timeframe=Timeframe.M1, open_ts=140, close_ts=150,
                open=124, high=125, low=123, close=124, volume=1.0, trading_day="2026-01-03")
    new = sorted([*NEW, night], key=lambda b: b.close_ts)
    res = stitch(SEGS, {"old": OLD, "new": new}, symbol=UID)
    assert [b.close_ts for b in res.bars] == [60, 120, 150, 180, 240]


def test_contracts_needed_is_deduped_and_ordered() -> None:
    assert contracts_needed(SEGS) == ["old", "new"]
