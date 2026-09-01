"""B 档结构原语 + C 档插件单测。

结构原语用**手工构造的、形状明确的** bar 序列验证，不用真实行情 ——
真实行情里"这里到底算不算吞没"没有独立答案，测了等于把实现抄一遍。
无未来函数这条则用真实行情跑：任何原语在 as-of 视图上的结果，
不能因为后面又来了新 bar 而改变。
"""

from __future__ import annotations

from typing import Any

import pytest

from sigdesk.core.models import Bar, Timeframe
from sigdesk.feed.okx import normalize_candles
from sigdesk.patterns.context import EvalContext, IndicatorCache
from sigdesk.patterns.expr import ExprError, compile_expr
from sigdesk.patterns.plugins import PLUGINS, PatternCtx, pattern
from sigdesk.patterns.values import PriceRange, scalar

UID = "TEST.SYMBOL"


def mk(*ohlc: tuple[float, float, float, float]) -> tuple[Bar, ...]:
    """按 (open, high, low, close) 造一串 1m bar。"""
    return tuple(
        Bar(UID, Timeframe.M1, i * 60, (i + 1) * 60, o, h, low, c, 100.0)
        for i, (o, h, low, c) in enumerate(ohlc)
    )


def ctx(bars: tuple[Bar, ...], cache: IndicatorCache | None = None) -> EvalContext:
    return EvalContext(UID, Timeframe.M1, bars, cache or IndicatorCache())


def ev(source: str, bars: tuple[Bar, ...]) -> bool | None:
    return compile_expr(source).evaluate(ctx(bars))


def flat(n: int, price: float = 100.0) -> tuple[Bar, ...]:
    return mk(*[(price, price + 1, price - 1, price)] * n)


# ---------------------------------------------------------------- range / breakout


def test_range_excludes_current_bar() -> None:
    """含当前根的话，当前根永远落在区间内，breakout 恒为假 —— 这是最容易写错的一处。"""
    bars = mk((100, 105, 95, 100), (100, 106, 94, 100), (100, 200, 90, 150))
    rng = compile_expr("range(2)").registry["range"].fn(ctx(bars), 2)
    assert rng == PriceRange(high=106.0, low=94.0)  # 不含最后那根的 200/90


def test_range_needs_enough_history() -> None:
    assert ev("breakout(range(20), dir='up')", flat(5)) is None


def test_breakout_up_and_down() -> None:
    base = [(100.0, 105.0, 95.0, 100.0)] * 3
    assert ev("breakout(range(3), dir='up')", mk(*base, (100, 110, 99, 106))) is True
    assert ev("breakout(range(3), dir='up')", mk(*base, (100, 110, 99, 104))) is False
    assert ev("breakout(range(3), dir='down')", mk(*base, (100, 101, 90, 94))) is True
    assert ev("breakout(range(3), dir='down')", mk(*base, (100, 101, 90, 96))) is False


def test_breakout_rejects_bad_direction() -> None:
    with pytest.raises(ExprError, match="dir"):
        compile_expr("breakout(range(3), dir='sideways')").evaluate(ctx(flat(10)))


def test_price_range_cannot_be_compared_directly() -> None:
    with pytest.raises(ExprError, match="价格区间"):
        compile_expr("range(3) > 100").evaluate(ctx(flat(10)))


def test_highest_lowest() -> None:
    bars = mk((1, 10, 1, 5), (1, 20, 2, 6), (1, 15, 0, 7))
    assert ev("highest(high, 3) == 20", bars) is True
    assert ev("lowest(low, 3) == 0", bars) is True
    assert ev("highest(high, 10) > 0", bars) is None  # 历史不足


# ---------------------------------------------------------------- 摆动点


def test_swing_high_is_confirmed_not_forming() -> None:
    """摆动高点要等右侧 n 根走完才算数。峰在 index 2，n=2 ⇒ 需要 index 4 存在。"""
    rising_peak = mk(
        (1, 10, 1, 5), (1, 12, 1, 5), (1, 20, 1, 5), (1, 13, 1, 5), (1, 11, 1, 5)
    )
    assert ev("swing_high(2) == 20", rising_peak) is True
    # 少了右侧最后一根 ⇒ 尚未确认
    assert ev("swing_high(2) > 0", rising_peak[:4]) is None


def test_swing_low_confirmed() -> None:
    dip = mk((1, 10, 9, 5), (1, 10, 7, 5), (1, 10, 2, 5), (1, 10, 6, 5), (1, 10, 8, 5))
    assert ev("swing_low(2) == 2", dip) is True


