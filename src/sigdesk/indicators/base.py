"""指标层的公共骨架。纯逻辑，无 IO、无当前时间依赖。

设计要点（ARCHITECTURE §4.1）：
- **增量更新**：每根 bar 收盘调一次 ``update``，回测与实盘走同一条路径（ADR-0001）。
  禁止每根 bar 对全历史重算。
- **预热期返回 None**：窗口未满时没有值。调用方必须处理 None，不得拿 0 当值用 ——
  用 0 会让"均线还没形成"看起来像"价格跌到 0"，是最典型的假信号来源。
- 口径（EMA 播种、MACD 是否 ×2、BOLL 标准差除数等）见 ADR-0006，实现与文档必须一致。
"""

from __future__ import annotations

import math
from collections import deque
from typing import Protocol


class Indicator[T](Protocol):
    """增量指标的统一面。``update`` 喂一个新样本，返回当前值（预热期为 None）。"""

    def update(self, x: float) -> T | None: ...

    @property
    def value(self) -> T | None: ...


class Rolling:
    """定长滚动窗口，维护精确的窗口和。

    朴素的"加新值减旧值"是 O(1)，但浮点误差会随更新次数单调累积 ——
    跑几十万根 bar 后与全量重算就对不上了。这里每满一个窗口用 ``math.fsum`` 重算一次，
    摊还仍是 O(1)，误差被钉在一个窗口之内。
    """

    __slots__ = ("_since_resync", "_sum", "values", "window")

    def __init__(self, window: int) -> None:
        if window < 1:
            raise ValueError(f"窗口必须 >= 1，收到 {window}")
        self.window = window
        self.values: deque[float] = deque(maxlen=window)
        self._sum = 0.0
        self._since_resync = 0

    def push(self, x: float) -> None:
        dropped = self.values[0] if len(self.values) == self.window else None
        self.values.append(x)
        self._sum += x
        if dropped is not None:
            self._sum -= dropped
        self._since_resync += 1
        if self._since_resync >= self.window:
            self._sum = math.fsum(self.values)  # 定期重算，掐断误差累积
            self._since_resync = 0

    @property
    def full(self) -> bool:
        return len(self.values) == self.window

    @property
    def sum(self) -> float:
        return self._sum

    @property
    def mean(self) -> float:
        return self._sum / len(self.values)

    def __len__(self) -> int:
        return len(self.values)


def run[T](indicator: Indicator[T], xs: list[float]) -> list[T | None]:
    """把一整段样本喂进指标，返回逐点结果（含预热期的 None）。

    回测与对拍用；实盘是逐根 ``update``，两条路径共用同一个状态对象实现。
    """
    return [indicator.update(x) for x in xs]


__all__ = ["Indicator", "Rolling", "run"]
