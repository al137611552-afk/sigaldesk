"""BarStore 与 as-of 视图单测。核心是 INV-1：视图必须物理截断未来数据。

两个市场（中国期货 / 加密永续）用**同一批断言**跑，验证 M0-B 的
「两个市场的 bar 进入同一 BarStore，as-of 视图行为一致」这条验收。
"""

from __future__ import annotations

from typing import Any

import pytest

from sigdesk.core.calendar import MarketCalendar
from sigdesk.core.models import Bar, Timeframe
from sigdesk.feed.okx import normalize_candles
from sigdesk.feed.quote_api import normalize_klines
from sigdesk.store.bar_builder import aggregate, aggregate_complete
from sigdesk.store.bar_store import BarStore, BarView

CN_UID = "CN.SHFE.rb2610"
CRYPTO_UID = "CRYPTO.OKX.BTCUSDT.PERP"
RB_CAL = MarketCalendar.from_config(
    "cn_night_23", ["09:00-10:15", "10:30-11:30", "13:30-15:00", "21:00-23:00"], []
)


@pytest.fixture
def cn_1m(rb2610_archived: dict[str, Any]) -> list[Bar]:
    """期货：有夜盘、有休盘跳空、带 trading_day。"""
    return normalize_klines(
        rb2610_archived["1m"],
        symbol=CN_UID,
        timeframe=Timeframe.M1,
        now_ts=2**31,
        calendar=RB_CAL,
    )


@pytest.fixture
def crypto_1m(btc_swap_okx: dict[str, Any]) -> list[Bar]:
    """加密：7×24 连续、无 trading_day。"""
    return normalize_candles(btc_swap_okx["1m"], symbol=CRYPTO_UID, timeframe=Timeframe.M1)


def bar(symbol: str, close_ts: int, close: float = 1.0, *, closed: bool = True) -> Bar:
    return Bar(symbol, Timeframe.M1, close_ts - 60, close_ts, 1, 1, 1, close, 1, closed=closed)


# ---------------------------------------------------------------- 派生与一致性


@pytest.mark.parametrize("market", ["cn", "crypto"])
@pytest.mark.parametrize("tf", [Timeframe.M5, Timeframe.M15, Timeframe.H1])
def test_derived_series_match_batch_aggregation(
    market: str, tf: Timeframe, cn_1m: list[Bar], crypto_1m: list[Bar]
) -> None:
    """增量派生（逐根 push）必须与批量聚合结果一致 —— 实盘与回测走同一条路径（ADR-0001）。"""
    bars_1m = cn_1m if market == "cn" else crypto_1m
    uid = bars_1m[0].symbol
    store = BarStore()
    for b in bars_1m:
        store.push(b)

    got = store.view(uid, as_of=2**31).bars(tf)
    # 基准是"已确认收盘的桶"（aggregate 不 flush）。若数据正好停在桶边界上，
    # 末桶也算已收盘 —— 桶边界那根 1m 就是本桶最后一根。
    expected = aggregate(uid, bars_1m, tf)
    assert [b.close_ts for b in got] == [b.close_ts for b in expected]
    assert list(got) == expected
    assert all(b.close_ts % tf.seconds == 0 for b in got), "高周期 bar 必须落在墙钟边界上"


@pytest.mark.parametrize("market", ["cn", "crypto"])
def test_push_returns_newly_closed_bars(
    market: str, cn_1m: list[Bar], crypto_1m: list[Bar]
) -> None:
    """push 的返回值就是"本次新收盘的 bar"，落盘/触发规则都靠它，不必回头扫序列。"""
    bars_1m = cn_1m if market == "cn" else crypto_1m
    store = BarStore(timeframes=[Timeframe.M5])
    emitted = [store.push(b) for b in bars_1m]

    assert all(out[0].timeframe is Timeframe.M1 for out in emitted), "第一项恒为这根 1m 自身"
    m5 = [b for out in emitted for b in out if b.timeframe is Timeframe.M5]
    assert m5 == list(store.view(bars_1m[0].symbol, as_of=2**31).bars(Timeframe.M5))


# ---------------------------------------------------------------- INV-1 截断


@pytest.mark.parametrize("market", ["cn", "crypto"])
def test_view_truncates_future_bars(market: str, cn_1m: list[Bar], crypto_1m: list[Bar]) -> None:
    """INV-1：as_of 之后的 bar 一根都不许出现（左闭右闭，as_of 那根算已知）。"""
    bars_1m = cn_1m if market == "cn" else crypto_1m
    uid = bars_1m[0].symbol
    store = BarStore()
    for b in bars_1m:
        store.push(b)

    as_of = bars_1m[100].close_ts
    view = store.view(uid, as_of)
    for tf in (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1):
        got = view.bars(tf)
        assert all(b.close_ts <= as_of for b in got), f"{tf} 漏出了未来 bar"
        assert all(b.closed for b in got)
    assert view.last(Timeframe.M1) is not None
    assert view.last(Timeframe.M1).close_ts == as_of  # type: ignore[union-attr]


