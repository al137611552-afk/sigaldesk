"""OKX 归一化与回补的纯逻辑单测（不触网）。

夹具 `btcusdt_swap_okx.json` 是 2026-08-28 从 OKX 真实抓取的归档段。
"""

from __future__ import annotations

from typing import Any

import pytest

from sigdesk.core.models import Bar, Timeframe
from sigdesk.feed.okx import (
    MAX_LIMIT,
    OkxApiError,
    OkxRestClient,
    OkxRestConfig,
    next_page_anchor,
    normalize_candles,
    unwrap_data,
)
from sigdesk.store.bar_builder import aggregate_complete

SYMBOL = "CRYPTO.OKX.BTCUSDT.PERP"

# [ts(开盘,ms), o, h, l, c, vol(张), volCcy(币), volCcyQuote(计价币), confirm]
ROW_CLOSED = ["1787900400000", "79846.1", "79860.0", "79844.6", "79847.1",
              "3844.9", "38.449", "3070207.378", "1"]
ROW_OPEN = ["1787900460000", "79847.1", "79859.3", "79818.0", "79821.0",
            "9827.3", "98.273", "7845868.9389", "0"]


def test_ts_is_open_time_not_close_time() -> None:
    """OKX 的 ts 是开盘时刻 —— 与期货 Quote API 的收盘时刻语义相反，搞反会整体错位一根。"""
    (bar,) = normalize_candles([ROW_CLOSED], symbol=SYMBOL, timeframe=Timeframe.M1)
    assert bar.open_ts == 1787900400
    assert bar.close_ts == 1787900460


def test_closed_comes_from_confirm_not_from_clock() -> None:
    """收盘判据取数据源的 confirm 字段，不依赖本地时钟、也不依赖数组位置。"""
    bars = normalize_candles(
        [ROW_OPEN, ROW_CLOSED], symbol=SYMBOL, timeframe=Timeframe.M1, closed_only=False
    )
    assert [b.closed for b in bars] == [True, False]  # 已按 close_ts 升序
    only_closed = normalize_candles([ROW_OPEN, ROW_CLOSED], symbol=SYMBOL, timeframe=Timeframe.M1)
    assert [b.close_ts for b in only_closed] == [1787900460]


def test_output_is_ascending_though_source_is_descending() -> None:
    """OKX 返回新→旧；下游 BarBuilder 要求时间递增，归一化必须翻正。"""
    rows = [ROW_OPEN, ROW_CLOSED]  # 降序（新的在前）
    bars = normalize_candles(rows, symbol=SYMBOL, timeframe=Timeframe.M1, closed_only=False)
    assert [b.close_ts for b in bars] == sorted(b.close_ts for b in bars)


def test_volume_is_coin_amount_not_contract_count() -> None:
    """volume 取 volCcy（币量）而非 vol（张数）：张数依赖 ctVal，跨品种不可比。"""
    (bar,) = normalize_candles([ROW_CLOSED], symbol=SYMBOL, timeframe=Timeframe.M1)
    assert bar.volume == 38.449
    assert bar.money == 3070207.378
    assert bar.open_interest == 0.0  # candle 接口不含持仓量
    assert bar.trading_day is None  # 加密 7×24，无交易日概念


def test_unwrap_rejects_error_code() -> None:
    """OKX 的 code 是字符串 "0"，不是数字 0。"""
    assert unwrap_data({"code": "0", "data": [ROW_CLOSED]}) == [ROW_CLOSED]
    assert unwrap_data({"code": "0", "data": None}) == []
    with pytest.raises(OkxApiError, match="51001"):
        unwrap_data({"code": "51001", "msg": "Instrument ID does not exist"})


def test_next_page_anchor_is_oldest_ts() -> None:
    assert next_page_anchor([ROW_OPEN, ROW_CLOSED]) == 1787900400000
    assert next_page_anchor([]) is None


# ------------------------------------------------------- 聚合对拍（核心验收项）


def _bars_1m(fixture: dict[str, Any]) -> list[Bar]:
    return normalize_candles(fixture["1m"], symbol=SYMBOL, timeframe=Timeframe.M1)


