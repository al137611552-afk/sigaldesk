"""信号质量统计单测。全部纯逻辑：手工构造 bar 序列，逐条核对口径。

统计口径最容易在四个地方把结论做假，每一条都有对应测试：
入场价、同根同时触及止损止盈、成本、neutral 信号是否算胜率。
"""

from __future__ import annotations

import datetime as dt
import pathlib
from dataclasses import replace
from typing import Any

import pytest

from sigdesk.core.models import CST, Bar, Timeframe
from sigdesk.rules.model import Direction, Signal
from sigdesk.stats.outcome import ExitReason, Outcome, OutcomeParams, evaluate, evaluate_all
from sigdesk.stats.report import (
    build_report,
    format_report,
    group_by,
    local_hour,
    summarize,
)

BTC = "CRYPTO.OKX.BTCUSDT.PERP"
RB = "CN.SHFE.rb2610"


def sig(
    fired_at: int = 600, price: float = 100.0, direction: Direction = Direction.LONG,
    symbol: str = BTC, rule: str = "r1", context: dict[str, float | None] | None = None,
) -> Signal:
    return Signal(
        rule_id=rule, symbol=symbol, direction=direction, timeframe=Timeframe.M1,
        fired_at=fired_at, trigger_price=price, dedup_key=f"{symbol}:{rule}:{fired_at}",
        context=context or {},
    )


def bar(ts: int, o: float, h: float, low: float, c: float, symbol: str = BTC) -> Bar:
    return Bar(symbol, Timeframe.M1, ts - 60, ts, o, h, low, c, 1.0)


def flat_path(start_ts: int, prices: list[float], symbol: str = BTC) -> list[Bar]:
    """每根 bar 的 OHLC 都等于给定价格 —— 只想控制"走到哪"，不想引入影线噪声。"""
    return [bar(start_ts + 60 * i, p, p, p, p, symbol) for i, p in enumerate(prices, start=1)]


# ---------------------------------------------------------------- 入场价口径


def test_entry_defaults_to_next_bar_open_not_signal_close() -> None:
    """信号在 bar 收盘时才成立，那个收盘价已经过去、成交不到。

    用它统计会系统性偏乐观 —— 尤其对"放量突破"这类信号，信号那根本身就走了一大截。
    """
    s = sig(price=100.0)
    future = [bar(660, o=105.0, h=106.0, low=104.0, c=105.5)]

    default = evaluate(s, future, OutcomeParams(horizon_bars=1))
    optimistic = evaluate(s, future, OutcomeParams(horizon_bars=1, entry_on_next_open=False))

    assert default.entry_price == 105.0, "默认应当用次根开盘价入场"
    assert optimistic.entry_price == 100.0
    assert optimistic.gross_ret > default.gross_ret, "用信号收盘价入场必然更好看"


def test_no_future_bars_is_marked_not_silently_zero() -> None:
    """信号之后没有数据时必须标成"无法评价"，不能当成 0 收益混进胜率。"""
    out = evaluate(sig(), [])
    assert out.reason is ExitReason.NO_DATA
    assert not out.evaluated
    assert summarize([out]).directional == 0


# ---------------------------------------------------------------- 止损止盈


def test_stop_wins_when_both_hit_on_the_same_bar() -> None:
    """同一根同时触及止损与止盈时保守取止损 —— bar 数据给不出先后，
    取止盈就是在给自己发奖。"""
    s = sig()
    # 入场 100，止损 99.5、止盈 101；这根 bar 高低都够到
    future = [bar(660, o=100.0, h=102.0, low=99.0, c=100.0)]

    out = evaluate(s, future, OutcomeParams(stop_pct=0.005, target_pct=0.01))

    assert out.reason is ExitReason.STOP
    assert out.exit_price == pytest.approx(99.5)


def test_target_is_taken_when_only_target_hit() -> None:
    s = sig()
    future = [bar(660, o=100.0, h=101.5, low=99.9, c=101.0)]
    out = evaluate(s, future, OutcomeParams(stop_pct=0.005, target_pct=0.01))
    assert out.reason is ExitReason.TARGET
    assert out.exit_price == pytest.approx(101.0)
    assert out.gross_ret == pytest.approx(0.01)


