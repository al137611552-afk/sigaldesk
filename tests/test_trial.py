"""规则历史试算 + 条件成立区间。

两条核心断言：
1. 试算与实盘/回放**逐条一致**（同一个引擎，ADR-0001）；
2. 条件区间与真正驱动链路的判定**同源** —— 画的和引擎看到的必须是一回事。
"""

from __future__ import annotations

from typing import Any

import pytest

from sigdesk.core.models import Bar, Timeframe
from sigdesk.rules.engine import RuleEngine
from sigdesk.rules.loader import load_rule
from sigdesk.rules.trial import ConditionRecorder, run_trial
from sigdesk.stats.outcome import ExitReason
from sigdesk.store.bar_store import MAX_BARS, BarStore

UID = "X"


def bars(closes: list[float], step: int = 60) -> list[Bar]:
    return [
        Bar(symbol=UID, timeframe=Timeframe.M1, open_ts=(i + 1) * step - step,
            close_ts=(i + 1) * step, open=c, high=c + 1, low=c - 1, close=c, volume=1.0)
        for i, c in enumerate(closes)
    ]


SINGLE: dict[str, Any] = {
    "id": "t", "universe": [UID],
    "conditions": [{"on": "1m", "mode": "state", "when": "close > 100"}],
    "emit": {"direction": "long", "dedup_key": "{symbol}:{rule}:{bar_close_ts}"},
}


def test_trial_matches_a_plain_engine_run() -> None:
    """试算就是 replay 模式。分家了就会出现"试算说会触发、实盘不触发"。"""
    rule = load_rule(SINGLE)
    data = bars([99, 101, 102, 98, 105])
    store = BarStore(timeframes=[])
    expected = [s for b in data for s in RuleEngine([rule], store).on_bars(store.push(b))]
    got = run_trial(rule, {UID: data}).signals
    assert [s.as_dict() for s in got] == [s.as_dict() for s in expected]


def test_trial_reports_what_it_scanned() -> None:
    res = run_trial(load_rule(SINGLE), {UID: bars([99, 101])})
    assert res.bars_scanned == 2 and res.symbols_scanned == [UID]


def test_symbols_without_data_are_named() -> None:
    """"0 条信号"到底是规则太严还是数据没回补，是两个完全不同的结论。"""
    raw = {**SINGLE, "universe": [UID, "MISSING"]}
    res = run_trial(load_rule(raw), {UID: bars([99])})
    assert res.symbols_without_data == ["MISSING"]


def test_trial_is_deterministic() -> None:
    rule, data = load_rule(SINGLE), bars([99, 101, 102, 98, 105])
    a, b = run_trial(rule, {UID: data}), run_trial(rule, {UID: data})
    assert [s.as_dict() for s in a.signals] == [s.as_dict() for s in b.signals]
    assert a.condition_bands == b.condition_bands


def test_outcomes_line_up_with_signals() -> None:
    res = run_trial(load_rule(SINGLE), {UID: bars([99, 101, 102, 103, 104, 105])})
    assert len(res.outcomes) == len(res.signals)
    assert res.report.overall.signals == len(res.outcomes)


# ---- 条件成立区间 ---------------------------------------------------------


def test_bands_break_where_the_condition_fails() -> None:
    """中间隔了不成立就必须断开，否则图上会画出一条"从来没断过"的假带子。"""
    res = run_trial(load_rule(SINGLE), {UID: bars([101, 102, 99, 103, 104])})
    got = [(b.value, b.from_ts, b.to_ts, b.bars) for b in res.condition_bands[UID]]
    assert got == [("true", 60, 120, 2), ("true", 240, 300, 2)]


def test_bands_carry_role_and_timeframe() -> None:
    res = run_trial(load_rule(SINGLE), {UID: bars([101, 102])})
    band = res.condition_bands[UID][0]
    assert band.timeframe == "1m" and band.role


def test_counts_separate_unknown_from_false() -> None:
    """某一级全是 unknown = 指标还没预热完，不是"规则太严"。"""
    raw = {**SINGLE,
           "conditions": [{"on": "1m", "mode": "state", "when": "close > ema(close, 5)"}]}
    res = run_trial(load_rule(raw), {UID: bars([100, 101, 102, 103, 104, 105, 106])})
    tally = res.condition_counts[UID][load_rule(raw).conditions[0].role]
    assert tally["unknown"] == 4, "ema(5) 前 4 根没有值，应当是 unknown 而不是 false"
    assert tally["true"] + tally["false"] == 3


