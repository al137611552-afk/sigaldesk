"""白名单表达式引擎单测。

三块重点：
1. **沙箱**：规则来自 YAML 配置，等于"配置即代码"，逃逸必须在编译期被挡住。
2. **三值逻辑**：指标预热期是 None，不是 0 也不是 False（ADR-0006）。
3. **增量性**：同一个指标跨 bar 复用状态，不是每次求值全量重算。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from sigdesk.core.models import Bar, Timeframe
from sigdesk.feed.okx import normalize_candles
from sigdesk.indicators.series import EMA, SMA
from sigdesk.patterns.context import EvalContext, IndicatorCache
from sigdesk.patterns.expr import ExprError, compile_expr
from sigdesk.patterns.values import Level, Series, scalar, truthy

UID = "CRYPTO.OKX.BTCUSDT.PERP"


@pytest.fixture(scope="module")
def bars(btc_swap_okx: dict[str, Any]) -> list[Bar]:
    return normalize_candles(btc_swap_okx["1m"], symbol=UID, timeframe=Timeframe.M1)


def ctx_at(bars: list[Bar], n: int, cache: IndicatorCache | None = None) -> EvalContext:
    """取前 n 根构成的 as-of 上下文。"""
    return EvalContext(
        symbol=UID, timeframe=Timeframe.M1, bars=tuple(bars[:n]), cache=cache or IndicatorCache()
    )


def ev(source: str, ctx: EvalContext) -> bool | None:
    return compile_expr(source).evaluate(ctx)


# ---------------------------------------------------------------- 沙箱


@pytest.mark.parametrize(
    "source",
    [
        "close.__class__",                      # 属性访问：沙箱逃逸的第一步
        "close.__class__.__bases__",
        "(1).__class__.__mro__[1].__subclasses__()",
        "__import__('os')",
        "open('/etc/passwd')",
        "eval('1+1')",
        "exec('x=1')",
        "[x for x in close]",                   # 推导式
        "lambda: 1",
        "close[0]",                             # 下标
        "(y := 1)",                             # 海象
        "min(*[1,2])",                          # 星号展开
        "2 ** 999999999",                       # 幂：能把求值线程挂死
        "{'a': 1}",
        "{1, 2}",
        "[1, 2]",
    ],
)
def test_dangerous_syntax_is_rejected_at_compile_time(source: str) -> None:
    """全部必须在**编译期**被拒 —— 不能等到运行期才发现，更不能静默为假。"""
    with pytest.raises(ExprError):
        compile_expr(source)


def test_unknown_function_is_rejected_with_helpful_message() -> None:
    """规则是手写 YAML，函数名打错是常事，要在加载时就报出来。"""
    with pytest.raises(ExprError, match="未注册的函数 emaa"):
        compile_expr("emaa(close, 20) > 1")


def test_unknown_variable_is_rejected() -> None:
    with pytest.raises(ExprError, match="未知变量 price"):
        compile_expr("price > 100")


def test_syntax_error_names_the_expression() -> None:
    with pytest.raises(ExprError, match="语法错误"):
        compile_expr("close >")


def test_allowed_syntax_compiles(bars: list[Bar]) -> None:
    expr = compile_expr(
        "ema(close,20) > ema(close,60) and rsi(14) < 45 and volume > sma(volume,20)*1.5"
    )
    assert expr.functions == {"ema", "rsi", "sma"}
    assert expr.evaluate(ctx_at(bars, 200)) in (True, False)


# ---------------------------------------------------------------- 三值逻辑


def test_warmup_yields_unknown_not_false(bars: list[Bar]) -> None:
    """预热期必须是 None（未知），不是 False —— 二者对上层的意义不同：
    None 表示"还不知道"，若塌缩成 0 则 `close > ema(close,60)` 会在预热期恒真。"""
    assert ev("ema(close, 60) > 0", ctx_at(bars, 10)) is None
    assert ev("close > ema(close, 60)", ctx_at(bars, 10)) is None
    assert ev("ema(close, 60) > 0", ctx_at(bars, 100)) is True


def test_and_short_circuits_false_over_unknown(bars: list[Bar]) -> None:
    """明确为假的 and 分支直接判假，不必等未知项 —— 否则预热期什么都判不了。"""
    assert ev("close < 0 and ema(close, 60) > 0", ctx_at(bars, 10)) is False
    assert ev("close > 0 and ema(close, 60) > 0", ctx_at(bars, 10)) is None


def test_or_short_circuits_true_over_unknown(bars: list[Bar]) -> None:
    assert ev("close > 0 or ema(close, 60) > 0", ctx_at(bars, 10)) is True
    assert ev("close < 0 or ema(close, 60) > 0", ctx_at(bars, 10)) is None


def test_not_propagates_unknown(bars: list[Bar]) -> None:
    assert ev("not (ema(close, 60) > 0)", ctx_at(bars, 10)) is None
    assert ev("not (close < 0)", ctx_at(bars, 10)) is True


def test_is_satisfied_treats_unknown_as_not_satisfied(bars: list[Bar]) -> None:
    """预热期宁可不报也不能误报。"""
    expr = compile_expr("ema(close, 60) > 0")
    assert expr.evaluate(ctx_at(bars, 10)) is None
    assert expr.is_satisfied(ctx_at(bars, 10)) is False


def test_arithmetic_propagates_unknown(bars: list[Bar]) -> None:
    assert ev("ema(close, 60) + 1 > 0", ctx_at(bars, 10)) is None


def test_division_by_zero_is_unknown_not_crash(bars: list[Bar]) -> None:
    """一条规则的算术退化不该掀翻整个引擎。"""
    assert ev("close / 0 > 1", ctx_at(bars, 50)) is None
    assert ev("close / (close - close) > 1", ctx_at(bars, 50)) is None


# ---------------------------------------------------------------- 语义正确性


def test_series_resolves_to_latest_closed_bar(bars: list[Bar]) -> None:
    ctx = ctx_at(bars, 50)
    assert ev(f"close == {bars[49].close}", ctx) is True
    assert ev(f"volume == {bars[49].volume}", ctx) is True


def test_indicator_value_matches_direct_computation(bars: list[Bar]) -> None:
    """表达式里的 sma/ema 必须与直接跑指标类的结果一致。"""
    closes = [b.close for b in bars[:120]]
    sma = SMA(20)
    for c in closes:
        sma.update(c)
    ctx = ctx_at(bars, 120)
    assert ev(f"sma(close, 20) == {sma.value}", ctx) is True

    ema = EMA(20)
    for c in closes:
        ema.update(c)
    assert ev(f"ema(close, 20) == {ema.value}", ctx) is True


def test_cross_up_needs_previous_values(bars: list[Bar]) -> None:
    """穿越判定要两侧的上一根值；这正是指标函数返回 Level 而非裸 float 的原因。"""
    cache = IndicatorCache()
    fired = [
        i for i in range(30, 300)
        if compile_expr("cross_up(close, ema(close, 20))").evaluate(ctx_at(bars, i, cache)) is True
    ]
    # 用独立方式复算：close 由下方穿到上方
    ema = EMA(20)
    vals = [ema.update(b.close) for b in bars]
    expect = [
        i for i in range(30, 300)
        if vals[i - 2] is not None
        and bars[i - 2].close <= vals[i - 2]  # type: ignore[operator]
        and bars[i - 1].close > vals[i - 1]  # type: ignore[operator]
    ]
    assert fired == expect


def test_cross_rejects_constant_operand() -> None:
    """cross_up(close, 100) 没有意义 —— 常数没有"上一根"，静默为假会藏 bug。"""
    with pytest.raises(ExprError, match="不能是常数"):
        compile_expr("cross_up(close, 100)").evaluate(
            EvalContext(UID, Timeframe.M1, (), IndicatorCache())
        )


def test_chained_comparison(bars: list[Bar]) -> None:
    assert ev("0 < rsi(14) < 100", ctx_at(bars, 100)) is True
    assert ev("100 < rsi(14) < 200", ctx_at(bars, 100)) is False


def test_keyword_and_string_constants_allowed(bars: list[Bar]) -> None:
    """B 档结构原语要用 dir='up' 这类关键字参数，字符串常量必须放行。"""
    compile_expr("rsi(close, 14) < 45")  # 位置参数形式
    assert ev("rsi(close, 14) == rsi(14)", ctx_at(bars, 100)) is True


def test_empty_bars_is_unknown() -> None:
    """标的刚接入、一根 bar 都没有时，一切未知，不能崩也不能误报。"""
    ctx = EvalContext(UID, Timeframe.M1, (), IndicatorCache())
    assert ev("close > 0", ctx) is None
    assert ev("sma(close, 20) > 0", ctx) is None


def test_bad_indicator_period_is_rejected(bars: list[Bar]) -> None:
    for bad in ("sma(close, 0)", "sma(close, -3)", "sma(close, 1.5)"):
        with pytest.raises(ExprError, match="求值失败|周期参数"):
            compile_expr(bad).evaluate(ctx_at(bars, 50))


def test_first_arg_must_be_a_field(bars: list[Bar]) -> None:
    with pytest.raises(ExprError, match="bar 字段"):
        compile_expr("sma(5, 20) > 1").evaluate(ctx_at(bars, 50))


# ---------------------------------------------------------------- 增量性


def test_indicator_state_is_reused_across_bars(bars: list[Bar]) -> None:
    """跨 bar 复用同一个指标状态对象 —— 这是"禁止每根 bar 全量重算"的落地检查。"""
    cache = IndicatorCache()
    expr = compile_expr("sma(close, 20) > 0")
    for i in range(21, 60):
        expr.evaluate(ctx_at(bars, i, cache))
    assert len(cache.states) == 1, "每次求值都新建了指标状态，增量性没生效"

    (state,) = cache.states.values()
    assert state.last_ts == bars[58].close_ts


def test_repeated_evaluation_on_same_bar_does_not_double_feed(bars: list[Bar]) -> None:
    """同一根 bar 上反复求值（多条规则共用一个指标）不得重复喂样本。"""
    cache = IndicatorCache()
    expr = compile_expr("sma(close, 20) > 0")
    ctx = ctx_at(bars, 100, cache)
    first = compile_expr("sma(close, 20) == sma(close, 20)").evaluate(ctx)
    for _ in range(5):
        expr.evaluate(ctx)
    (state,) = cache.states.values()
    assert first is True
    assert state.last_ts == bars[99].close_ts

    direct = SMA(20)
    for b in bars[:100]:
        direct.update(b.close)
    assert state.cur == pytest.approx(direct.value)


def test_same_indicator_different_params_are_separate_states(bars: list[Bar]) -> None:
    cache = IndicatorCache()
    compile_expr("sma(close,20) > sma(close,60) and sma(volume,20) > 0").evaluate(
        ctx_at(bars, 100, cache)
    )
    assert len(cache.states) == 3, "参数/字段不同的指标必须各自持有状态"


def test_cache_reset_clears_state(bars: list[Bar]) -> None:
    """换月、换标的、回测重跑都要清空 —— 残留状态会污染新序列。"""
    cache = IndicatorCache()
    compile_expr("sma(close, 20) > 0").evaluate(ctx_at(bars, 100, cache))
    cache.reset()
    assert cache.states == {}


# ---------------------------------------------------------------- 值语义


def test_scalar_and_truthy() -> None:
    assert scalar(Level(cur=1.5, prev=1.0)) == 1.5
    assert scalar(Series("close", (1.0, 2.0))) == 2.0
    assert scalar(None) is None
    assert truthy(Level(None, None)) is None
    assert truthy(Level(0.0, None)) is False
    with pytest.raises(TypeError):
        scalar(object())


def test_series_prev_needs_two_points() -> None:
    assert Series("close", ()).cur is None
    assert Series("close", (1.0,)).prev is None
    assert Series("close", (1.0, 2.0)).prev == 1.0


# ---------------------------------------------------------------- 复杂度回归


class _CountingSeq(Sequence[float]):
    """记录"被逐个访问"的次数。切片不计数 —— 正确实现只做一次切片。"""

    def __init__(self, data: list[float]) -> None:
        self._data = data
        self.touched = 0

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return self._data[index]
        self.touched += 1
        return self._data[index]

    def __len__(self) -> int:
        return len(self._data)


def test_catch_up_does_not_rescan_old_samples(bars: list[Bar]) -> None:
    """每次求值只该碰"新到的那几根"，不能从头遍历窗口再逐个跳过。

    留证：原实现就是从头遍历（结果正确、update 次数也正确，所以按调用次数根本测不出来），
    但复杂度是 O(窗口长度) —— 实盘预热 2000 根跑了 5 分半，回测里会退化成平方复杂度。
    改用 bisect 定位起点后是 O(log n + 新增根数)。
    """
    closes = [b.close for b in bars]
    cache = IndicatorCache()

    warm = _CountingSeq(closes[:200])
    cache.level(("k",), lambda: SMA(20), warm, tuple(range(200)))
    assert warm.touched == 0, "首次预热应当整段切片，不是逐个取"

    step = _CountingSeq(closes[:201])
    cache.level(("k",), lambda: SMA(20), step, tuple(range(201)))
    assert step.touched == 0, "增量喂养仍在逐个访问旧样本 —— 复杂度退化了"


def test_priming_a_long_history_is_fast(bars: list[Bar]) -> None:
    """粗放的性能护栏（余量 ~100 倍）。挡的是"又写回从头扫"这类复杂度回退。"""
    import time

    long_series = bars * 10  # 3000 根
    cache = IndicatorCache()
    expr = compile_expr("ema(close,20) > ema(close,60) and rsi(14) < 70")
    t0 = time.perf_counter()
    for i in range(100, len(long_series), 10):
        expr.evaluate(
            EvalContext(UID, Timeframe.M1, tuple(long_series[:i]), cache)
        )
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"求值 {len(long_series) // 10} 次耗时 {elapsed:.1f}s，复杂度可能退化了"