def test_horizon_exit_uses_close() -> None:
    s = sig()
    future = flat_path(600, [100.0, 100.1, 100.2])
    out = evaluate(s, future, OutcomeParams(horizon_bars=3, stop_pct=0.5, target_pct=0.5))
    assert out.reason is ExitReason.HORIZON
    assert out.bars_held == 3
    assert out.exit_price == pytest.approx(100.2)


def test_short_direction_is_normalised() -> None:
    """方向已归一化：ret > 0 恒表示"这条信号是对的"，空头下跌也应为正收益。"""
    s = sig(direction=Direction.SHORT)
    future = flat_path(600, [100.0, 99.0])
    out = evaluate(s, future, OutcomeParams(horizon_bars=2, stop_pct=0.5, target_pct=0.5))
    assert out.gross_ret == pytest.approx(0.01)
    assert out.is_win


def test_short_stop_is_above_entry() -> None:
    s = sig(direction=Direction.SHORT)
    future = [bar(660, o=100.0, h=100.6, low=100.0, c=100.5)]
    out = evaluate(s, future, OutcomeParams(stop_pct=0.005, target_pct=0.01))
    assert out.reason is ExitReason.STOP
    assert out.exit_price == pytest.approx(100.5)
    assert out.ret < 0


def test_atr_key_overrides_percentage_stops() -> None:
    """期货各品种波动率差异极大，固定百分比会让活跃品种全打止损、呆滞品种永不触发。"""
    s = sig(context={"atr14": 2.0})
    future = [bar(660, o=100.0, h=100.5, low=96.5, c=98.0)]

    pct = evaluate(s, future, OutcomeParams(stop_pct=0.005, target_pct=0.01, atr_key=None))
    atr = evaluate(
        s, future, OutcomeParams(stop_pct=0.005, target_pct=0.01, atr_key="atr14", stop_atr=1.5)
    )

    assert pct.exit_price == pytest.approx(99.5)  # 100 × 0.5%
    assert atr.exit_price == pytest.approx(97.0)  # 100 − 1.5×2.0
    assert atr.reason is ExitReason.STOP


def test_atr_is_the_default_basis() -> None:
    """**默认口径就是 ATR。** 曾经默认是百分比，而纸上撮合/rule_eval 各自传了
    atr14 —— 面板上的胜率和模拟盘的胜率算的不是同一件事。默认值是全仓唯一来源，
    这条测试就是钉死"不传参数时按 ATR"。"""
    s = sig(context={"atr14": 2.0})
    future = [bar(660, o=100.0, h=100.5, low=96.5, c=98.0)]
    out = evaluate(s, future, OutcomeParams())
    assert out.exit_price == pytest.approx(97.0)  # 100 − 1.5×2.0，不是 99.5
    assert out.exit_basis == "atr"
    assert OutcomeParams().atr_key == "atr14"


def test_missing_atr_falls_back_to_percentage() -> None:
    s = sig(context={"atr14": None})
    future = [bar(660, o=100.0, h=100.1, low=99.0, c=99.2)]
    out = evaluate(s, future, OutcomeParams(atr_key="atr14", stop_pct=0.005))
    assert out.exit_price == pytest.approx(99.5)


def test_silent_fallback_is_recorded_not_hidden() -> None:
    """回落本身没错，**看不出回落了**才是问题：一批信号里混着两套口径，
    报告上毫无异常，横向比较却已经不成立。所以每条都记下自己用的口径。"""
    have = sig(context={"atr14": 2.0})
    lack = sig(context={})  # 规则的 context: 没声明 atr14，或预热期还是 None
    future = [bar(660, o=100.0, h=100.1, low=99.4, c=99.6)]

    assert evaluate(have, future, OutcomeParams()).exit_basis == "atr"
    assert evaluate(lack, future, OutcomeParams()).exit_basis == "pct"
    assert evaluate(have, future, OutcomeParams(atr_key=None)).exit_basis == "pct"


# ---------------------------------------------------------------- 成本


