"""多级别规则引擎单测。脱网、无磁盘。

覆盖 M2 的四条验收里的三条（第四条 replay==live 在 test_replay.py）：
链路回退、TTL、换月隔离，外加最容易写错的**记账与判定的先后顺序**。
"""

from __future__ import annotations

from typing import Any

import pytest

from sigdesk.core.models import Bar, Timeframe
from sigdesk.feed.okx import normalize_candles
from sigdesk.rules.engine import RuleEngine
from sigdesk.rules.loader import load_rule
from sigdesk.rules.model import Signal
from sigdesk.rules.state import Phase
from sigdesk.store.bar_store import BarStore

UID = "CRYPTO.OKX.BTCUSDT.PERP"
OTHER = "CN.SHFE.rb2610"


def bar(close_ts: int, close: float, symbol: str = UID, volume: float = 10.0) -> Bar:
    return Bar(symbol, Timeframe.M1, close_ts - 60, close_ts, close, close + 0.5, close - 0.5,
               close, volume)


def build(raw: dict[str, Any], timeframes: list[Timeframe]) -> tuple[RuleEngine, BarStore]:
    store = BarStore(timeframes=timeframes)
    return RuleEngine([load_rule(raw)], store), store


def run(engine: RuleEngine, store: BarStore, bars: list[Bar]) -> list[Signal]:
    """按实盘路径走：push 得到"本批同时收盘的 bar"，整批交给引擎。"""
    out: list[Signal] = []
    for b in bars:
        out.extend(engine.on_bars(store.push(b)))
    return out


def phase(engine: RuleEngine, rule_id: str, symbol: str, chain_len: int) -> Phase:
    return engine.instance(rule_id, symbol).state.phase(chain_len)


# ---------------------------------------------------------------- 记账与判定的顺序


TWO_LEVEL = {
    "id": "two",
    "universe": [UID],
    "timeframes": {"setup": "5m", "trigger": "1m"},
    "conditions": [
        {"on": "setup", "mode": "state", "when": "close > 100"},
        {"on": "trigger", "mode": "state", "when": "close > 100"},
    ],
    "emit": {"direction": "long"},
}


def test_same_instant_close_is_booked_before_the_trigger_decides() -> None:
    """5m(setup) 与 1m(trigger) 同刻收盘时，扳机必须读到**这一根** 5m 的结果。

    记账与判定若不分两步（或者判定先于同批其他周期的记账），信号会整整晚一根 bar。
    这里 300 秒处 5m 桶收盘、且两级条件同时成立 ⇒ 必须就在 300 触发。
    """
    engine, store = build(TWO_LEVEL, [Timeframe.M5])
    bars = [bar(60 * i, 99.0) for i in range(1, 5)] + [bar(300, 101.0)]

    signals = run(engine, store, bars)

    assert [s.fired_at for s in signals] == [300], "同刻收盘的大级别没有先记账"
    assert signals[0].role_bars == {"setup": 300, "trigger": 300}


def test_trigger_alone_does_not_fire_without_the_upper_level() -> None:
    """1m 成立但 5m 桶还没收盘 ⇒ 上一级仍未满足，不该触发。"""
    engine, store = build(TWO_LEVEL, [Timeframe.M5])
    signals = run(engine, store, [bar(60, 101.0), bar(120, 101.0)])
    assert signals == []
    assert phase(engine, "two", UID, 2) is Phase.IDLE


# ---------------------------------------------------------------- window 模式


WINDOW_RULE = {
    "id": "win",
    "universe": [UID],
    "timeframes": {"setup": "1m", "trigger": "1m"},
    "conditions": [
        {"on": "setup", "mode": "window", "within": 3, "when": "close > 100"},
        {"on": "trigger", "mode": "event", "when": "volume > 50"},
    ],
    "emit": {"direction": "long"},
}


WINDOW_AFTER_TREND = {
    "id": "wat",
    "universe": [UID],
    "timeframes": {"trend": "1m", "setup": "1m", "trigger": "1m"},
    "conditions": [
        {"on": "trend", "mode": "state", "when": "high > 1000"},
        {"on": "setup", "mode": "window", "within": 3, "when": "close > 100"},
        {"on": "trigger", "mode": "event", "when": "volume > 50"},
    ],
    "emit": {"direction": "long"},
}


def _b(ts: int, *, close: float, trend_on: bool, volume: float) -> Bar:
    """趋势信号挂在 high 上，好让 close（setup 用）与它互不干扰。
    high 必须 >= close，否则 Bar 会正确地拒绝这根非法数据。"""
    high = 9999.0 if trend_on else close + 1.0
    return Bar(UID, Timeframe.M1, ts - 60, ts, close, high, close - 1.0, close, volume)


