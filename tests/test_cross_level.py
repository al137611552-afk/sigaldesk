"""跨级别引用 at('1h', ...)。

两条最要命的性质：
1. **as-of 对齐** —— 5m 那根收在 10:05 时，1h 侧只能看到 10:00 收盘那根，
   绝不能偷看正在走的 1h（INV-1/INV-2）。
2. **漏派生不能静默** —— 少派生一个周期的后果是空序列 -> 指标恒 None ->
   条件恒"不成立"，一条信号不报且毫无提示。必须当场报错。
"""

from __future__ import annotations

import pytest

from sigdesk.core.models import Bar, Timeframe
from sigdesk.patterns.expr import ExprError, compile_expr
from sigdesk.rules.engine import RuleEngine
from sigdesk.rules.loader import load_rule
from sigdesk.rules.model import store_timeframes
from sigdesk.store.bar_store import BarStore

UID = "X"


def m1(i: int, close: float) -> Bar:
    return Bar(UID, Timeframe.M1, i * 60, (i + 1) * 60, close, close + 1, close - 1, close, 10.0)


# ---- 编译期 -------------------------------------------------------------


def test_timeframe_must_be_a_literal() -> None:
    """周期要在编译期就能确定，否则调用方无从知道还需要派生哪些周期。"""
    with pytest.raises(ExprError, match="字面量周期"):
        compile_expr("at(close, close > 0)")


def test_unknown_timeframe_is_rejected() -> None:
    with pytest.raises(ExprError, match="无效"):
        compile_expr("at('7m', close > 0)")


def test_daily_is_referenceable() -> None:
    """`at('1d', ...)` 是"贴近日线均线"那类条件的写法。"""
    assert compile_expr("at('1d', close > ema(close, 20))").timeframes == frozenset(
        {Timeframe.D1}
    )


def test_compiled_expr_reports_the_timeframes_it_references() -> None:
    """这是"要派生哪些周期"的唯一事实源。"""
    e = compile_expr("at('1h', close > ema(close, 60)) and at('15m', rsi(14) > 50)")
    assert e.timeframes == frozenset({Timeframe.H1, Timeframe.M15})
    assert compile_expr("close > 0").timeframes == frozenset()


def test_rule_required_timeframes_includes_referenced_ones() -> None:
    rule = load_rule({
        "id": "r", "universe": [UID],
        "conditions": [{"on": "5m", "mode": "state",
                        "when": "close > 0 and at('1h', close > ema(close, 20))"}],
        "emit": {"direction": "long"},
    })
    assert rule.required_timeframes == {Timeframe.M5, Timeframe.H1}
    assert store_timeframes([rule]) == [Timeframe.M5, Timeframe.H1]


# ---- as-of 对齐 ---------------------------------------------------------


def _run(when: str, closes: list[float]) -> list[bool | None]:
    """逐根求值，返回每根 5m 收盘时该表达式的真值。"""
    rule = load_rule({
        "id": "r", "universe": [UID],
        "conditions": [{"on": "5m", "mode": "state", "when": when}],
        "emit": {"direction": "long"},
    })
    store = BarStore(timeframes=store_timeframes([rule]))
    engine = RuleEngine([rule], store)
    out: list[bool | None] = []
    for i, c in enumerate(closes):
        for bar in store.push(m1(i, c)):
            if bar.timeframe is Timeframe.M5:
                ctx = engine._context(UID, Timeframe.M5, bar.close_ts)  # noqa: SLF001
                out.append(rule.conditions[0].when.evaluate(ctx))
    return out


def test_cross_level_sees_only_closed_higher_bars() -> None:
    """1h 侧只有走满 60 根 1m 才会有第一根收盘 bar。在那之前 at() 必须是"未知"，
    **不能**拿正在走的那根凑数。"""
    closes = [100.0 + i for i in range(180)]
    got = _run("at('1h', close > 0)", closes)
    # 前 12 根 5m（= 前 60 分钟）落在第一根 1h 收盘之前
    assert got[:11] == [None] * 11
    assert got[12] is True