@pytest.mark.parametrize("market", ["cn", "crypto"])
def test_view_is_frozen_against_later_pushes(
    market: str, cn_1m: list[Bar], crypto_1m: list[Bar]
) -> None:
    """视图一旦构造就定死：之后 store 再收到数据，旧视图也看不到。

    这是 INV-1 的实质 —— 规则求值期间即使有新 bar 到达，也不会偷看到未来。
    """
    bars_1m = cn_1m if market == "cn" else crypto_1m
    uid = bars_1m[0].symbol
    store = BarStore()
    for b in bars_1m[:100]:
        store.push(b)

    view = store.view(uid, as_of=bars_1m[99].close_ts)
    before = len(view.bars(Timeframe.M1))
    for b in bars_1m[100:200]:
        store.push(b)

    assert len(view.bars(Timeframe.M1)) == before
    assert len(store.view(uid, as_of=2**31).bars(Timeframe.M1)) == 200


def test_view_of_unknown_symbol_is_empty_not_error() -> None:
    """规则可能引用尚未收到数据的标的 —— 那是"暂无数据"，不是配置错误。"""
    view = BarStore().view("CRYPTO.OKX.DOGEUSDT.PERP", as_of=2**31)
    assert view.bars(Timeframe.M1) == ()
    assert view.last(Timeframe.M5) is None
    assert view.closes(Timeframe.H1) == []


def test_view_accepts_timeframe_string() -> None:
    """ARCHITECTURE 里写的是 view.bars("1h")，字符串入口要能用。"""
    store = BarStore()
    store.push(bar(CRYPTO_UID, 3600, close=7.0))
    view = store.view(CRYPTO_UID, as_of=3600)
    assert view.bars("1m") == view.bars(Timeframe.M1)
    assert view.closes("1m") == [7.0]


def test_both_markets_share_one_store(cn_1m: list[Bar], crypto_1m: list[Bar]) -> None:
    """M0-B 验收项：两个市场进同一个 BarStore，互不干扰、行为一致。"""
    store = BarStore()
    for b in cn_1m[:120]:
        store.push(b)
    for b in crypto_1m[:120]:
        store.push(b)

    assert store.symbols() == sorted([CN_UID, CRYPTO_UID])
    for uid, src in ((CN_UID, cn_1m), (CRYPTO_UID, crypto_1m)):
        view = store.view(uid, as_of=src[59].close_ts)
        assert len(view.bars(Timeframe.M1)) == 60
        assert {b.symbol for b in view.bars(Timeframe.M1)} == {uid}
    # 期货带 trading_day、加密不带 —— 同一个 store 里两种语义并存
    assert store.view(CN_UID, 2**31).bars(Timeframe.M1)[0].trading_day is not None
    assert store.view(CRYPTO_UID, 2**31).bars(Timeframe.M1)[0].trading_day is None


# ---------------------------------------------------------------- 投递健壮性


def test_duplicate_push_is_ignored() -> None:
    """重叠窗口轮询与重连回补都会重投，重复不得进入序列。"""
    store = BarStore(timeframes=[Timeframe.M5])
    assert store.push(bar(CRYPTO_UID, 60))
    assert store.push(bar(CRYPTO_UID, 60)) == []
    assert len(store.view(CRYPTO_UID, 2**31).bars(Timeframe.M1)) == 1


def test_unclosed_bar_never_enters_store() -> None:
    """INV-2：进行中的 bar 不入库，否则序列会被临时值污染。"""
    store = BarStore()
    assert store.push(bar(CRYPTO_UID, 60, closed=False)) == []
    assert store.view(CRYPTO_UID, 2**31).bars(Timeframe.M1) == ()


def test_backwards_bar_raises() -> None:
    """时间倒流是真 bug（Feed 排序坏了），不该被悄悄吞掉。"""
    store = BarStore(timeframes=[Timeframe.M5])
    store.push(bar(CRYPTO_UID, 300))
    store.push(bar(CRYPTO_UID, 360))
    with pytest.raises(ValueError, match="倒流"):
        store.push(bar(CRYPTO_UID, 120))