@pytest.mark.parametrize(
    ("key", "timeframe", "expected_count"),
    [("5m", Timeframe.M5, 60), ("15m", Timeframe.M15, 20), ("1H", Timeframe.H1, 5)],
)
def test_aggregation_matches_source_higher_timeframes(
    btc_swap_okx: dict[str, Any], key: str, timeframe: Timeframe, expected_count: int
) -> None:
    """由 1m 自建的高周期必须与 OKX 自己的高周期线逐根一致。

    价格零容差；成交量因浮点累加允许 1e-9 相对误差。
    """
    ours = aggregate_complete(SYMBOL, _bars_1m(btc_swap_okx), timeframe)
    theirs = normalize_candles(btc_swap_okx[key], symbol=SYMBOL, timeframe=timeframe)
    assert len(ours) == len(theirs) == expected_count

    for a, b in zip(ours, theirs, strict=True):
        assert a.close_ts == b.close_ts, f"{timeframe} 桶边界错位"
        for field in ("open", "high", "low", "close"):
            assert getattr(a, field) == getattr(b, field), (
                f"{timeframe} @{a.close_ts} {field}: "
                f"自建={getattr(a, field)} 源={getattr(b, field)}"
            )
        assert a.volume == pytest.approx(b.volume, rel=1e-9)
        assert a.money == pytest.approx(b.money, rel=1e-9)


def test_fixture_1m_is_contiguous_and_all_closed(btc_swap_okx: dict[str, Any]) -> None:
    """加密 7×24 无休市，1m 序列不该有任何缺口 —— 这是它与期货最大的不同。"""
    bars = _bars_1m(btc_swap_okx)
    assert len(bars) == 300
    assert all(b.closed for b in bars)
    assert [b.close_ts for b in bars] == list(
        range(bars[0].close_ts, bars[0].close_ts + 300 * 60, 60)
    )


# ------------------------------------------------------- 分页回补（打桩，不触网）


class _FakeClient(OkxRestClient):
    """用一整段连续 1m 行喂假分页，验证 fetch_range 的翻页与区间裁剪。"""

    def __init__(self, rows_desc: list[list[str]], page: int = 100) -> None:
        super().__init__(OkxRestConfig(min_interval_s=0.0))
        self._rows = rows_desc  # 降序
        self._page = page
        self.calls: list[int | None] = []

    async def history_candles(  # type: ignore[override]
        self,
        inst_id: str,
        timeframe: Timeframe,
        *,
        after_ms: int | None = None,
        before_ms: int | None = None,
        limit: int = MAX_LIMIT,
    ) -> list[list[str]]:
        self.calls.append(after_ms)
        rows = self._rows if after_ms is None else [r for r in self._rows if int(r[0]) < after_ms]
        return rows[: self._page]


async def test_fetch_range_pages_backwards_without_gap_or_dup(
    btc_swap_okx: dict[str, Any],
) -> None:
    rows = btc_swap_okx["1m"]  # 300 根，降序
    client = _FakeClient(rows, page=100)
    all_bars = normalize_candles(rows, symbol=SYMBOL, timeframe=Timeframe.M1)
    start, end = all_bars[0].close_ts - 1, all_bars[-1].close_ts

    got = await client.fetch_range("BTC-USDT-SWAP", SYMBOL, Timeframe.M1, start, end)

    assert len(client.calls) >= 3, "300 根 / 每页 100 至少要翻 3 页"
    assert [b.close_ts for b in got] == [b.close_ts for b in all_bars], "翻页有重叠或缺口"


async def test_fetch_range_clips_to_requested_window(btc_swap_okx: dict[str, Any]) -> None:
    """左开右闭：start_ts 那根不含，end_ts 那根要含 —— 与 parquet_io.read_range 同约定。"""
    rows = btc_swap_okx["1m"]
    all_bars = normalize_candles(rows, symbol=SYMBOL, timeframe=Timeframe.M1)
    lo, hi = all_bars[10].close_ts, all_bars[20].close_ts
    got = await _FakeClient(rows).fetch_range("BTC-USDT-SWAP", SYMBOL, Timeframe.M1, lo, hi)
    assert [b.close_ts for b in got] == [b.close_ts for b in all_bars[11:21]]


def test_daily_candles_use_24h_periods() -> None:
    """**加密的"一天"就是 24 小时 UTC。**

    `Timeframe.D1` 在模型里是日历周期（`seconds == 0`），因为国内期货的一个交易日
    含前一晚的夜盘、长度不固定。但 OKX 的 1D bar 是定长的，归一化时要补上 86400 ——
    不补的话 `open_ts = close_ts - 0`，日线回补直接报"不是固定长度周期"。
    """
    rows = [["1735689600000", "1", "2", "0.5", "1.5", "10", "10", "15000", "1"]]
    bars = normalize_candles(rows, symbol="X", timeframe=Timeframe.D1)
    assert len(bars) == 1
    assert bars[0].close_ts - bars[0].open_ts == 86400


def test_weekly_and_monthly_are_still_refused() -> None:
    """周线月线是真的不定长，仍然拒绝 —— 它们由日线聚合出来，不从 OKX 直接拉。"""
    rows = [["1735689600000", "1", "2", "0.5", "1.5", "10", "10", "15000", "1"]]
    for tf in (Timeframe.W1, Timeframe.MON1):
        with pytest.raises(ValueError, match="不是固定长度周期"):
            normalize_candles(rows, symbol="X", timeframe=tf)
