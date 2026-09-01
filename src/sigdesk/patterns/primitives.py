"""B 档结构原语：K 线与价格结构（ADR-0003）。

与 A 档指标注册在**同一张函数表**里，因此规则可以自由混用：
``ema(close,20) > ema(close,60) and breakout(range(20), dir='up')``。

**没有未来函数**：所有原语只读 ``ctx.bars``，而它已由 BarView 按 as-of 物理截断（INV-1）。
其中 ``swing_high/swing_low`` 有天然的**确认滞后** —— 一个摆动高点要等右侧 n 根走完才成立，
所以它返回的永远是"已确认"的那个，绝不是正在形成的那个。这是正确行为，不是缺陷。
"""

from __future__ import annotations

from ..core.models import Bar
from .context import EvalContext
from .functions import num_arg, register
from .values import Level, PriceRange, Series, scalar


def _n(x: object, who: str) -> int:
    v = scalar(x)
    if not isinstance(v, int | float) or isinstance(v, bool) or int(v) != v or v < 1:
        raise ValueError(f"{who} 的根数参数必须是 >= 1 的整数，收到 {x!r}")
    return int(v)


def _dir(x: object, who: str) -> str:
    v = scalar(x)
    if v not in ("up", "down"):
        raise ValueError(f"{who} 的 dir 必须是 'up' 或 'down'，收到 {x!r}")
    return str(v)


def _body(bar: Bar) -> float:
    return abs(bar.close - bar.open)


# ---------------------------------------------------------------- 区间与突破


@register("range", "最近 n 根（**不含当前根**）的高低区间：range(20)")
def _range(ctx: EvalContext, n: object) -> PriceRange | None:
    """不含当前根 —— 含了的话当前根永远落在区间内，breakout 恒为假。"""
    count = _n(n, "range")
    window = ctx.bars[-(count + 1) : -1]
    if len(window) < count:
        return None
    return PriceRange(high=max(b.high for b in window), low=min(b.low for b in window))


@register("breakout", "突破区间：breakout(range(20), dir='up')")
def _breakout(ctx: EvalContext, rng: object, dir: str | object = "up") -> bool | None:  # noqa: A002
    if rng is None or ctx.bar is None:
        return None
    if not isinstance(rng, PriceRange):
        raise TypeError("breakout 的第一个参数必须是价格区间，如 range(20)")
    d = _dir(dir, "breakout")
    return ctx.bar.close > rng.high if d == "up" else ctx.bar.close < rng.low


@register("highest", "最近 n 根（含当前根）的最大值：highest(high, 20)")
def _highest(ctx: EvalContext, src: object, n: object) -> Level:
    return _extreme(src, _n(n, "highest"), high=True)


@register("lowest", "最近 n 根（含当前根）的最小值：lowest(low, 20)")
def _lowest(ctx: EvalContext, src: object, n: object) -> Level:
    return _extreme(src, _n(n, "lowest"), high=False)


def _extreme(src: object, count: int, *, high: bool) -> Level:
    if not isinstance(src, Series):
        raise TypeError("highest/lowest 的第一个参数要求是 bar 字段，如 high/low/close")
    pick = max if high else min
    cur = pick(src.values[-count:]) if len(src.values) >= count else None
    prev = pick(src.values[-count - 1 : -1]) if len(src.values) >= count + 1 else None
    return Level(cur=cur, prev=prev)


# ---------------------------------------------------------------- 摆动点


def _swing_points(
    bars: tuple[Bar, ...], n: int, *, high: bool, want: int = 2
) -> list[tuple[int, float]]:
    """已确认的摆动点 ``(下标, 价格)``，由新到旧。

    确认条件：某根两侧各 n 根都不比它更极端。右侧那 n 根必须**已经走完**，
    所以最新可确认的位置是 ``len - 1 - n`` —— 这个滞后是摆动点定义自带的，
    想"实时"拿到只能偷看未来。

    返回下标（而不是只返回价格）是为了双底/双顶：要检查两个低点**之间**
    有没有一个像样的反弹高点，没有颈线的两个相近低点不叫双底，叫横盘。
    """
    out: list[tuple[int, float]] = []
    for i in range(len(bars) - 1 - n, n - 1, -1):
        window = bars[i - n : i + n + 1]
        pivot = bars[i]
        if high and pivot.high == max(b.high for b in window):
            out.append((i, pivot.high))
        elif not high and pivot.low == min(b.low for b in window):
            out.append((i, pivot.low))
        if len(out) == want:
            break
    return out


def _swings(bars: tuple[Bar, ...], n: int, *, high: bool) -> list[float]:
    return [price for _, price in _swing_points(bars, n, high=high)]


@register("swing_high", "最近**已确认**的摆动高点价格：swing_high(5)")
def _swing_high(ctx: EvalContext, n: object = 5) -> Level:
    found = _swings(ctx.bars, _n(n, "swing_high"), high=True)
    return Level(cur=found[0] if found else None, prev=found[1] if len(found) > 1 else None)


@register("swing_low", "最近**已确认**的摆动低点价格：swing_low(5)")
def _swing_low(ctx: EvalContext, n: object = 5) -> Level:
    found = _swings(ctx.bars, _n(n, "swing_low"), high=False)
    return Level(cur=found[0] if found else None, prev=found[1] if len(found) > 1 else None)