def test_cross_level_value_equals_the_last_closed_hour() -> None:
    """5m 侧读到的必须恰好是最近一根**已收盘** 1h 的收盘价。"""
    closes = [100.0 + i for i in range(180)]
    store = BarStore(timeframes=[Timeframe.M5, Timeframe.H1])
    rule = load_rule({
        "id": "r", "universe": [UID],
        "conditions": [{"on": "5m", "mode": "state", "when": "at('1h', close) > 0"}],
        "emit": {"direction": "long"},
    })
    engine = RuleEngine([rule], store)
    for i, c in enumerate(closes):
        store.push(m1(i, c))
    as_of = 150 * 60  # 落在第二根 1h（120 分钟）之后、第三根之前
    ctx = engine._context(UID, Timeframe.M5, as_of)  # noqa: SLF001
    expr = compile_expr("at('1h', close)")
    hour_bars = store.view(UID, as_of).bars(Timeframe.H1)
    assert expr.value(ctx).cur == hour_bars[-1].close
    assert hour_bars[-1].close_ts <= as_of, "偷看了没收盘的 1h"


def test_bare_field_outside_at_still_uses_own_timeframe() -> None:
    """at() 之外的 close 仍是本级别的 —— 作用域不能泄漏。"""
    closes = [100.0 + i for i in range(180)]
    store = BarStore(timeframes=[Timeframe.M5, Timeframe.H1])
    rule = load_rule({
        "id": "r", "universe": [UID],
        "conditions": [{"on": "5m", "mode": "state", "when": "close > 0"}],
        "emit": {"direction": "long"},
    })
    engine = RuleEngine([rule], store)
    for i, c in enumerate(closes):
        store.push(m1(i, c))
    as_of = 150 * 60
    ctx = engine._context(UID, Timeframe.M5, as_of)  # noqa: SLF001
    five = store.view(UID, as_of).bars(Timeframe.M5)
    assert compile_expr("close").value(ctx).cur == five[-1].close
    assert compile_expr("at('1h', close)").value(ctx).cur != five[-1].close


# ---- 漏派生必须报错，不能静默 --------------------------------------------


def test_missing_derived_timeframe_raises_instead_of_never_firing() -> None:
    """手写 BarStore 周期表而漏掉 at() 引用的那个 —— 曾经会变成
    "永远不报警且毫无提示"，这是最难查的失败。现在当场炸。"""
    rule = load_rule({
        "id": "r", "universe": [UID],
        "conditions": [{"on": "5m", "mode": "state", "when": "at('1h', close > 0)"}],
        "emit": {"direction": "long"},
    })
    store = BarStore(timeframes=[Timeframe.M5])  # 故意漏掉 1h
    engine = RuleEngine([rule], store)
    with pytest.raises(ExprError, match="没有派生"):
        for i in range(180):
            engine.on_bars(store.push(m1(i, 100.0 + i)))


def test_indicator_cache_is_shared_per_timeframe() -> None:
    """1h 的 EMA 只该算一份，不管是被 1h 条件用还是被 5m 通过 at() 用。"""
    rule = load_rule({
        "id": "r", "universe": [UID],
        "timeframes": {"trend": "1h", "trigger": "5m"},
        "conditions": [
            {"on": "trend", "mode": "state", "when": "close > ema(close, 20)"},
            {"on": "trigger", "mode": "state", "when": "at('1h', close > ema(close, 20))"},
        ],
        "emit": {"direction": "long"},
    })
    store = BarStore(timeframes=store_timeframes([rule]))
    engine = RuleEngine([rule], store)
    for i in range(600):
        engine.on_bars(store.push(m1(i, 100.0 + (i % 37))))
    cache = engine.cache_for(UID, Timeframe.H1)
    ema_keys = [k for k in cache.states if "ema" in k]
    assert len(ema_keys) == 1, f"1h 的 ema 被算了 {len(ema_keys)} 份: {ema_keys}"