@pytest.mark.parametrize(
    ("trend_ok_at", "expect_pass_setup"),
    [(180, True), (300, False)],
)
def test_window_governs_whether_the_chain_can_advance_past_that_stage(
    trend_ok_at: int, expect_pass_setup: bool
) -> None:
    """``within`` 管的是"链路能否在**这一根**推进过这一段"。

    setup 在第 1 根成立。趋势在第 3 根才成立时，窗口（第 1/2/3 根）里还有它 ⇒ 能推进过 setup；
    趋势拖到第 5 根，窗口（第 3/4/5 根）已经没有它 ⇒ 推进不过去。
    """
    engine, store = build(WINDOW_AFTER_TREND, [])
    bars = [
        _b(ts, close=101.0 if ts == 60 else 99.0, trend_on=ts >= trend_ok_at, volume=10.0)
        for ts in (60, 120, 180, 240, 300)
    ]
    run(engine, store, bars)

    stage = engine.instance("wat", UID).state.stage
    assert (stage >= 2) is expect_pass_setup, f"stage={stage}"


def test_armed_duration_is_governed_by_ttl_not_by_the_window() -> None:
    """setup 一旦推进过去，链路就 armed 了；窗口随后滑走**不会**让它退回来。

    这是刻意的分工：``within`` 说"最近有没有发生过"，TTL 说"armed 能撑多久"。
    window 是瞬时型条件，若要求它持续成立，链路会永远推进不下去。
    要限制 armed 时长请配 ttl，别指望窗口。
    """
    engine, store = build(WINDOW_RULE, [])
    bars = [
        bar(60, 101.0),                    # setup 成立 -> armed
        bar(120, 99.0),
        bar(180, 99.0),
        bar(240, 99.0),                    # 窗口里已无 setup
        bar(300, 99.0, volume=99.0),       # 扳机边沿
    ]
    assert [s.fired_at for s in run(engine, store, bars)] == [300]

    # 同样的行情，配上 ttl: 2 bars 之后就会在扳机到来前作废
    engine2, store2 = build(dict(WINDOW_RULE, emit={"direction": "long", "ttl": "2 bars"}), [])
    assert run(engine2, store2, bars) == []


def test_window_needs_the_edge_on_the_trigger() -> None:
    """扳机是 event：量一直大不会每根都报，只在"刚放量"那一根报。"""
    engine, store = build(WINDOW_RULE, [])
    bars = [bar(60, 101.0), bar(120, 101.0, volume=99.0), bar(180, 101.0, volume=99.0)]
    assert [s.fired_at for s in run(engine, store, bars)] == [120]


# ---------------------------------------------------------------- 回退（验收项）


THREE_LEVEL = {
    "id": "three",
    "universe": [UID],
    "timeframes": {"trend": "5m", "setup": "1m", "trigger": "1m"},
    "conditions": [
        {"on": "trend", "mode": "state", "when": "close > 100"},
        {"on": "setup", "mode": "window", "within": 5, "when": "volume > 50"},
        {"on": "trigger", "mode": "event", "when": "close > open"},
    ],
    "emit": {"direction": "long", "ttl": "3 bars"},
}


def test_trend_failure_retreats_and_leaves_no_armed_state() -> None:
    """M2 验收：trend 失效时链路正确回退，无残留 armed 状态。"""
    engine, store = build(THREE_LEVEL, [Timeframe.M5])
    # 先把 5m trend 做成立（300 秒处 5m 桶收在 101）
    warm = [bar(60 * i, 99.0) for i in range(1, 5)] + [bar(300, 101.0)]
    run(engine, store, warm)
    run(engine, store, [bar(360, 101.0, volume=99.0)])  # setup 成立 -> armed
    assert phase(engine, "three", UID, 3) is Phase.ARMED
    assert engine.instance("three", UID).state.ttl_left == 3

    # 让 5m trend 失效：600 秒处的 5m 桶收在 99
    run(engine, store, [bar(t, 99.0) for t in (420, 480, 540, 600)])

    state = engine.instance("three", UID).state
    assert phase(engine, "three", UID, 3) is Phase.IDLE
    assert (state.stage, state.ttl_left, state.armed_at) == (0, 0, 0), "残留了 armed 状态"


def test_ttl_expires_the_armed_chain() -> None:
    """M2 验收（TTL）：armed 后 3 根扳机 bar 内没触发就作废，退回上一段。"""
    engine, store = build(THREE_LEVEL, [Timeframe.M5])
    run(engine, store, [bar(60 * i, 99.0) for i in range(1, 5)] + [bar(300, 101.0)])
    run(engine, store, [bar(360, 101.0, volume=99.0)])
    assert phase(engine, "three", UID, 3) is Phase.ARMED

    # 后续 bar 都收阴（扳机不成立），且 trend 保持成立
    for i, t in enumerate((420, 480, 540, 600), start=1):
        run(engine, store, [Bar(UID, Timeframe.M1, t - 60, t, 101.0, 101.5, 100.0, 100.5, 1.0)])
        if i < 4:
            continue
    state = engine.instance("three", UID).state
    assert state.stage < 2, "TTL 到期后仍停在 armed"
    assert state.ttl_left == 0