def test_raw_and_satisfied_can_disagree_for_event_mode() -> None:
    """event 只认 False->True 跳变。"表达式成立但链路不认"是"为什么没触发"最常见的答案。"""
    raw = {**SINGLE, "conditions": [{"on": "1m", "mode": "event", "when": "close > 100"}]}
    rule = load_rule(raw)
    rec = ConditionRecorder()
    store = BarStore(timeframes=[])
    engine = RuleEngine([rule], store, recorder=rec)
    for b in bars([99, 101, 102, 103]):
        engine.on_bars(store.push(b))
    expr = [(x.from_ts, x.to_ts) for x in rec.bands(which="raw")[UID]]
    chain = [(x.from_ts, x.to_ts) for x in rec.bands(which="satisfied")[UID]]
    assert expr == [(120, 240)], "表达式从第 2 根起一直成立"
    assert chain == [(120, 120)], "链路只认第一次跳变那一根"


def test_recorder_never_changes_engine_behaviour() -> None:
    """钩子是只读旁路。挂上它前后，信号必须一模一样。"""
    rule, data = load_rule(SINGLE), bars([99, 101, 102, 98, 105])
    out = []
    for rec in (None, ConditionRecorder()):
        store = BarStore(timeframes=[])
        engine = RuleEngine([rule], store, recorder=rec)
        out.append([s.as_dict() for b in data for s in engine.on_bars(store.push(b))])
    assert out[0] == out[1]


def test_multi_level_bands_cover_every_role() -> None:
    raw = {
        "id": "m", "universe": [UID],
        "timeframes": {"trend": "5m", "trigger": "1m"},
        "conditions": [
            {"on": "trend", "mode": "state", "when": "close > 0"},
            {"on": "trigger", "mode": "state", "when": "close > 100"},
        ],
        "emit": {"direction": "long", "dedup_key": "{symbol}:{rule}:{bar_close_ts}"},
    }
    res = run_trial(load_rule(raw), {UID: bars([101] * 20)})
    roles = {b.role for b in res.condition_bands[UID]}
    assert roles == {"trend", "trigger"}
    tfs = {b.role: b.timeframe for b in res.condition_bands[UID]}
    assert tfs == {"trend": "5m", "trigger": "1m"}


@pytest.mark.parametrize("which", ["satisfied", "raw"])
def test_bands_are_sorted_and_non_overlapping(which: str) -> None:
    rec = ConditionRecorder()
    rule = load_rule(SINGLE)
    store = BarStore(timeframes=[])
    engine = RuleEngine([rule], store, recorder=rec)
    for b in bars([99, 101, 102, 98, 105, 106, 97]):
        engine.on_bars(store.push(b))
    got = rec.bands(which=which)[UID]
    for prev, cur in zip(got, got[1:], strict=False):
        assert prev.to_ts < cur.from_ts


def test_trial_evaluates_signals_older_than_the_store_memory_cap() -> None:
    """**试算的评价序列必须是完整输入，不是 BarStore 里那份被裁过的。**

    BarStore 有 MAX_BARS 的内存上限（开发机 2 核 4G），超出就裁掉最旧的。
    原来 run_trial 用 `store.view(uid, end).bars(trigger_tf)` 取评价序列，注释还
    写着"给全量是安全的" —— 它不是全量。于是早于保留窗口的信号在 evaluate_all
    里二分落到 0，**静默拿窗口开头那几根当"未来"**：真实数据上三条相隔两周的
    信号算出一模一样的 entry/exit，报告上毫无异常。扳机是 1m 时最严重
    （5000 根只有三天半）。

    这条测试喂超过上限的 bar，检查最早那条信号的 entry 紧跟着它自己的 fired_at。
    """
    rule = load_rule(SINGLE)
    n = MAX_BARS * 2
    # 前半段在 100 之上（会触发），后半段继续走高，保证序列远超内存上限
    series = bars([101.0 + (i % 7) for i in range(n)])
    res = run_trial(rule, {UID: series})

    assert res.signals, "这段行情应该有信号"
    first = min(res.signals, key=lambda s: s.fired_at)
    assert first.fired_at < series[-MAX_BARS].close_ts, "最早的信号应落在被裁掉的那段里"

    by_fired = {o.fired_at: o for o in res.outcomes}
    out = by_fired[first.fired_at]
    assert out.reason is not ExitReason.NO_DATA, "完整序列在手，不该评价不了"
    # 进场必须紧跟它自己那根，而不是保留窗口的开头
    assert first.fired_at < out.entry_ts <= first.fired_at + 120

    # 每条信号的 entry 都要各不相同 —— 全挤在同一根上正是那个 bug 的表征
    entries = [o.entry_ts for o in res.outcomes if o.evaluated]
    assert len(set(entries)) == len(entries), "不同信号被评价到了同一根 bar 上"
