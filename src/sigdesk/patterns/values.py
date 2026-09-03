"""表达式里的值语义。纯逻辑。

两件事决定了这一层的形状：

1. **预热期是 None，不是 0**（ADR-0006）。表达式必须做**三值逻辑**：
   任一操作数为 None ⇒ 结果 None ⇒ 规则判定为"不成立"。
   若用 0 顶替，`close > ema(close,60)` 在预热期恒真，数据刚接上的头 60 根会疯狂误报。

2. **穿越类原语需要"上一根的值"**。若指标函数只返回一个 float，
   `cross_up(close, ema(close,20))` 拿到的就只是当前值，上一根的信息已经丢了。
   所以指标函数返回 ``Level(cur, prev)``，在算术/比较语境下再降解为 ``cur``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Quantity(Protocol):
    """带"上一根"的量。指标值与字段序列都满足它。"""

    @property
    def cur(self) -> float | None: ...

    @property
    def prev(self) -> float | None: ...


@dataclass(frozen=True, slots=True)
class Level:
    """指标在当前 bar 与上一根 bar 上的值。预热期为 None。

    ``history`` 是有界的近期取值（升序，末位 == ``cur``），供 ``prev(x, n)`` 回看多根。
    默认空元组：不是所有 Quantity 都有历史（比如手工构造的），回看不到就报错不静默。
    """

    cur: float | None
    prev: float | None = None
    history: tuple[float | None, ...] = ()


@dataclass(frozen=True, slots=True)
class Series:
    """对某个 bar 字段的引用（close/volume/...），同时携带该字段的历史值。

    历史值供指标首次构造时预热回放；``cur``/``prev`` 供直接参与比较。
    """

    name: str
    values: tuple[float, ...]

    @property
    def cur(self) -> float | None:
        return self.values[-1] if self.values else None

    @property
    def prev(self) -> float | None:
        return self.values[-2] if len(self.values) >= 2 else None


@dataclass(frozen=True, slots=True)
class PriceRange:
    """一段价格区间（B 档结构原语的中间值）。不可直接参与比较，只能喂给 breakout 之类。"""

    high: float
    low: float

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0


def scalar(x: object) -> float | bool | str | None:
    """把值降解为参与算术/比较的标量。``Quantity`` 取当前值。

    字符串原样返回：B 档原语要用 ``dir='up'`` 这类关键字参数，
    它们参与的是等值比较与分支选择，不是算术。
    """
    if isinstance(x, Level | Series):
        return x.cur
    if isinstance(x, PriceRange):
        raise TypeError("价格区间不能直接参与比较；请用 breakout(range(20), dir='up') 这类原语")
    if isinstance(x, bool | float | int | str):
        return x
    if x is None:
        return None
    raise TypeError(f"表达式里出现了不支持的值类型: {type(x).__name__}")


def truthy(x: object) -> bool | None:
    """三值逻辑的真值判定：None 保持 None（"未知"），不塌缩成 False。"""
    s = scalar(x)
    return None if s is None else bool(s)


__all__ = ["Level", "PriceRange", "Quantity", "Series", "scalar", "truthy"]