# ---------------------------------------------------------------- 换月隔离（验收项）


def test_symbols_do_not_share_state() -> None:
    """M2 验收：换月当日新旧合约状态互不污染。

    状态按 (rule, symbol) 分实例；旧合约推进到 armed 不会让新合约跟着 armed。
    """
    raw = dict(TWO_LEVEL, universe=[UID, OTHER])
    engine, store = build(raw, [Timeframe.M5])

    run(engine, store, [bar(60 * i, 99.0) for i in range(1, 5)] + [bar(300, 101.0)])
    assert phase(engine, "two", UID, 2) is not Phase.IDLE

    inst_other = engine.instance("two", OTHER)
    assert inst_other.state.stage == 0, "另一个合约被旧合约的推进污染了"
    assert all(not log.results for log in inst_other.logs.values())

    # 新合约自己走一遍，互不影响
    new_bars = [bar(60 * i, 99.0, symbol=OTHER) for i in range(1, 5)] + [
        bar(300, 101.0, symbol=OTHER)
    ]
    signals = run(engine, store, new_bars)
    assert {s.symbol for s in signals} == {OTHER}


def test_rules_do_not_share_state() -> None:
    store = BarStore(timeframes=[Timeframe.M5])
    a = load_rule(dict(TWO_LEVEL, id="a"))
    b = load_rule(dict(TWO_LEVEL, id="b", emit={"direction": "short"}))
    engine = RuleEngine([a, b], store)
    signals = run(engine, store, [bar(60 * i, 99.0) for i in range(1, 5)] + [bar(300, 101.0)])
    assert sorted(s.rule_id for s in signals) == ["a", "b"]
    assert engine.instance("a", UID) is not engine.instance("b", UID)


# ---------------------------------------------------------------- 去重与快照


def test_dedup_key_binds_to_the_upper_level_bar() -> None:
    """默认玩法：把去重绑在大级别 bar 上 = 同一根大级别 bar 内同规则同标的只报一次。"""
    raw = dict(
        TWO_LEVEL,
        conditions=[
            {"on": "setup", "mode": "state", "when": "close > 100"},
            {"on": "trigger", "mode": "state", "when": "close > 100"},
        ],
        emit={"direction": "long", "dedup_key": "{symbol}:{rule}:{setup_bar_close_ts}"},
    )
    engine, store = build(raw, [Timeframe.M5])
    # 300 之后条件一直成立，共跨越两根 5m（300 与 600）⇒ 每根 5m 只应报一次
    bars = [bar(60 * i, 99.0) for i in range(1, 5)] + [bar(60 * i, 101.0) for i in range(5, 11)]

    signals = run(engine, store, bars)

    assert [s.dedup_key.rsplit(":", 1)[-1] for s in signals] == ["300", "600"], (
        "去重键绑在 5m 上时，每根 5m 内只应报一次"
    )
    assert [s.fired_at for s in signals] == [300, 600], (
        "去重键随 5m bar 变化：5m 不换，中间的扳机全被去重挡住"
    )


def test_unknown_dedup_placeholder_is_reported_with_available_names() -> None:
    engine, store = build(
        dict(TWO_LEVEL, emit={"direction": "long", "dedup_key": "{nope}"}), [Timeframe.M5]
    )
    with pytest.raises(ValueError, match="未知占位符"):
        run(engine, store, [bar(60 * i, 101.0) for i in range(1, 6)])


def test_context_by_role_snapshots_each_level(btc_swap_okx: dict[str, Any]) -> None:
    """M2 验收：推送内容含**各级别**关键值。"""
    raw = {
        "id": "ctx",
        "universe": [UID],
        "timeframes": {"trend": "15m", "trigger": "1m"},
        "conditions": [
            {"on": "trend", "mode": "state", "when": "close > 0"},
            {"on": "trigger", "mode": "state", "when": "close > 0"},
        ],
        "context": {"atr14": "atr(14)"},
        "context_by_role": {
            "trend": {"ema5": "ema(close,5)", "rsi14": "rsi(14)"},
            "trigger": {"ema20": "ema(close,20)"},
        },
        "emit": {"direction": "long", "dedup_key": "{symbol}:{rule}:{trend_bar_close_ts}"},
    }
    engine, store = build(raw, [Timeframe.M15])
    bars = normalize_candles(btc_swap_okx["1m"], symbol=UID, timeframe=Timeframe.M1)
    signals = run(engine, store, bars)

    assert signals
    sig = signals[-1]
    assert set(sig.context) == {
        "close", "volume", "atr14", "trend.ema5", "trend.rsi14", "trigger.ema20"
    }
    assert sig.context["trend.ema5"] is not None
    assert sig.context["trigger.ema20"] is not None
    assert sig.context["trend.ema5"] != sig.context["trigger.ema20"], (
        "两个级别的 ema 不该是同一个数 —— 说明角色周期没生效"
    )
    assert sig.role_bars["trend"] % 900 == 0
    assert sig.role_bars["trigger"] % 60 == 0


