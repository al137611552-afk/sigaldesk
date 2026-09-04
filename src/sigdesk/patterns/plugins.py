"""C 档：Python 插件（ADR-0003 的逃生舱）。

A/B 两档能表达的形态都能被未来的可视化编辑器结构化编辑；表达不了的用这一档，
代价是不可视化。插件用 ``@pattern`` 注册进**同一张函数表**，
因此规则里 ``my_setup()`` 和 ``ema(close,20)`` 写法完全一样。

安全边界与 A/B 档不同，必须说清楚：
- A/B 档是**数据**（YAML 里的表达式字符串），经白名单 AST 求值，不能执行任意代码。
- C 档是**代码**（用户自己写的 Python 函数），能做的事和进程里任何代码一样多。
  它不是沙箱，也不打算是 —— 谁能往 `plugins/` 放文件，谁本来就能改这个进程。
  真正被保证的是**数据边界**：``PatternCtx`` 只给到 as-of 截断后的只读序列，
  所以插件即使写错，最坏是形态判断错，**不会造成未来函数、不会污染回测有效性**。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.models import Bar, Timeframe
from ..indicators.bars import ATR
from ..indicators.series import EMA, RSI, SMA
from .context import EvalContext
from .functions import register
from .values import Level


class PatternIndicators:
    """插件里的指标入口。走与表达式**同一份**增量缓存，所以插件不会退化成全量重算。"""

    __slots__ = ("_ctx",)

    def __init__(self, ctx: EvalContext) -> None:
        self._ctx = ctx

    def sma(self, field: str = "close", n: int = 20) -> float | None:
        return self._ctx.level(("sma", field, n), lambda: SMA(n), self._ctx.series(field)).cur

    def ema(self, field: str = "close", n: int = 20) -> float | None:
        return self._ctx.level(("ema", field, n), lambda: EMA(n), self._ctx.series(field)).cur

    def rsi(self, field: str = "close", n: int = 14) -> float | None:
        return self._ctx.level(("rsi", field, n), lambda: RSI(n), self._ctx.series(field)).cur

    def atr(self, n: int = 14) -> float | None:
        return self._ctx.bar_level(("atr", n), lambda: ATR(n), lambda v: v).cur


@dataclass(frozen=True, slots=True)
class PatternCtx:
    """插件能看到的**全部**东西：已按 as-of 截断的只读 bar 序列 + 指标 + 参数。

    没有网络、没有磁盘、没有当前时间、没有未截断的历史 —— 未来函数在结构上不可能发生。
    """

    symbol: str
    timeframe: Timeframe
    bars: Sequence[Bar]
    ind: PatternIndicators
    params: Mapping[str, Any]

    @property
    def bar(self) -> Bar | None:
        return self.bars[-1] if self.bars else None

    def closes(self) -> tuple[float, ...]:
        return tuple(b.close for b in self.bars)

    def level(self, value: float | None, prev: float | None = None) -> Level:
        """把插件算出的数值包成 Level，好让 cross_up 之类能用上它。"""
        return Level(cur=value, prev=prev)


PLUGINS: dict[str, dict[str, Any]] = {}


def pattern(
    name: str, params: Mapping[str, Any] | None = None, doc: str = ""
) -> Callable[[Callable[[PatternCtx], Any]], Callable[[PatternCtx], Any]]:
    """把一个函数注册成可在规则表达式里调用的形态。

        @pattern("inside_bar_breakout", params={"lookback": 20})
        def inside_bar_breakout(ctx: PatternCtx) -> bool:
            return ctx.bars[-1].high > ctx.bars[-2].high

    调用点可以覆盖默认参数：``inside_bar_breakout(lookback=30)``。
    """
    defaults = dict(params or {})

    def deco(fn: Callable[[PatternCtx], Any]) -> Callable[[PatternCtx], Any]:
        def adapter(ctx: EvalContext, **overrides: Any) -> Any:
            unknown = set(overrides) - set(defaults)
            if unknown:
                raise ValueError(
                    f"形态 {name} 不认识参数 {sorted(unknown)}；已声明: {sorted(defaults)}"
                )
            pctx = PatternCtx(
                symbol=ctx.symbol,
                timeframe=ctx.timeframe,
                bars=ctx.bars,
                ind=PatternIndicators(ctx),
                params={**defaults, **overrides},
            )
            return fn(pctx)

        register(name, doc or f"Python 插件形态 {name}")(adapter)
        PLUGINS[name] = defaults
        return fn

    return deco


__all__ = ["PLUGINS", "PatternCtx", "PatternIndicators", "pattern"]