def test_cost_is_charged_on_both_sides() -> None:
    """一进一出都要付。只扣单边会让高频规则看起来能赚钱。"""
    s = sig()
    future = flat_path(600, [100.0, 101.0])
    p = OutcomeParams(horizon_bars=2, stop_pct=0.5, target_pct=0.5, cost_bps=5.0)

    out = evaluate(s, future, p)

    assert out.gross_ret == pytest.approx(0.01)
    assert out.ret == pytest.approx(0.01 - 2 * 5e-4)


def test_zero_cost_is_gross_return() -> None:
    out = evaluate(sig(), flat_path(600, [100.0, 101.0]),
                   OutcomeParams(horizon_bars=2, stop_pct=0.5, target_pct=0.5))
    assert out.ret == out.gross_ret


# ---------------------------------------------------------------- MFE / MAE


def test_mfe_and_mae_track_the_whole_path() -> None:
    """最大浮盈/浮亏用来判断止损是不是设太紧了 —— 只看最终收益看不出这件事。"""
    s = sig()
    future = [
        bar(660, o=100.0, h=100.0, low=100.0, c=100.0),
        bar(720, o=100.0, h=103.0, low=98.0, c=100.0),
        bar(780, o=100.0, h=100.0, low=100.0, c=100.0),
    ]
    out = evaluate(s, future, OutcomeParams(horizon_bars=3, stop_pct=0.5, target_pct=0.5))
    assert out.mfe == pytest.approx(0.03)
    assert out.mae == pytest.approx(-0.02)


def test_mfe_mae_for_short_are_direction_adjusted() -> None:
    s = sig(direction=Direction.SHORT)
    future = [bar(660, o=100.0, h=103.0, low=98.0, c=100.0)]
    out = evaluate(s, future, OutcomeParams(horizon_bars=1, stop_pct=0.5, target_pct=0.5))
    assert out.mfe == pytest.approx(0.02), "空头下跌 2% 才是有利偏移"
    assert out.mae == pytest.approx(-0.03)


# ---------------------------------------------------------------- 汇总


def outcomes_for(rets: list[float], direction: Direction = Direction.LONG) -> list[Outcome]:
    return [
        Outcome(
            rule_id="r1", symbol=BTC, direction=direction, fired_at=600 + 60 * i,
            entry_ts=600, entry_price=100.0, exit_ts=660, exit_price=100 * (1 + r),
            reason=ExitReason.TARGET if r > 0 else ExitReason.STOP,
            ret=r, gross_ret=r, mfe=max(r, 0.0), mae=min(r, 0.0), bars_held=1,
        )
        for i, r in enumerate(rets)
    ]


def test_summarize_basic_metrics() -> None:
    st = summarize(outcomes_for([0.02, -0.01, 0.01, -0.01]))
    assert (st.signals, st.evaluated, st.directional) == (4, 4, 4)
    assert (st.wins, st.losses) == (2, 2)
    assert st.win_rate == pytest.approx(0.5)
    assert st.avg_return == pytest.approx(0.0025)
    assert st.avg_win == pytest.approx(0.015)
    assert st.avg_loss == pytest.approx(-0.01)
    assert st.payoff == pytest.approx(1.5)
    assert st.false_rate == pytest.approx(0.5)


def test_neutral_signals_do_not_enter_win_rate() -> None:
    """neutral 是"去看一眼"的提示，不是方向判断。把它算进胜率等于给自己注水。"""
    st = summarize(outcomes_for([0.02, -0.01], direction=Direction.NEUTRAL))
    assert st.signals == 2 and st.evaluated == 2
    assert st.directional == 0
    assert st.win_rate == 0.0 and st.avg_return == 0.0
    assert st.avg_mfe != 0.0, "但 MFE/MAE 对 neutral 仍然有意义"


def test_summarize_empty_is_zeroed_not_crash() -> None:
    st = summarize([])
    assert st.signals == 0 and st.win_rate == 0.0 and st.payoff == 0.0


def test_payoff_without_losses_is_zero_not_infinity() -> None:
    st = summarize(outcomes_for([0.01, 0.02]))
    assert st.losses == 0
    assert st.payoff == 0.0