# ---------------------------------------------------------------- 预热


def test_prime_books_without_firing(btc_swap_okx: dict[str, Any]) -> None:
    """预热要把 window/event/指标都喂饱，但绝不能把历史行情当实时报一遍。"""
    engine, store = build(WINDOW_RULE, [])
    history = [bar(60 * i, 101.0, volume=99.0) for i in range(1, 20)]
    derived = [b for src in history for b in store.push(src)]
    engine.prime(derived)

    assert engine.instance("win", UID).state.stage == 0, "预热动了状态机"
    log = engine.instance("win", UID).logs["setup"]
    assert log.last_ts == history[-1].close_ts, "预热没有记账"

    # 预热之后第一根真实 bar：setup 已在窗口内，扳机是 event 且上一根已成立 ⇒ 不该误报
    assert run(engine, store, [bar(60 * 20, 101.0, volume=99.0)]) == []


# ---------------------------------------------------------------- 换月（验收项）


OLD_CONTRACT = "CN.SHFE.rb2610"
NEW_CONTRACT = "CN.SHFE.rb2701"


def test_contract_rollover_keeps_states_and_dedup_keys_apart() -> None:
    """M2 验收：换月当日新旧合约状态互不污染。

    换月当日新旧合约都在订阅里（ARCHITECTURE §3.4：状态机**不迁移**，新合约从 IDLE 起）。
    要保证三件事：状态实例分开、去重键不撞车、旧合约的推进不会让新合约"继承"进度。
    """
    raw = dict(
        TWO_LEVEL,
        universe=[OLD_CONTRACT, NEW_CONTRACT],
        emit={"direction": "long", "dedup_key": "{symbol}:{rule}:{setup_bar_close_ts}"},
    )
    engine, store = build(raw, [Timeframe.M5])

    # 旧合约走完整条链路并触发
    old_bars = [bar(60 * i, 99.0, symbol=OLD_CONTRACT) for i in range(1, 5)]
    old_bars += [bar(300, 101.0, symbol=OLD_CONTRACT)]
    old_signals = run(engine, store, old_bars)
    assert [s.symbol for s in old_signals] == [OLD_CONTRACT]

    # 新合约此刻仍是 IDLE：没有继承任何进度
    new_inst = engine.instance("two", NEW_CONTRACT)
    assert new_inst.state == engine.instance("two", NEW_CONTRACT).state
    assert new_inst.state.stage == 0
    assert new_inst.state.last_fired_ts == 0

    # 新合约自己走一遍：同样能触发，且去重键与旧合约不冲突
    new_bars = [bar(60 * i, 99.0, symbol=NEW_CONTRACT) for i in range(1, 5)]
    new_bars += [bar(300, 101.0, symbol=NEW_CONTRACT)]
    new_signals = run(engine, store, new_bars)

    assert [s.symbol for s in new_signals] == [NEW_CONTRACT]
    assert old_signals[0].dedup_key != new_signals[0].dedup_key, "两个合约的去重键撞车了"
    assert old_signals[0].dedup_key.startswith(OLD_CONTRACT)
    assert new_signals[0].dedup_key.startswith(NEW_CONTRACT)
    # 旧合约的状态没有被新合约的推进带偏
    assert engine.instance("two", OLD_CONTRACT).state.last_fired_ts == 300


def test_rollover_snapshot_keeps_contracts_separate() -> None:
    """快照/恢复也要按合约分行，否则重启后换月状态会串。"""
    raw = dict(TWO_LEVEL, universe=[OLD_CONTRACT, NEW_CONTRACT])
    engine, store = build(raw, [Timeframe.M5])
    run(engine, store, [bar(60 * i, 99.0, symbol=OLD_CONTRACT) for i in range(1, 5)]
        + [bar(300, 101.0, symbol=OLD_CONTRACT)])
    run(engine, store, [bar(60 * i, 99.0, symbol=NEW_CONTRACT) for i in range(1, 4)])

    rows = engine.snapshot()
    symbols = {r["symbol"] for r in rows}

    assert symbols == {OLD_CONTRACT, NEW_CONTRACT}
    stages = {r["symbol"]: r["stage"] for r in rows}
    assert stages[OLD_CONTRACT] != stages[NEW_CONTRACT], "两个合约的阶段被存成同一个了"