def test_swing_high_never_uses_future_bars(btc_swap_okx: dict[str, Any]) -> None:
    """无未来函数的实质检查：同一时点的判定，不能因为后面又来了 bar 而改变。"""
    real = tuple(normalize_candles(btc_swap_okx["1m"], symbol=UID, timeframe=Timeframe.M1))
    expr = compile_expr("swing_high(3) > 0")
    at_100 = compile_expr("swing_high(3)").registry["swing_high"].fn(ctx(real[:100]), 3)
    at_300 = compile_expr("swing_high(3)").registry["swing_high"].fn(ctx(real[:100]), 3)
    assert at_100 == at_300
    assert expr.evaluate(ctx(real[:100])) is True
    # 用更长的历史重算截至第 100 根的视图，结果必须一致
    assert compile_expr("swing_high(3)").registry["swing_high"].fn(ctx(real[:100]), 3) == at_100


# ---------------------------------------------------------------- 单根/两根形态


def test_gap() -> None:
    assert ev("gap(dir='up')", mk((1, 10, 5, 8), (12, 15, 11, 14))) is True
    assert ev("gap(dir='up')", mk((1, 10, 5, 8), (9, 15, 9, 14))) is False
    assert ev("gap(dir='down')", mk((1, 10, 5, 8), (4, 4.5, 2, 3))) is True
    assert ev("gap(dir='up')", mk((1, 10, 5, 8))) is None  # 只有一根


def test_engulfing() -> None:
    # 前阴(10->8)后阳(7->11)，实体完全覆盖
    assert ev("engulfing(dir='up')", mk((10, 11, 7, 8), (7, 12, 6, 11))) is True
    # 后阳但没盖住
    assert ev("engulfing(dir='up')", mk((10, 11, 7, 8), (9, 12, 8, 9.5))) is False
    # 前阳后阴
    assert ev("engulfing(dir='down')", mk((8, 11, 7, 10), (11, 12, 7, 7.5))) is True


def test_pin_bar_up_means_long_lower_shadow() -> None:
    """dir='up' 指**下**影线长（看涨锤子）：长下影意味着下方买盘承接。"""
    hammer = mk((10, 10.5, 5, 10.2))  # 实体 0.2，下影 5，上影 0.3
    assert ev("pin_bar(dir='up')", hammer) is True
    assert ev("pin_bar(dir='down')", hammer) is False
    shooting = mk((10, 15, 9.8, 10.2))  # 上影长
    assert ev("pin_bar(dir='down')", shooting) is True


def test_pin_bar_flat_bar_is_false_not_crash() -> None:
    """一字线（涨跌停封死）高低相等，除法会炸 —— 必须直接判假。"""
    assert ev("pin_bar(dir='up')", mk((5, 5, 5, 5))) is False


def test_pin_bar_ratio_is_tunable() -> None:
    bar = mk((10, 10.5, 8, 10.2))  # 实体 0.2，下影 2，上影 0.3
    assert ev("pin_bar(dir='up', ratio=5)", bar) is True
    assert ev("pin_bar(dir='up', ratio=20)", bar) is False  # 下影 2 < 20×0.2


def test_pin_bar_rejects_two_sided_shadows() -> None:
    """上下影线都长（十字星）不是 pin bar —— 它表达的是犹豫，不是单侧承接。"""
    assert ev("pin_bar(dir='up')", mk((10, 12, 8, 10.1))) is False
    assert ev("pin_bar(dir='down')", mk((10, 12, 8, 10.1))) is False


def test_pin_bar_tolerates_tiny_body() -> None:
    """十字锤：实体几乎为 0、下影很长、上影很短 —— 这是最标准的锤子线，必须判真。"""
    assert ev("pin_bar(dir='up')", mk((10.0, 10.05, 5.0, 10.0))) is True


def test_inside_bar() -> None:
    assert ev("inside_bar()", mk((1, 20, 1, 10), (1, 15, 5, 10))) is True
    assert ev("inside_bar()", mk((1, 20, 1, 10), (1, 25, 5, 10))) is False


def test_consolidation() -> None:
    assert ev("consolidation(5, 0.05)", flat(5, 100.0)) is True  # 振幅 2/100
    wide = mk(*[(100, 130, 70, 100)] * 5)
    assert ev("consolidation(5, 0.05)", wide) is False
    assert ev("consolidation(50, 0.05)", flat(5)) is None  # 历史不足


# ---------------------------------------------------------------- 混用 A/B 档


def test_primitives_mix_with_indicators_in_one_expression() -> None:
    """ADR-0003 的要求：A 档与 B 档在同一张函数表里，可自由混用。"""
    bars = flat(60) + mk((100, 130, 99, 128))
    expr = compile_expr("breakout(range(20), dir='up') and close > sma(close, 20)")
    assert expr.functions == {"breakout", "range", "sma"}
    assert expr.evaluate(ctx(bars)) is True


