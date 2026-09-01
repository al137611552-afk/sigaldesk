"""指标层单测。核心验收：**增量结果与全量重算逐点一致**。

本文件里的 `*_ref` 是**故意写成朴素的全量重算**（每个位置从头算一遍，O(n²)），
与 `src/sigdesk/indicators/` 里的增量实现完全独立 —— 否则"对拍"就是自己跟自己比，毫无意义。
样本用 M0 抓的真实行情夹具，不用合成数据。
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from sigdesk.core.models import Bar, Timeframe
from sigdesk.feed.okx import normalize_candles
from sigdesk.feed.quote_api import normalize_klines
from sigdesk.indicators.bars import ATR, KDJ, true_range
from sigdesk.indicators.base import Rolling, run
from sigdesk.indicators.series import BOLL, EMA, MACD, RSI, SMA, StdDev, Wilder

REL = 1e-9  # 浮点相对容差


# ---------------------------------------------------------------- 朴素参照实现


def sma_ref(xs: list[float], n: int) -> list[float | None]:
    return [math.fsum(xs[i - n + 1 : i + 1]) / n if i >= n - 1 else None for i in range(len(xs))]


def _smooth_ref(xs: list[float], n: int, alpha: float) -> list[float | None]:
    """从头重算的递推平滑：前 n 个取均值播种，其后逐点递推。"""
    out: list[float | None] = []
    for i in range(len(xs)):
        if i < n - 1:
            out.append(None)
            continue
        cur = math.fsum(xs[:n]) / n
        for x in xs[n : i + 1]:
            cur = cur + alpha * (x - cur)
        out.append(cur)
    return out


def ema_ref(xs: list[float], n: int) -> list[float | None]:
    return _smooth_ref(xs, n, 2.0 / (n + 1))


def wilder_ref(xs: list[float], n: int) -> list[float | None]:
    return _smooth_ref(xs, n, 1.0 / n)


def std_ref(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(xs)):
        if i < n - 1:
            out.append(None)
            continue
        win = xs[i - n + 1 : i + 1]
        mean = math.fsum(win) / n
        out.append(math.sqrt(math.fsum((v - mean) ** 2 for v in win) / n))
    return out


def rsi_ref(xs: list[float], n: int) -> list[float | None]:
    ups = [max(b - a, 0.0) for a, b in zip(xs, xs[1:], strict=False)]
    downs = [max(a - b, 0.0) for a, b in zip(xs, xs[1:], strict=False)]
    up_s, down_s = wilder_ref(ups, n), wilder_ref(downs, n)
    out: list[float | None] = [None]  # 首个样本只用于差分
    for u, d in zip(up_s, down_s, strict=True):
        if u is None or d is None:
            out.append(None)
        elif d == 0.0:
            out.append(100.0 if u > 0.0 else 50.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + u / d))
    return out


def atr_ref(bars: list[Bar], n: int) -> list[float | None]:
    trs = [bars[0].high - bars[0].low]
    trs += [
        max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
        for a, b in zip(bars, bars[1:], strict=False)
    ]
    return wilder_ref(trs, n)


def kdj_ref(bars: list[Bar], n: int, m1: int, m2: int) -> list[tuple[float, float, float] | None]:
    out: list[tuple[float, float, float] | None] = []
    for i in range(len(bars)):
        if i < n - 1:
            out.append(None)
            continue
        k = d = 50.0
        for j in range(n - 1, i + 1):  # 从第一个成形位置起重放
            win = bars[j - n + 1 : j + 1]
            hhv, llv = max(b.high for b in win), min(b.low for b in win)
            rsv = 50.0 if hhv == llv else (bars[j].close - llv) / (hhv - llv) * 100.0
            k = ((m1 - 1) * k + rsv) / m1
            d = ((m2 - 1) * d + k) / m2
        out.append((k, d, 3.0 * k - 2.0 * d))
    return out


# ---------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def cn_bars(rb2610_archived: dict[str, Any]) -> list[Bar]:
    return normalize_klines(
        rb2610_archived["1m"], symbol="CN.SHFE.rb2610", timeframe=Timeframe.M1, now_ts=2**31
    )


@pytest.fixture(scope="module")
def crypto_bars(btc_swap_okx: dict[str, Any]) -> list[Bar]:
    return normalize_candles(
        btc_swap_okx["1m"], symbol="CRYPTO.OKX.BTCUSDT.PERP", timeframe=Timeframe.M1
    )


def assert_same(
    got: list[float | None], want: list[float | None], label: str, rel: float = REL
) -> None:
    assert len(got) == len(want), f"{label}: 长度不一致"
    for i, (a, b) in enumerate(zip(got, want, strict=True)):
        assert (a is None) == (b is None), f"{label}[{i}]: 预热期不一致 增量={a} 重算={b}"
        if a is not None and b is not None:
            assert a == pytest.approx(b, rel=rel), f"{label}[{i}]: 增量={a} 重算={b}"


# ------------------------------------------- 验收：增量 == 全量重算（两个市场）


@pytest.mark.parametrize("market", ["cn", "crypto"])
@pytest.mark.parametrize("window", [5, 20, 60])
def test_sma_incremental_matches_full_recompute(
    market: str, window: int, cn_bars: list[Bar], crypto_bars: list[Bar]
) -> None:
    xs = [b.close for b in (cn_bars if market == "cn" else crypto_bars)]
    assert_same(run(SMA(window), xs), sma_ref(xs, window), f"SMA({window})")


@pytest.mark.parametrize("market", ["cn", "crypto"])
@pytest.mark.parametrize("window", [5, 20, 60])
def test_ema_incremental_matches_full_recompute(
    market: str, window: int, cn_bars: list[Bar], crypto_bars: list[Bar]
) -> None:
    xs = [b.close for b in (cn_bars if market == "cn" else crypto_bars)]
    assert_same(run(EMA(window), xs), ema_ref(xs, window), f"EMA({window})")


@pytest.mark.parametrize("market", ["cn", "crypto"])
def test_wilder_incremental_matches_full_recompute(
    market: str, cn_bars: list[Bar], crypto_bars: list[Bar]
) -> None:
    xs = [b.close for b in (cn_bars if market == "cn" else crypto_bars)]
    assert_same(run(Wilder(14), xs), wilder_ref(xs, 14), "Wilder(14)")


@pytest.mark.parametrize("market", ["cn", "crypto"])
def test_stddev_incremental_matches_full_recompute(
    market: str, cn_bars: list[Bar], crypto_bars: list[Bar]
) -> None:
    xs = [b.close for b in (cn_bars if market == "cn" else crypto_bars)]
    assert_same(run(StdDev(20), xs), std_ref(xs, 20), "StdDev(20)")


@pytest.mark.parametrize("market", ["cn", "crypto"])
@pytest.mark.parametrize("window", [6, 14])
def test_rsi_incremental_matches_full_recompute(
    market: str, window: int, cn_bars: list[Bar], crypto_bars: list[Bar]
) -> None:
    xs = [b.close for b in (cn_bars if market == "cn" else crypto_bars)]
    assert_same(run(RSI(window), xs), rsi_ref(xs, window), f"RSI({window})")


@pytest.mark.parametrize("market", ["cn", "crypto"])
def test_macd_incremental_matches_full_recompute(
    market: str, cn_bars: list[Bar], crypto_bars: list[Bar]
) -> None:
    xs = [b.close for b in (cn_bars if market == "cn" else crypto_bars)]
    fast, slow, signal = 12, 26, 9
    got = run(MACD(fast, slow, signal), xs)

    f, s = ema_ref(xs, fast), ema_ref(xs, slow)
    difs = [a - b if a is not None and b is not None else None for a, b in zip(f, s, strict=True)]
    solid = [d for d in difs if d is not None]
    dea_solid = ema_ref(solid, signal)
    dea = [None] * (len(difs) - len(solid)) + dea_solid

    assert_same([g.dif if g else None for g in got],
                [d if e is not None else None for d, e in zip(difs, dea, strict=True)], "MACD.dif")
    assert_same([g.dea if g else None for g in got], list(dea), "MACD.dea")
    assert_same(
        [g.hist if g else None for g in got],
        [2.0 * (d - e) if d is not None and e is not None else None
         for d, e in zip(difs, dea, strict=True)],
        "MACD.hist",
    )


@pytest.mark.parametrize("market", ["cn", "crypto"])
def test_boll_incremental_matches_full_recompute(
    market: str, cn_bars: list[Bar], crypto_bars: list[Bar]
) -> None:
    xs = [b.close for b in (cn_bars if market == "cn" else crypto_bars)]
    got = run(BOLL(20, 2.0), xs)
    mid, std = sma_ref(xs, 20), std_ref(xs, 20)
    assert_same([g.mid if g else None for g in got], mid, "BOLL.mid")
    assert_same(
        [g.upper if g else None for g in got],
        [m + 2.0 * s if m is not None and s is not None else None
         for m, s in zip(mid, std, strict=True)],
        "BOLL.upper",
    )
    assert_same(
        [g.lower if g else None for g in got],
        [m - 2.0 * s if m is not None and s is not None else None
         for m, s in zip(mid, std, strict=True)],
        "BOLL.lower",
    )


@pytest.mark.parametrize("market", ["cn", "crypto"])
def test_atr_incremental_matches_full_recompute(
    market: str, cn_bars: list[Bar], crypto_bars: list[Bar]
) -> None:
    bars = cn_bars if market == "cn" else crypto_bars
    ind = ATR(14)
    assert_same([ind.update(b) for b in bars], atr_ref(bars, 14), "ATR(14)")


@pytest.mark.parametrize("market", ["cn", "crypto"])
def test_kdj_incremental_matches_full_recompute(
    market: str, cn_bars: list[Bar], crypto_bars: list[Bar]
) -> None:
    bars = cn_bars if market == "cn" else crypto_bars
    ind = KDJ(9, 3, 3)
    got = [ind.update(b) for b in bars]
    want = kdj_ref(bars, 9, 3, 3)
    assert_same([g.k if g else None for g in got], [w[0] if w else None for w in want], "KDJ.k")
    assert_same([g.d if g else None for g in got], [w[1] if w else None for w in want], "KDJ.d")
    assert_same([g.j if g else None for g in got], [w[2] if w else None for w in want], "KDJ.j")


def test_indicators_work_on_volume_series(cn_bars: list[Bar]) -> None:
    """量能就是把同一批指标作用在成交量上 —— 不需要单独的"量能指标"。"""
    vols = [b.volume for b in cn_bars]
    assert_same(run(SMA(20), vols), sma_ref(vols, 20), "SMA(volume,20)")


# ---------------------------------------------------------------- 预热期与边界


@pytest.mark.parametrize(
    ("factory", "first_value_at"),
    [
        (lambda: SMA(20), 20),
        (lambda: EMA(20), 20),  # SMA 播种 ⇒ 第 20 个样本才出值
        (lambda: Wilder(14), 14),
        (lambda: StdDev(20), 20),
        (lambda: RSI(14), 15),  # 首个样本只用于差分，故比窗口多一根
        (lambda: MACD(12, 26, 9), 34),  # slow(26) 出值后还要再攒 signal(9) 根
        (lambda: BOLL(20, 2.0), 20),
    ],
)
def test_warmup_returns_none_until_defined(factory: Any, first_value_at: int) -> None:
    """预热期必须返回 None，绝不能拿 0 顶替 —— 用 0 会让"均线还没形成"
    看起来像"价格跌到 0"，是最典型的假信号来源。"""
    xs = [100.0 + i for i in range(80)]
    out = run(factory(), xs)
    assert all(v is None for v in out[: first_value_at - 1]), "预热期出现了非 None"
    assert out[first_value_at - 1] is not None, "该出值的位置仍是 None"


def test_atr_and_kdj_warmup(cn_bars: list[Bar]) -> None:
    atr = ATR(14)
    got = [atr.update(b) for b in cn_bars[:20]]
    assert got[12] is None and got[13] is not None

    kdj = KDJ(9, 3, 3)
    got_k = [kdj.update(b) for b in cn_bars[:20]]
    assert got_k[7] is None and got_k[8] is not None


# ---------------------------------------------------------------- 除零与退化


def test_rsi_handles_one_sided_moves() -> None:
    """单边上涨时跌幅平滑为 0，不做保护就是除零崩溃。"""
    assert run(RSI(14), [100.0 + i for i in range(30)])[-1] == 100.0
    assert run(RSI(14), [100.0 - i for i in range(30)])[-1] == 0.0
    assert run(RSI(14), [100.0] * 30)[-1] == 50.0  # 完全走平：涨跌均为 0


def test_kdj_handles_frozen_market() -> None:
    """涨跌停封死时窗口内 high == low，rsv 除零 —— 定义为 50。"""
    flat = [Bar("X", Timeframe.M1, i * 60, (i + 1) * 60, 5, 5, 5, 5, 1) for i in range(20)]
    kdj = KDJ(9, 3, 3)
    out = [kdj.update(b) for b in flat][-1]
    assert out is not None and out.k == pytest.approx(50.0)


def test_boll_width_handles_zero_mid() -> None:
    from sigdesk.indicators.series import BollValue

    assert BollValue(mid=0.0, upper=1.0, lower=-1.0).width == 0.0
    assert BollValue(mid=10.0, upper=11.0, lower=9.0).width == pytest.approx(0.2)


def test_true_range_first_bar_has_no_prev_close(cn_bars: list[Bar]) -> None:
    bar = cn_bars[0]
    assert true_range(bar, None) == bar.high - bar.low


# ---------------------------------------------------------------- 参数校验与数值


@pytest.mark.parametrize("factory", [lambda: SMA(0), lambda: EMA(0), lambda: Wilder(0)])
def test_invalid_window_rejected(factory: Any) -> None:
    with pytest.raises(ValueError, match="窗口"):
        factory()


def test_macd_rejects_fast_ge_slow() -> None:
    with pytest.raises(ValueError, match="必须小于"):
        MACD(26, 12, 9)


def test_wilder_is_not_ema() -> None:
    """Wilder 的 alpha=1/n 与 EMA 的 2/(n+1) 不是一回事；混用会让 RSI 明显偏离主流软件。"""
    xs = [100.0 + (i % 7) for i in range(200)]
    assert run(Wilder(14), xs)[-1] != pytest.approx(run(EMA(14), xs)[-1], rel=1e-6)
    # Wilder(n) 等价于 EMA(2n-1)
    assert run(Wilder(14), xs)[-1] == pytest.approx(run(EMA(27), xs)[-1], rel=1e-6)


def test_rolling_sum_does_not_drift_over_long_runs() -> None:
    """朴素的加新减旧会让浮点误差单调累积；定期用 fsum 重算把误差钉在一个窗口内。"""
    roll = Rolling(20)
    for i in range(200_000):
        roll.push(1e8 + (i % 13) * 0.1)
    assert roll.sum == pytest.approx(math.fsum(roll.values), rel=0, abs=1e-6)
