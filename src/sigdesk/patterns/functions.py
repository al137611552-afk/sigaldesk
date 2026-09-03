"""表达式函数表：A 档指标类。B 档结构原语注册在同一张表里（primitives.py）。

同一张表是 ADR-0003 的要求 —— 规则里可以自由混用
``ema(close,20) > ema(close,60) and breakout(range(20), dir='up')``。

约定：
- 函数**返回 ``Level``**（当前值 + 上一根值），而不是裸 float，
  这样 ``cross_up`` 之类的穿越原语才拿得到上一根。
- 预热期返回 ``Level(None, None)``，由三值逻辑向上传播成"不成立"。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..indicators.bars import ATR, KDJ
from ..indicators.series import BOLL, EMA, MACD, RSI, SMA, StdDev
from .context import MAX_LOOKBACK, EvalContext
from .values import Level, Quantity, Series, scalar


@dataclass(frozen=True, slots=True)
class FuncSpec:
    """一个可在表达式里调用的函数。``fn`` 的第一个参数恒为 EvalContext。"""

    name: str
    fn: Callable[..., Any]
    doc: str
    signature: inspect.Signature
    # 特殊形式：参数**不能先求值再传进来**（at 要在另一个周期的上下文里求值第二个参数）。
    # 求值器专门为它开一条路，编译期照样核对形状。
    special: bool = False

    def check_call(self, n_positional: int, keywords: Sequence[str]) -> str | None:
        """编译期核对调用形状。返回错误说明，没问题返回 None。

        为什么非做不可：`config/rules/` 是 fail-fast 加载的，坏规则本不该进得了生产。
        但参数个数写错原先能编译通过，要等**上线后第一根 bar** 才抛 TypeError ——
        那时候已经在盯盘了。实测 `ema(close, 60, '1h')` 就是这么溜过去的。
        """
        try:
            # 第一个位置塞 None 代表 ctx；只核对形状，不求值
            self.signature.bind(None, *([None] * n_positional), **dict.fromkeys(keywords))
        except TypeError as e:
            return str(e)
        return None


REGISTRY: dict[str, FuncSpec] = {}


def register(
    name: str, doc: str, *, special: bool = False
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in REGISTRY:
            raise ValueError(f"表达式函数重名: {name}")
        REGISTRY[name] = FuncSpec(
            name=name, fn=fn, doc=doc, signature=inspect.signature(fn), special=special
        )
        return fn

    return deco


def _as_series(x: object, who: str) -> Series:
    if not isinstance(x, Series):
        raise TypeError(f"{who} 的第一个参数要求是 bar 字段（如 close/volume），收到 {x!r}")
    return x


def _part_of(value: object, part: str) -> float | None:
    """从多输出指标（MacdValue/BollValue/KdjValue）里取一个分量。预热期为 None。"""
    return None if value is None else float(getattr(value, part))


def num_arg(x: object, who: str, default: float) -> float:
    """取浮点参数。**不能写成 `scalar(x) or default`** —— 那会把显式传入的 0 也吃掉。"""
    v = scalar(x)
    if v is None:
        return default
    if isinstance(v, str):
        raise ValueError(f"{who} 的数值参数不能是字符串，收到 {x!r}")
    return float(v)


def _int(x: object, who: str) -> int:
    v = scalar(x)
    if not isinstance(v, int | float) or isinstance(v, bool) or int(v) != v or v < 1:
        raise ValueError(f"{who} 的周期参数必须是 >= 1 的整数，收到 {x!r}")
    return int(v)


# ---------------------------------------------------------------- 均线与波动


@register("sma", "简单移动平均：sma(close, 20)")
def _sma(ctx: EvalContext, src: object, n: object) -> Level:
    s, w = _as_series(src, "sma"), _int(n, "sma")
    return ctx.level(("sma", s.name, w), lambda: SMA(w), s)


@register("ema", "指数移动平均（SMA 播种，ADR-0006）：ema(close, 20)")
def _ema(ctx: EvalContext, src: object, n: object) -> Level:
    s, w = _as_series(src, "ema"), _int(n, "ema")
    return ctx.level(("ema", s.name, w), lambda: EMA(w), s)


@register("std", "滚动总体标准差（除以 n）：std(close, 20)")
def _std(ctx: EvalContext, src: object, n: object) -> Level:
    s, w = _as_series(src, "std"), _int(n, "std")
    return ctx.level(("std", s.name, w), lambda: StdDev(w), s)


@register("atr", "平均真实波幅（Wilder 平滑）：atr(14)")
def _atr(ctx: EvalContext, n: object = 14) -> Level:
    w = _int(n, "atr")
    return ctx.bar_level(("atr", w), lambda: ATR(w), lambda v: v)


# ---------------------------------------------------------------- 摆动指标


@register("rsi", "相对强弱（Wilder）：rsi(14) 或 rsi(close, 14)")
def _rsi(ctx: EvalContext, a: object = 14, b: object | None = None) -> Level:
    if b is None and isinstance(a, Series):
        # rsi(close) 少写了周期。不拦的话 close 会被 _int 当成周期数，
        # 价格恰好是整数时甚至不会报错 —— 变成一条永远算不对的规则。
        raise ValueError("rsi 缺少周期参数；写成 rsi(14) 或 rsi(close, 14)")
    src, n = (a, b) if b is not None else (ctx.series("close"), a)
    s, w = _as_series(src, "rsi"), _int(n, "rsi")
    return ctx.level(("rsi", s.name, w), lambda: RSI(w), s)


def _pick_macd(ctx: EvalContext, part: str, f: int, sl: int, sg: int, src: Series) -> Level:
    state = ctx.cache.level(
        (ctx.symbol, ctx.timeframe, "macd", f, sl, sg),
        lambda: MACD(f, sl, sg),
        src.values,
        ctx.stamps(),
    )
    return Level(cur=_part_of(state.cur, part), prev=_part_of(state.prev, part))


@register("macd_dif", "MACD 快慢线之差：macd_dif()")
def _macd_dif(ctx: EvalContext, fast: object = 12, slow: object = 26, signal: object = 9) -> Level:
    return _pick_macd(ctx, "dif", _int(fast, "macd"), _int(slow, "macd"), _int(signal, "macd"),
                      ctx.series("close"))


@register("macd_dea", "MACD 信号线：macd_dea()")
def _macd_dea(ctx: EvalContext, fast: object = 12, slow: object = 26, signal: object = 9) -> Level:
    return _pick_macd(ctx, "dea", _int(fast, "macd"), _int(slow, "macd"), _int(signal, "macd"),
                      ctx.series("close"))


@register("macd_hist", "MACD 柱 = 2×(DIF−DEA)（国内口径，ADR-0006）：macd_hist()")
def _macd_hist(ctx: EvalContext, fast: object = 12, slow: object = 26, signal: object = 9) -> Level:
    return _pick_macd(ctx, "hist", _int(fast, "macd"), _int(slow, "macd"), _int(signal, "macd"),
                      ctx.series("close"))


def _pick_kdj(ctx: EvalContext, part: str, n: int, m1: int, m2: int) -> Level:
    return ctx.bar_level(
        ("kdj", n, m1, m2), lambda: KDJ(n, m1, m2),
        lambda v: _part_of(v, part),
    )


@register("kdj_k", "KDJ 的 K：kdj_k()")
def _kdj_k(ctx: EvalContext, n: object = 9, m1: object = 3, m2: object = 3) -> Level:
    return _pick_kdj(ctx, "k", _int(n, "kdj"), _int(m1, "kdj"), _int(m2, "kdj"))


@register("kdj_d", "KDJ 的 D：kdj_d()")
def _kdj_d(ctx: EvalContext, n: object = 9, m1: object = 3, m2: object = 3) -> Level:
    return _pick_kdj(ctx, "d", _int(n, "kdj"), _int(m1, "kdj"), _int(m2, "kdj"))


@register("kdj_j", "KDJ 的 J = 3K−2D：kdj_j()")
def _kdj_j(ctx: EvalContext, n: object = 9, m1: object = 3, m2: object = 3) -> Level:
    return _pick_kdj(ctx, "j", _int(n, "kdj"), _int(m1, "kdj"), _int(m2, "kdj"))


def _pick_boll(ctx: EvalContext, part: str, n: int, k: float) -> Level:
    src = ctx.series("close")
    state = ctx.cache.level(
        (ctx.symbol, ctx.timeframe, "boll", n, k), lambda: BOLL(n, k), src.values, ctx.stamps()
    )
    return Level(cur=_part_of(state.cur, part), prev=_part_of(state.prev, part))


@register("boll_mid", "布林中轨：boll_mid(20, 2)")
def _boll_mid(ctx: EvalContext, n: object = 20, k: object = 2) -> Level:
    return _pick_boll(ctx, "mid", _int(n, "boll"), num_arg(k, "boll", 2.0))


@register("boll_upper", "布林上轨：boll_upper(20, 2)")
def _boll_upper(ctx: EvalContext, n: object = 20, k: object = 2) -> Level:
    return _pick_boll(ctx, "upper", _int(n, "boll"), num_arg(k, "boll", 2.0))


@register("boll_lower", "布林下轨：boll_lower(20, 2)")
def _boll_lower(ctx: EvalContext, n: object = 20, k: object = 2) -> Level:
    return _pick_boll(ctx, "lower", _int(n, "boll"), num_arg(k, "boll", 2.0))


# ---------------------------------------------------------------- 穿越与工具


def _cross(a: object, b: object, up: bool) -> bool | None:
    """穿越判定需要两侧的**上一根**值 —— 这正是函数返回 Level 而非裸 float 的原因。"""
    if not isinstance(a, Level | Series) or not isinstance(b, Level | Series):
        raise TypeError("cross_up/cross_down 的参数必须是字段或指标，不能是常数")
    if a.cur is None or a.prev is None or b.cur is None or b.prev is None:
        return None  # 预热期：未知，不是"没穿越"
    if up:
        return a.prev <= b.prev and a.cur > b.cur
    return a.prev >= b.prev and a.cur < b.cur


@register("cross_up", "上穿：cross_up(close, ema(close,20))")
def _cross_up(ctx: EvalContext, a: object, b: object) -> bool | None:
    return _cross(a, b, up=True)


@register("cross_down", "下穿：cross_down(close, ema(close,20))")
def _cross_down(ctx: EvalContext, a: object, b: object) -> bool | None:
    return _cross(a, b, up=False)


@register(
    "at",
    "跨级别引用：at('1h', close > ema(close, 60)) —— 把整个子表达式放到另一个周期上求值",
    special=True,
)
def _at(ctx: EvalContext, timeframe: object, expr: object) -> object:
    """**特殊形式**，求值器不会先算 expr 再传进来（见 expr.py）。

    这里的签名只为编译期核对参数个数与生成文档；真正的求值在 expr.py 里，
    因为第二个参数必须在**切换后的上下文**里算 —— 先算再传就没有意义了。
    """
    raise AssertionError("at 是特殊形式，应由求值器直接处理")


@register(
    "prev",
    "回看 n 根前的值（默认 1）：prev(close)、prev(ema(close,20), 6)；"
    "对 swing_low/swing_high 则是**上一个摆动点**，双底/双顶靠它",
)
def _prev(ctx: EvalContext, x: object, n: object = 1) -> float | None:
    """指标与字段本来就带 ``prev``（cross_up 内部一直在用），只是从没暴露给表达式。

    注意语义随来源而变：``prev(close)`` 是上一根 bar 的收盘价，
    而 ``prev(swing_low(5))`` 是**上一个已确认的摆动低点**（不是上一根）。
    双底要比的正是后者。

    **``n`` 是为了判断均线朝向。** 单根差（``ema20 < prev(ema20)``）噪声极大：
    实测 fu2611 曾以 0.031 个 ATR 的降幅被判成「空头趋势」，图上是一条平线。
    ``ema(close,20) < prev(ema(close,20), 6)`` 才是「比 6 根之前低」——
    仍然是**同一条均线跟它自己比**，只是把噪声平掉了。

    两种"取不到"要分开处理，**不能都返回 None**：
      - 历史还不够长（预热期）-> None，走三值逻辑，与指标预热一致（ADR-0006）
      - n 超过历史缓冲上限     -> **抛错**。那是配置写错，静默变成"条件永不成立"
        会让规则一条都不报且毫无提示 —— 这个项目栽在这类静默失效上太多次了。
    """
    if not isinstance(x, Quantity):
        raise TypeError(
            f"prev() 只能用在带「上一根」的量上（bar 字段或指标函数的返回值），收到 {x!r}"
        )
    k = _int(n, "prev")
    if k < 1:
        raise ValueError(f"prev() 的回看根数必须 >= 1，收到 {k}")
    if k > MAX_LOOKBACK:
        raise ValueError(
            f"prev() 的回看根数 {k} 超过历史缓冲上限 {MAX_LOOKBACK}；"
            "要更远的回看请调大 patterns/context.py 的 MAX_LOOKBACK"
        )
    if k == 1:
        return x.prev
    hist = getattr(x, "values", None) or getattr(x, "history", None)
    if not hist:
        raise TypeError(f"prev(x, {k}) 需要 x 带历史值，{type(x).__name__} 没有")
    if k >= len(hist):
        return None      # 预热期：历史还不够长。与指标预热一致，走三值逻辑
    got: float | None = hist[-(k + 1)]
    return got


@register("abs", "绝对值")
def _abs(ctx: EvalContext, x: object) -> float | None:
    v = scalar(x)
    return None if v is None else abs(float(v))


@register("min", "取小")
def _min(ctx: EvalContext, *xs: object) -> float | None:
    vs = [scalar(x) for x in xs]
    return None if any(v is None for v in vs) else min(float(v) for v in vs if v is not None)


@register("max", "取大")
def _max(ctx: EvalContext, *xs: object) -> float | None:
    vs = [scalar(x) for x in xs]
    return None if any(v is None for v in vs) else max(float(v) for v in vs if v is not None)


__all__ = ["REGISTRY", "FuncSpec", "num_arg", "register"]