# ---------------------------------------------------------------- C 档插件


def test_plugin_registers_and_is_callable_like_builtin() -> None:
    @pattern("test_higher_high", params={"lookback": 2}, doc="仅测试用")
    def _higher_high(pctx: PatternCtx) -> bool:
        n = int(pctx.params["lookback"])
        if len(pctx.bars) <= n:
            return False
        return pctx.bars[-1].high > max(b.high for b in pctx.bars[-n - 1 : -1])

    assert PLUGINS["test_higher_high"] == {"lookback": 2}
    rising = mk((1, 10, 1, 5), (1, 11, 1, 5), (1, 20, 1, 5))
    assert ev("test_higher_high()", rising) is True
    assert ev("test_higher_high()", mk((1, 30, 1, 5), (1, 11, 1, 5), (1, 20, 1, 5))) is False


def test_plugin_params_can_be_overridden_at_call_site() -> None:
    @pattern("test_echo_param", params={"k": 1})
    def _echo(pctx: PatternCtx) -> bool:
        return bool(pctx.params["k"] > 5)

    assert ev("test_echo_param()", flat(3)) is False
    assert ev("test_echo_param(k=9)", flat(3)) is True


def test_plugin_rejects_undeclared_param() -> None:
    """参数名打错要立刻报出来，不能悄悄用默认值跑。"""
    @pattern("test_strict_param", params={"k": 1})
    def _strict(pctx: PatternCtx) -> bool:
        return True

    with pytest.raises(ExprError, match="不认识参数"):
        compile_expr("test_strict_param(kk=2)").evaluate(ctx(flat(3)))


def test_plugin_shares_the_incremental_indicator_cache() -> None:
    """插件里的指标走同一份缓存，不能退化成每次全量重算。"""
    @pattern("test_uses_ema")
    def _uses_ema(pctx: PatternCtx) -> bool:
        value = pctx.ind.ema("close", 20)
        return value is not None and pctx.bars[-1].close > value

    cache = IndicatorCache()
    bars = flat(60)
    expr = compile_expr("test_uses_ema()")
    for i in range(25, 60):
        expr.evaluate(EvalContext(UID, Timeframe.M1, bars[:i], cache))
    assert len(cache.states) == 1


def test_plugin_only_sees_truncated_bars() -> None:
    """PatternCtx 给到的就是 as-of 截断后的序列 —— 插件写错最多是形态判断错，
    不可能拿到未来数据。"""
    seen: list[int] = []

    @pattern("test_records_length")
    def _records(pctx: PatternCtx) -> bool:
        seen.append(len(pctx.bars))
        return True

    bars = flat(50)
    compile_expr("test_records_length()").evaluate(EvalContext(UID, Timeframe.M1, bars[:10],
                                                               IndicatorCache()))
    assert seen == [10]


def test_duplicate_registration_is_rejected() -> None:
    @pattern("test_dup_name")
    def _first(pctx: PatternCtx) -> bool:
        return True

    with pytest.raises(ValueError, match="重名"):
        @pattern("test_dup_name")
        def _second(pctx: PatternCtx) -> bool:
            return True


def test_scalar_rejects_price_range() -> None:
    with pytest.raises(TypeError, match="价格区间"):
        scalar(PriceRange(high=1.0, low=0.0))


# ---------------------------------------------------------------- 参数不得被静默吃掉


def test_rsi_without_period_is_rejected() -> None:
    """rsi(close) 少写周期时，close 会被当成周期数；价格恰好是整数时连报错都不会有，
    变成一条永远算不对的规则。必须直接拒绝。"""
    with pytest.raises(ExprError, match="缺少周期参数"):
        compile_expr("rsi(close) < 45").evaluate(ctx(flat(30)))


@pytest.mark.parametrize(
    "source",
    ["pin_bar(dir='up', ratio=0)", "consolidation(3, 0)", "boll_upper(20, 0)"],
)
def test_explicit_zero_is_not_swallowed_by_defaults(source: str) -> None:
    """显式传 0 必须当 0 用。写成 `scalar(x) or 默认值` 会把 0 悄悄换成默认值 ——
    规则看着生效，实际跑的是另一套参数。"""
    bars = flat(30)
    if "pin_bar" in source:
        with pytest.raises(ExprError, match="必须为正"):
            compile_expr(source).evaluate(ctx(bars))
        return
    # 阈值 0 的窄幅盘整：只有完全无波动才成立 —— 若 0 被换成默认值就会判真
    assert compile_expr("consolidation(3, 0)").evaluate(ctx(bars)) is False
    # k=0 的布林上轨应当等于中轨
    upper = compile_expr("boll_upper(20, 0) == boll_mid(20, 0)").evaluate(ctx(flat(40)))
    assert upper is True



