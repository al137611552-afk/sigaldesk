"""需要 OHLC 的指标。输入是 Bar，不是单个数值。

与 series.py 的分工：能作用在任意数值序列上的（收盘价/成交量都行）放 series.py；
必须同时看高开低收的（真实波幅、随机指标）放这里。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Bar
from .base import Rolling
from .series import Wilder


def true_range(bar: Bar, prev_close: float | None) -> float:
    """真实波幅。首根没有前收，退化为当根高低差 —— 这是 Wilder 原著的处理。"""
    if prev_close is None:
        return bar.high - bar.low
    return max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))


class ATR:
    """平均真实波幅。Wilder 平滑（alpha=1/n），**不是** EMA(n)。"""

    __slots__ = ("_prev_close", "_wilder")

    def __init__(self, window: int = 14) -> None:
        self._wilder = Wilder(window)
        self._prev_close: float | None = None

    def update(self, bar: Bar) -> float | None:
        tr = true_range(bar, self._prev_close)
        self._prev_close = bar.close
        return self._wilder.update(tr)

    @property
    def value(self) -> float | None:
        return self._wilder.value


@dataclass(frozen=True, slots=True)
class KdjValue:
    k: float
    d: float
    j: float


class KDJ:
    """随机指标（国内 KDJ 口径）。

    ``rsv = (close - LLV(low,n)) / (HHV(high,n) - LLV(low,n)) * 100``；
    ``k = (2*k_prev + rsv)/3``、``d = (2*d_prev + k)/3``、``j = 3k - 2d``，
    k/d 均以 **50** 起步 —— 这是国内软件的通行做法（ADR-0006）。

    最高价等于最低价（窗口内完全无波动，如涨跌停封死）时 rsv 定义为 50，
    不做保护会直接除零崩溃。
    """

    __slots__ = ("_d", "_highs", "_k", "_lows", "_m1", "_m2", "_value")

    def __init__(self, window: int = 9, m1: int = 3, m2: int = 3) -> None:
        if m1 < 1 or m2 < 1:
            raise ValueError("平滑参数必须 >= 1")
        self._highs = Rolling(window)
        self._lows = Rolling(window)
        self._m1, self._m2 = m1, m2
        self._k = 50.0
        self._d = 50.0
        self._value: KdjValue | None = None

    def update(self, bar: Bar) -> KdjValue | None:
        self._highs.push(bar.high)
        self._lows.push(bar.low)
        if not self._highs.full:
            self._value = None
            return None
        hhv, llv = max(self._highs.values), min(self._lows.values)
        rsv = 50.0 if hhv == llv else (bar.close - llv) / (hhv - llv) * 100.0
        self._k = ((self._m1 - 1) * self._k + rsv) / self._m1
        self._d = ((self._m2 - 1) * self._d + self._k) / self._m2
        self._value = KdjValue(k=self._k, d=self._d, j=3.0 * self._k - 2.0 * self._d)
        return self._value

    @property
    def value(self) -> KdjValue | None:
        return self._value


__all__ = ["ATR", "KDJ", "KdjValue", "true_range"]