# ---------------------------------------------------------------- 单根/两根形态


@register("gap", "跳空：gap(dir='up')")
def _gap(ctx: EvalContext, dir: str | object = "up") -> bool | None:  # noqa: A002
    if len(ctx.bars) < 2:
        return None
    prev, cur = ctx.bars[-2], ctx.bars[-1]
    return cur.low > prev.high if _dir(dir, "gap") == "up" else cur.high < prev.low


@register("engulfing", "吞没：engulfing(dir='up')")
def _engulfing(ctx: EvalContext, dir: str | object = "up") -> bool | None:  # noqa: A002
    if len(ctx.bars) < 2:
        return None
    prev, cur = ctx.bars[-2], ctx.bars[-1]
    if _dir(dir, "engulfing") == "up":
        return (
            prev.close < prev.open
            and cur.close > cur.open
            and cur.close >= prev.open
            and cur.open <= prev.close
        )
    return (
        prev.close > prev.open
        and cur.close < cur.open
        and cur.close <= prev.open
        and cur.open >= prev.close
    )


@register("pin_bar", "长影线：pin_bar(dir='up', ratio=2)")
def _pin_bar(
    ctx: EvalContext, dir: str | object = "up", ratio: object = 2.0  # noqa: A002
) -> bool | None:
    """dir='up' 指**下**影线长（看涨锤子线），因为长下影意味着下方买盘承接。

    判据是两条：长的那侧影线 ≥ ratio×实体，且**另一侧影线 ≤ 长影线/ratio**。

    第二条不能写成"另一侧影线 ≤ 实体"—— 实体趋近 0（十字锤）时那等于要求另一侧影线必须为 0，
    会把最标准的锤子线判掉。用影线之间的相对比较才对小实体稳健。
    """
    if ctx.bar is None:
        return None
    bar = ctx.bar
    if bar.high == bar.low:
        return False  # 一字线（涨跌停封死）没有影线可言
    body = _body(bar)
    upper = bar.high - max(bar.open, bar.close)
    lower = min(bar.open, bar.close) - bar.low
    k = num_arg(ratio, "pin_bar", 2.0)
    if k <= 0:
        raise ValueError(f"pin_bar 的 ratio 必须为正，收到 {ratio!r}")
    if _dir(dir, "pin_bar") == "up":
        return lower >= k * body and upper <= lower / k
    return upper >= k * body and lower <= upper / k


@register("consolidation", "窄幅盘整：consolidation(20, 0.02) —— n 根的振幅占比低于阈值")
def _consolidation(ctx: EvalContext, n: object, max_width: object = 0.02) -> bool | None:
    count = _n(n, "consolidation")
    if len(ctx.bars) < count:
        return None
    window = ctx.bars[-count:]
    hi, lo = max(b.high for b in window), min(b.low for b in window)
    mid = (hi + lo) / 2.0
    if mid == 0:
        return None
    return (hi - lo) / mid <= num_arg(max_width, "consolidation", 0.02)


@register("inside_bar", "内包线：本根高低完全落在上一根之内")
def _inside_bar(ctx: EvalContext) -> bool | None:
    if len(ctx.bars) < 2:
        return None
    prev, cur = ctx.bars[-2], ctx.bars[-1]
    return cur.high <= prev.high and cur.low >= prev.low


__all__ = ["PriceRange"]


def _double(ctx: EvalContext, n: int, tol: float, *, high: bool) -> bool | None:
    """双底/双顶。三个条件缺一不可：

    1. 最近两个**已确认**的摆动点价格接近（相对差 <= tol）；
    2. 两点之间存在一个反向的极值（颈线），且与两点的距离 >= tol ——
       **没有颈线的两个相近低点不是双底，是横盘**；
    3. 数据不够判断时返回 None 而不是 False（ADR-0006：未知不等于不成立）。
    """
    pts = _swing_points(ctx.bars, n, high=high)
    if len(pts) < 2:
        return None
    (i_new, p_new), (i_old, p_old) = pts[0], pts[1]
    base = min(abs(p_new), abs(p_old))
    if base <= 0:
        return None
    if abs(p_new - p_old) / base > tol:
        return False
    between = ctx.bars[i_old + 1 : i_new]
    if not between:
        return False
    if high:
        neck = min(b.low for b in between)
        return (min(p_new, p_old) - neck) / base >= tol
    neck = max(b.high for b in between)
    return (neck - max(p_new, p_old)) / base >= tol


@register(
    "double_bottom",
    "双底：double_bottom(5, 0.003) —— 最近两个摆动低点相差不超过 tol，且中间有像样的反弹",
)
def _double_bottom(ctx: EvalContext, n: object = 5, tol: object = 0.003) -> bool | None:
    return _double(ctx, _n(n, "double_bottom"), num_arg(tol, "double_bottom", 0.003), high=False)


@register(
    "double_top",
    "双顶：double_top(5, 0.003) —— 最近两个摆动高点相差不超过 tol，且中间有像样的回落",
)
def _double_top(ctx: EvalContext, n: object = 5, tol: object = 0.003) -> bool | None:
    return _double(ctx, _n(n, "double_top"), num_arg(tol, "double_top", 0.003), high=True)