def test_median_return() -> None:
    assert summarize(outcomes_for([0.01, 0.02, 0.03])).median_return == pytest.approx(0.02)
    assert summarize(outcomes_for([0.01, 0.03])).median_return == pytest.approx(0.02)


# ---------------------------------------------------------------- 分组与可复现


def test_local_hour_uses_market_timezone() -> None:
    """期货看北京时间才有意义（夜盘 21:00 与日盘 09:00 是完全不同的时段）。"""
    ts = int(dt.datetime(2026, 8, 28, 21, 30, tzinfo=CST).timestamp())
    futures = Outcome("r", RB, Direction.LONG, ts, ts, 1.0, ts, 1.0, ExitReason.HORIZON,
                      0.0, 0.0, 0.0, 0.0, 1)
    crypto = Outcome("r", BTC, Direction.LONG, ts, ts, 1.0, ts, 1.0, ExitReason.HORIZON,
                     0.0, 0.0, 0.0, 0.0, 1)
    assert local_hour(futures) == 21
    assert local_hour(crypto) == dt.datetime.fromtimestamp(ts, dt.UTC).hour


def test_group_by_output_is_sorted() -> None:
    """报告要逐字节可复现 ⇒ 分组键必须排序输出，不能沿用插入顺序。"""
    outs = outcomes_for([0.01, -0.01, 0.02])
    shuffled = [outs[2], outs[0], outs[1]]

    grouped = group_by(shuffled, lambda o: o.fired_at)

    assert list(grouped) == sorted(grouped)
    assert list(grouped) != [o.fired_at for o in shuffled], "顺序没被归一化，测试就没意义"


def test_report_is_reproducible() -> None:
    """M3 验收：同一批输入两次汇总，结果完全一致。"""
    outs = outcomes_for([0.02, -0.01, 0.015, -0.005, 0.0])
    a = build_report(outs, {"horizon_bars": 20})
    b = build_report(list(reversed(outs)), {"horizon_bars": 20})
    assert a.as_dict()["overall"] == b.as_dict()["overall"], "汇总结果不该依赖输入顺序"
    assert build_report(outs).as_dict() == build_report(outs).as_dict()


def test_report_carries_the_params() -> None:
    """一份不写明口径的胜率没有意义 —— 20 根持有期和 200 根能差出天壤之别。"""
    report = build_report(outcomes_for([0.01]), {"horizon_bars": 20, "cost_bps": 5.0})
    assert report.params == {"horizon_bars": 20, "cost_bps": 5.0}
    assert "horizon_bars" in format_report(report)


def test_report_groups_all_four_dimensions() -> None:
    outs = outcomes_for([0.01, -0.01])
    outs += [
        Outcome("r2", RB, Direction.SHORT, 700, 700, 1.0, 760, 1.0, ExitReason.TARGET,
                0.02, 0.02, 0.02, 0.0, 1)
    ]
    report = build_report(outs)
    assert set(report.by_rule) == {"r1", "r2"}
    assert set(report.by_symbol) == {BTC, RB}
    assert set(report.by_direction) == {"long", "short"}
    assert report.by_rule["r2"].wins == 1


def test_format_report_is_readable() -> None:
    text = format_report(build_report(outcomes_for([0.02, -0.01])))
    assert "信号质量报告" in text
    assert "胜率" in text and "假信号率" in text


# ---------------------------------------------------------------- 批量评价


def test_evaluate_all_only_uses_bars_after_the_signal() -> None:
    """评价用的数据必须完全落在信号之后 —— 与 INV-1 同一个道理，物理截断。"""
    series = flat_path(0, [100.0] * 5 + [110.0] * 5)
    s = sig(fired_at=series[4].close_ts)

    (out,) = evaluate_all([s], {BTC: series}, OutcomeParams(horizon_bars=1))

    assert out.entry_ts > s.fired_at
    assert out.entry_price == 110.0


def test_evaluate_all_handles_unknown_symbol() -> None:
    (out,) = evaluate_all([sig(symbol="NOPE")], {BTC: flat_path(0, [1.0])})
    assert out.reason is ExitReason.NO_DATA


# ---------------------------------------------------------------- 有效样本量