# ---- prev()：把 Level/Series 已有的"上一根"暴露出来 -----------------------


def path(*closes: float, wick: float = 0.2) -> tuple[Bar, ...]:
    """按收盘价序列造 bar，高低各留一点影线。"""
    return mk(*[(c, c + wick, c - wick, c) for c in closes])


def val(source: str, bars: tuple[Bar, ...]) -> object:
    return compile_expr(source).value(ctx(bars))


def test_prev_of_a_field_is_the_previous_bar() -> None:
    bars = mk((10, 12, 9, 11), (11, 14, 10, 13))
    assert val("prev(close)", bars) == 11.0


def test_prev_of_an_indicator_is_its_previous_value() -> None:
    bars = path(*[100.0 + i for i in range(6)])
    cur = val("sma(close, 3)", bars)
    assert val("prev(sma(close, 3))", bars) == cur.prev  # type: ignore[union-attr]


def test_prev_of_a_bare_number_is_refused() -> None:
    """常数没有「上一根」。静默返回 None 会让写错的人以为只是预热期。"""
    with pytest.raises(ExprError, match="prev"):
        ev("prev(3) > 1", flat(3))


def test_prev_unlocks_double_bottom_by_hand() -> None:
    """用户要的双底，最朴素的写法就是比较最近两个摆动低点。"""
    bars = path(5, 4, 3, 2, 1, 2, 3, 4, 5, 4, 3, 2, 1.002, 2, 3, 4, 5)
    assert ev("abs(swing_low(3) - prev(swing_low(3))) / close < 0.01", bars) is True


# ---- 双底 / 双顶 ---------------------------------------------------------


def w_shape(second_low: float) -> tuple[Bar, ...]:
    """W 形：低 -> 反弹 -> 再低 -> 反弹。"""
    return path(5, 4, 3, 2, 1, 2, 3, 4, 5, 4, 3, 2, second_low, 2, 3, 4, 5)


def test_double_bottom_accepts_two_similar_lows_with_a_neckline() -> None:
    assert val("double_bottom(3, 0.05)", w_shape(1.02)) is True


def test_double_bottom_rejects_lows_that_are_too_far_apart() -> None:
    # 第二个低点明显更高（3 对 1）：这是上升趋势中的回踩，不是双底
    bars = path(5, 4, 3, 2, 1, 2, 3, 4, 5, 4, 3.5, 3, 3.5, 4, 5)
    assert val("double_bottom(3, 0.05)", bars) is False


def test_double_bottom_needs_a_real_bounce_between_the_lows() -> None:
    """**没有颈线的两个相近低点不是双底，是横盘** —— 这是最容易漏的一条。"""
    bars = path(3, 2, 1, 1.01, 1.0, 1.01, 1.0, 1.01, 1.0, 1.01, 2, 3, wick=0.005)
    assert val("double_bottom(2, 0.05)", bars) is not True


def test_double_bottom_is_none_before_two_swings_are_confirmed() -> None:
    """数据不够是「未知」不是「不成立」（ADR-0006）。"""
    assert val("double_bottom(3, 0.05)", path(5, 4, 3, 2, 1)) is None


def test_double_top_mirrors_double_bottom() -> None:
    bars = path(1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3, 4, 4.98, 4, 3, 2, 1)
    assert val("double_top(3, 0.05)", bars) is True


# ---- 编译期核对调用形状 ---------------------------------------------------


@pytest.mark.parametrize(
    "source",
    ["ema(close, 60, '1h')", "ema(close)", "rsi(14, 2, 3)", "abs(close, 1)",
     "sma(close, 20, 30)", "double_bottom(3, 0.01, 5)"],
)
def test_wrong_arity_is_rejected_at_compile_time(source: str) -> None:
    """**上线后第一根 bar 才炸**是不可接受的：config/rules 是 fail-fast 加载的，
    坏规则本不该进得了生产。实测 `ema(close, 60, '1h')` 原先就是这么溜过去的。"""
    with pytest.raises(ExprError, match="参数不对"):
        compile_expr(source)


@pytest.mark.parametrize(
    "source", ["sma(close, 20)", "atr()", "rsi(14)", "min(close, open, high)",
               "consolidation(20, 0.02)", "pin_bar(dir='down', ratio=3)"],
)
def test_valid_calls_still_compile(source: str) -> None:
    assert compile_expr(source).source == source


def test_double_star_unpacking_is_refused() -> None:
    """`f(**d)` 的 keyword.arg 是 None，求值器会把它静默丢掉 —— 参数凭空消失。"""
    with pytest.raises(ExprError, match="展开传参"):
        compile_expr("sma(**close)")
