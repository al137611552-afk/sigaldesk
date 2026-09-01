"""作用在单条数值序列上的指标（收盘价、成交量皆可）。

口径见 ADR-0006 —— 概括：EMA 用 SMA 播种；RSI/ATR 用 Wilder 平滑；
MACD 柱取 **2×(DIF−DEA)**（国内看盘软件口径）；BOLL 标准差**除以 n**（总体标准差）。
这些约定在国内外软件之间确有分歧，写死在这里并配单测，免得日后"和看盘软件对不上"来回改。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .base import Rolling


class SMA:
    """简单移动平均。窗口未满返回 None。"""

    __slots__ = ("_roll", "_value")

    def __init__(self, window: int) -> None:
        self._roll = Rolling(window)
        self._value: float | None = None

    def update(self, x: float) -> float | None:
        self._roll.push(x)
        self._value = self._roll.mean if self._roll.full else None
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


class EMA:
    """指数移动平均，``alpha = 2/(n+1)``。

    **播种方式是有分歧的口径**：本实现用前 n 个样本的 SMA 作种子（TA-Lib 口径），
    因此第 n 个样本才出第一个值。部分国内软件用首个样本直接播种，会与此在前若干根不同，
    但差异按 (1-alpha)^k 指数衰减，很快收敛。见 ADR-0006。
    """

    __slots__ = ("_alpha", "_seed", "_value", "window")

    def __init__(self, window: int) -> None:
        if window < 1:
            raise ValueError(f"窗口必须 >= 1，收到 {window}")
        self.window = window
        self._alpha = 2.0 / (window + 1)
        self._seed = Rolling(window)
        self._value: float | None = None

    def update(self, x: float) -> float | None:
        if self._value is None:
            self._seed.push(x)
            if self._seed.full:
                self._value = self._seed.mean
            return self._value
        self._value += self._alpha * (x - self._value)
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


class Wilder:
    """Wilder 平滑（RMA）：``alpha = 1/n``，用前 n 个样本的均值播种。

    RSI 与 ATR 都建立在它之上 —— Wilder 原著用的就是这个，与 EMA(n) **不是**一回事
    （EMA 的 alpha 是 2/(n+1)），混用会让 RSI 明显偏离所有主流软件。
    """

    __slots__ = ("_alpha", "_seed", "_value", "window")

    def __init__(self, window: int) -> None:
        if window < 1:
            raise ValueError(f"窗口必须 >= 1，收到 {window}")
        self.window = window
        self._alpha = 1.0 / window
        self._seed = Rolling(window)
        self._value: float | None = None

    def update(self, x: float) -> float | None:
        if self._value is None:
            self._seed.push(x)
            if self._seed.full:
                self._value = self._seed.mean
            return self._value
        self._value += self._alpha * (x - self._value)
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


class StdDev:
    """滚动**总体**标准差（除以 n，不是 n-1）。BOLL 用它。

    除数取 n 还是 n-1 是另一个分歧点：TA-Lib 与国内主流软件取 n，本项目跟随。
    """

    __slots__ = ("_roll", "_value")

    def __init__(self, window: int) -> None:
        self._roll = Rolling(window)
        self._value: float | None = None

    def update(self, x: float) -> float | None:
        self._roll.push(x)
        if not self._roll.full:
            self._value = None
            return None
        mean = self._roll.mean
        var = math.fsum((v - mean) ** 2 for v in self._roll.values) / self._roll.window
        self._value = math.sqrt(var)
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


class RSI:
    """相对强弱。Wilder 平滑的涨跌幅之比。

    首个样本只用于取差分，因此第 n+1 个样本才出第一个值。
    全为涨（无跌幅）时定义为 100 —— 不做除零保护会直接崩在单边行情上。
    """

    __slots__ = ("_down", "_prev", "_up", "_value")

    def __init__(self, window: int = 14) -> None:
        self._up = Wilder(window)
        self._down = Wilder(window)
        self._prev: float | None = None
        self._value: float | None = None

    def update(self, x: float) -> float | None:
        if self._prev is None:
            self._prev = x
            return None
        change = x - self._prev
        self._prev = x
        up = self._up.update(max(change, 0.0))
        down = self._down.update(max(-change, 0.0))
        if up is None or down is None:
            self._value = None
        elif down == 0.0:
            self._value = 100.0 if up > 0.0 else 50.0  # 无跌幅：全涨=100，完全走平=50
        else:
            self._value = 100.0 - 100.0 / (1.0 + up / down)
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


@dataclass(frozen=True, slots=True)
class MacdValue:
    dif: float
    dea: float
    hist: float


class MACD:
    """MACD。``dif = ema(fast) - ema(slow)``，``dea = ema(dif, signal)``。

    **``hist = 2 × (dif - dea)``**，即国内看盘软件（通达信/文华）的口径；
    西方软件多用 `dif - dea`（差一个 2 倍）。取国内口径是因为本项目主战场是国内期货，
    规则阈值要能照着屏幕上的数字写。见 ADR-0006。
    """

    __slots__ = ("_dea", "_fast", "_slow", "_value")

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if fast >= slow:
            raise ValueError(f"fast({fast}) 必须小于 slow({slow})")
        self._fast = EMA(fast)
        self._slow = EMA(slow)
        self._dea = EMA(signal)
        self._value: MacdValue | None = None

    def update(self, x: float) -> MacdValue | None:
        fast = self._fast.update(x)
        slow = self._slow.update(x)
        if fast is None or slow is None:
            self._value = None
            return None
        dif = fast - slow
        dea = self._dea.update(dif)
        if dea is None:
            self._value = None
            return None
        self._value = MacdValue(dif=dif, dea=dea, hist=2.0 * (dif - dea))
        return self._value

    @property
    def value(self) -> MacdValue | None:
        return self._value


@dataclass(frozen=True, slots=True)
class BollValue:
    mid: float
    upper: float
    lower: float

    @property
    def width(self) -> float:
        """带宽（相对中轨），常用于判断挤压/扩张。中轨为 0 时返回 0。"""
        return 0.0 if self.mid == 0 else (self.upper - self.lower) / self.mid


class BOLL:
    """布林带。中轨 = SMA(n)，上下轨 = 中轨 ± k × 总体标准差。"""

    __slots__ = ("_k", "_sma", "_std", "_value")

    def __init__(self, window: int = 20, k: float = 2.0) -> None:
        self._sma = SMA(window)
        self._std = StdDev(window)
        self._k = k
        self._value: BollValue | None = None

    def update(self, x: float) -> BollValue | None:
        mid = self._sma.update(x)
        std = self._std.update(x)
        if mid is None or std is None:
            self._value = None
            return None
        self._value = BollValue(mid=mid, upper=mid + self._k * std, lower=mid - self._k * std)
        return self._value

    @property
    def value(self) -> BollValue | None:
        return self._value


__all__ = ["BOLL", "EMA", "MACD", "RSI", "SMA", "BollValue", "MacdValue", "StdDev", "Wilder"]