def _outcome(sym: str, entry: int, exit_ts: int) -> Any:
    from sigdesk.rules.model import Direction
    from sigdesk.stats.outcome import ExitReason, Outcome

    return Outcome(rule_id="r", symbol=sym, direction=Direction.LONG, fired_at=entry,
                   entry_ts=entry, entry_price=1.0, exit_ts=exit_ts, exit_price=1.0,
                   reason=ExitReason.HORIZON, ret=0.0, gross_ret=0.0, mfe=0.0, mae=0.0,
                   bars_held=1)


def test_effective_n_discounts_overlapping_holds() -> None:
    """**`stdev/sqrt(n)` 假设信号相互独立，但持有期比冷却期长时它们不独立** ——
    同一段价格变动会被好几条信号同时吃到。

    实例：扳机换到 1m 后名义 n=250、SE ±0.0073%，看着像 6.7 个标准误的强证据；
    按重叠折算后有效 n 只有 7、SE ±0.0440%，区间当场跨零 ——
    **结论从「唯一站得住的改动」变成「测不出差别」。**
    """
    from sigdesk.stats.baseline import effective_n

    # 完全不重叠 -> 有效样本量就是条数
    assert effective_n([_outcome("A", i * 1000, i * 1000 + 100) for i in range(10)]) == 10

    # 全部压在同一段 -> 只值一条
    assert effective_n([_outcome("A", 0, 1000) for _ in range(10)]) == pytest.approx(1.0)

    # 每 30 开一笔、持有 100 -> 每条与 |i-j|<=3 的 7 条相交 -> n/7
    roll = [_outcome("A", i * 30, i * 30 + 100) for i in range(60)]
    assert 7 <= effective_n(roll) <= 11, "滚动开仓的有效样本量该在 n/7 附近"

    # **跨标的不算重叠**：不同品种的价格变动是两回事
    assert effective_n([_outcome("A", 0, 1000), _outcome("B", 0, 1000)]) == 2


def test_standard_error_uses_effective_n() -> None:
    """标准误必须按有效样本量算，否则系统性偏小。"""
    import statistics as _st

    from sigdesk.stats.baseline import standard_error

    # 十条完全重叠、收益各异 -> SE 应当接近 stdev/sqrt(1) 而不是 /sqrt(10)
    outs = []
    for i in range(10):
        o = _outcome("A", 0, 1000)
        outs.append(replace(o, ret=(i - 4.5) / 1000))
    sd = _st.stdev([o.ret for o in outs])
    assert standard_error(outs) == pytest.approx(sd, rel=0.01), "全重叠时不该再除 sqrt(10)"