def test_push_rejects_non_1m() -> None:
    store = BarStore()
    five = Bar(CRYPTO_UID, Timeframe.M5, 0, 300, 1, 1, 1, 1, 1)
    with pytest.raises(ValueError, match="load"):
        store.push(five)


# ---------------------------------------------------------------- 历史装载


def test_load_history_then_serve_as_of(crypto_1m: list[Bar]) -> None:
    """从 Parquet 读出的历史直接 load 进来，视图立刻可用。"""
    store = BarStore()
    store.load(crypto_1m[:50])
    store.load(aggregate_complete(CRYPTO_UID, crypto_1m[:50], Timeframe.M5))

    view = store.view(CRYPTO_UID, as_of=crypto_1m[29].close_ts)
    assert len(view.bars(Timeframe.M1)) == 30
    assert all(b.close_ts <= view.as_of for b in view.bars(Timeframe.M5))


def test_load_same_close_ts_overwrites(crypto_1m: list[Bar]) -> None:
    """次日用权威数据回填校正：同一根被新值覆盖，而不是并排出现两根。"""
    store = BarStore()
    store.load(crypto_1m[:10])
    corrected = crypto_1m[5]
    fixed = Bar(**{**{f: getattr(corrected, f) for f in corrected.__slots__}, "close": 12345.0})
    store.load([fixed])

    bars = store.view(CRYPTO_UID, 2**31).bars(Timeframe.M1)
    assert len(bars) == 10
    assert bars[5].close == 12345.0


def test_load_ignores_unclosed(crypto_1m: list[Bar]) -> None:
    tentative = Bar(
        CRYPTO_UID, Timeframe.M1, 0, 60, 1, 1, 1, 1, 1, closed=False
    )
    store = BarStore()
    store.load([tentative])
    assert store.view(CRYPTO_UID, 2**31).bars(Timeframe.M1) == ()


def test_series_are_trimmed_to_max_bars() -> None:
    """内存有上限（开发机 2核4G），超出后裁掉最旧的，视图仍然自洽。"""
    store = BarStore(timeframes=[], max_bars=100)
    for i in range(1, 1200):
        store.push(bar(CRYPTO_UID, i * 60))
    bars = store.view(CRYPTO_UID, 2**31).bars(Timeframe.M1)
    assert 100 <= len(bars) <= 100 + 512
    assert bars[-1].close_ts == 1199 * 60
    assert [b.close_ts for b in bars] == sorted(b.close_ts for b in bars)


def test_view_repr_is_informative() -> None:
    store = BarStore(timeframes=[Timeframe.M5])
    store.push(bar(CRYPTO_UID, 300))
    assert "CRYPTO.OKX.BTCUSDT.PERP" in repr(store.view(CRYPTO_UID, 300))


def test_store_supports_daily_via_trading_day() -> None:
    """日线不走墙钟分桶，走交易日归属（DayBuilder）。"""
    store = BarStore(timeframes=[Timeframe.D1])
    assert store.timeframes == (Timeframe.D1,)


def test_store_rejects_1m_as_a_derived_timeframe() -> None:
    """1m 是输入本身，不是派生出来的。"""
    with pytest.raises(ValueError, match="输入本身"):
        BarStore(timeframes=[Timeframe.M1])


def test_barview_type_is_exported() -> None:
    assert isinstance(BarStore().view("x", 0), BarView)


def test_resume_map_reports_last_position_per_symbol(crypto_1m: list[Bar]) -> None:
    """预热完成后拿它给 Feed 播种（见 test_polling 里那条留证）。"""
    store = BarStore(timeframes=[])
    assert store.resume_map() == {}
    assert store.last_close_ts(CRYPTO_UID) is None

    for b in crypto_1m[:20]:
        store.push(b)
    store.push(bar(CN_UID, 999_999_999))

    assert store.last_close_ts(CRYPTO_UID) == crypto_1m[19].close_ts
    assert store.resume_map() == {
        CRYPTO_UID: crypto_1m[19].close_ts,
        CN_UID: 999_999_999,
    }


def test_resume_map_is_per_timeframe(crypto_1m: list[Bar]) -> None:
    """5m 的续播位置是「不晚于 1m 位置的最近一个 5m 边界」。

    两者**可能相等**（1m 正好收在 5m 边界上时，5m 桶同刻收盘），所以不能断言严格小于。
    """
    store = BarStore(timeframes=[Timeframe.M5])
    for b in crypto_1m[:22]:
        store.push(b)
    m1 = store.last_close_ts(CRYPTO_UID)
    m5 = store.last_close_ts(CRYPTO_UID, Timeframe.M5)
    assert m1 is not None and m5 is not None
    assert m5 % 300 == 0
    assert m5 <= m1 < m5 + 300