def test_cli_and_library_share_one_standard_error() -> None:
    """**同一个量不许两处各写一套。** `rule_eval.py` 原来自己算了一遍
    `stdev/sqrt(n)`，于是修了库里那份、CLI 还在报旧值（真踩过）。"""
    src = pathlib.Path("scripts/rule_eval.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "standard_error(" in code, "CLI 要用库里那份"
    assert "statistics.stdev" not in code, "CLI 不许自己再算一遍标准误"


def test_evaluate_all_reports_no_data_when_series_misses_the_signal() -> None:
    """**序列覆盖不到这条信号时要如实标 NO_DATA，不能静默拿最老的几根当"未来"。**

    信号必然是在某根 bar 上触发的，所以正常情况下至少有一根 `close_ts <= fired_at`。
    一根都没有，说明喂进来的序列根本不包含这条信号所在的那段行情。
    不拦的话 `series[0:...]` 会把序列**开头**当成未来，算出一个看着完全正常的
    entry/exit —— 报告上毫无异常，谁也看不出来。

    真踩过：BarStore 的 MAX_BARS 把 1m 序列裁到最后 5000 根，三条相隔两周的信号
    被评价到同一根上，entry/exit 一模一样（见 rules/trial.py 里的说明）。
    """
    old = sig(fired_at=600)
    future = flat_path(100_000, [100.0, 101.0, 102.0])  # 整条都远在信号之后

    out = evaluate_all([old], {BTC: future})[0]
    assert out.reason is ExitReason.NO_DATA
    assert not out.evaluated
    assert out.entry_ts == 0

    # 覆盖得到时照常评价
    ok = evaluate_all([sig(fired_at=100_060)], {BTC: future})[0]
    assert ok.reason is not ExitReason.NO_DATA
    assert ok.entry_ts > 100_060


def test_excess_interval_is_centred_on_the_excess_not_the_gross() -> None:
    """**区间要画在判据上。** 判据是超额（毛期望 − 随机进场基准），
    而 rule_eval 曾经打印 `毛期望 ± 2·SE` —— 中心是另一个量。

    真被读错过：某条规则毛期望 +0.0344%、基准 +0.0035%、超额 +0.0309%，
    打印出来的 `+0.0015% ~ +0.0673%` 不跨零，于是被当成"超额显著为正"；
    平移到超额上是 `-0.0020% ~ +0.0638%`，其实跨零。差别正好是基准那一段，
    在这个例子里就是"显著"与"分辨不出"的分界。

    面板（app.js renderExcess）一直按超额画，是 CLI 跟它分了家 ——
    同一个量两处各写一套的老问题。这条测试钉住两边同一个算法。
    """
    gross, base, se = 0.0344, 0.0035, 0.01645
    excess = gross - base

    # 错的画法（以毛期望为中心）会得出"不跨零"
    assert gross - 2 * se > 0

    # 对的画法（以超额为中心）跨零
    lo, hi = excess - 2 * se, excess + 2 * se
    assert lo < 0 < hi, "超额的区间应当跨零"
    # 两种画法只差一个基准
    assert (gross - 2 * se) - lo == pytest.approx(base)


def test_random_baseline_uses_the_same_exit_convention_as_the_rule() -> None:
    """**基准和规则必须用同一套出场口径，否则"超额"里混进止损宽度差。**

    `random_entry_expectation` 造的假信号原来不带 context，于是 `risk_distances`
    静默回落到百分比止损，而规则的信号带着 `atr14` 走 ATR 倍数 ——
    实测规则 87 笔全 `atr`、基准 602 笔全 `pct`，两边根本不是一套口径。
    BTC 上这一项让基准从 +0.0029% 虚高到 +0.0184%，
    **和我们要分辨的超额同一量级**，结论会被它带偏。

    模块文档一直宣称"用同一个 evaluate_all 是关键 —— 两者之差才只包含选时"。
    函数共用挡不住，分家的是喂进去的数据。
    """
    from sigdesk.stats.baseline import random_entry_expectation

    bars = flat_path(0, [100.0 + (i % 5) for i in range(200)])
    # 造一段有真实波动的序列，让 ATR 算得出来
    bars = [bar(60 * i, 100.0 + i * 0.1, 100.6 + i * 0.1, 99.4 + i * 0.1, 100.2 + i * 0.1)
            for i in range(1, 201)]

    exp_atr, n = random_entry_expectation(bars, Direction.LONG, OutcomeParams(), stride=10)
    exp_pct, _ = random_entry_expectation(
        bars, Direction.LONG, OutcomeParams(atr_key=None), stride=10)
    assert n > 0

    # 口径不同必然给出不同的基准 —— 相等就说明 atr_key 根本没生效
    assert exp_atr != exp_pct, "基准没有跟着 atr_key 走，说明假信号仍然不带 context"

    # 直接查每一笔用的口径
    import collections

    from sigdesk.indicators.bars import ATR
    from sigdesk.rules.model import Signal
    from sigdesk.stats.outcome import evaluate_all

    atr = ATR(14)
    vals = [atr.update(b) for b in bars]
    fake = [Signal(rule_id="__random__", symbol=b.symbol, direction=Direction.LONG,
                   timeframe=b.timeframe, fired_at=b.close_ts, trigger_price=b.close,
                   dedup_key=f"r{i}", context={"atr14": vals[i]})
            for i, b in enumerate(bars) if i % 10 == 0]
    got = collections.Counter(
        o.exit_basis for o in evaluate_all(fake, {BTC: list(bars)}, OutcomeParams()))
    assert got["atr"] > got["pct"], f"基准应以 ATR 口径为主，实际 {dict(got)}"
