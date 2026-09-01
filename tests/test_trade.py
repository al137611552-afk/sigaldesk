"""交易层单测：仓位定量、风控闸、纸上撮合。全部纯逻辑，脱网无磁盘。

M4 验收之一是"每条风控规则有针对性单测（触发即拒单）"—— 下面 `test_reject_*` 系列
一条规则一条测试，且每条都**只**违反那一条，其余保持宽松，确保拒单原因不会张冠李戴。

最要紧的一条是 `test_paper_exit_matches_the_statistics_engine`：
撮合与信号质量统计必须给出同一个出场，否则"统计说赚钱、模拟盘说不赚"无从排查。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from sigdesk.core.models import Bar, Market, Symbol, Timeframe
from sigdesk.rules.model import Direction, Signal
from sigdesk.stats.outcome import ExitReason, OutcomeParams, evaluate
from sigdesk.trade.model import Account, FillKind, Intent, Position, RejectReason, Side
from sigdesk.trade.paper import FillParams, PaperBroker
from sigdesk.trade.risk import RiskGate, RiskParams
from sigdesk.trade.strategy import StrategyParams, lot_of, make_intent

BTC = "CRYPTO.OKX.BTCUSDT.PERP"
RB = "CN.SHFE.rb2610"

BTC_SYM = Symbol(uid=BTC, market=Market.CRYPTO, exchange="OKX", code="BTC-USDT-SWAP",
                 calendar="crypto_24x7", price_tick=0.1, multiplier=0.01)
RB_SYM = Symbol(uid=RB, market=Market.CN_FUTURES, exchange="SHFE", code="rb2610",
                calendar="cn_night_23", price_tick=1.0, multiplier=10.0)


def sig(price: float = 100.0, direction: Direction = Direction.LONG, symbol: str = BTC,
        key: str = "k1", ts: int = 600, ctx: dict[str, float | None] | None = None) -> Signal:
    return Signal(rule_id="r1", symbol=symbol, direction=direction, timeframe=Timeframe.M1,
                  fired_at=ts, trigger_price=price, dedup_key=key, context=ctx or {})


def bar(ts: int, o: float, h: float, low: float, c: float, symbol: str = BTC,
        day: str | None = None) -> Bar:
    return Bar(symbol, Timeframe.M1, ts - 60, ts, o, h, low, c, 1.0, trading_day=day)


def account(cash: float = 100_000.0) -> Account:
    return Account(cash=cash)


def intent(**kw: object) -> Intent:
    base: dict[str, object] = dict(
        signal_key="k1", rule_id="r1", symbol=BTC, side=Side.BUY, qty=1.0,
        created_at=600, ref_price=100.0, multiplier=1.0, stop=99.5, target=101.0,
    )
    base.update(kw)
    return Intent(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 仓位定量


def test_neutral_signal_produces_no_intent() -> None:
    """neutral 是"去看一眼"的提示，不是方向判断 —— 与统计口径一致（它也不进胜率）。"""
    assert make_intent(sig(direction=Direction.NEUTRAL), BTC_SYM, 100_000.0) is None


def test_risk_sizing_keeps_每笔风险贴着预算() -> None:
    """按风险定量：止损远则手数少，止损近则手数多，每笔最大亏损大致相等。"""
    p = StrategyParams(mode="risk", risk_per_trade=0.01,
                       exits=OutcomeParams(stop_pct=0.005, target_pct=0.01))
    got = make_intent(sig(price=100.0), BTC_SYM, 100_000.0, p)

    assert got is not None
    assert got.side is Side.BUY
    # 预算 1000；每单位风险 = 0.5 × 0.01 = 0.005 ⇒ 200000 单位，按 lot=0.01 取整
    assert got.risk == pytest.approx(1000.0, rel=1e-6)
    assert got.stop == pytest.approx(99.5)
    assert got.target == pytest.approx(101.0)


def test_risk_sizing_shrinks_when_the_stop_is_wider() -> None:
    p_tight = StrategyParams(mode="risk", exits=OutcomeParams(stop_pct=0.005))
    p_wide = StrategyParams(mode="risk", exits=OutcomeParams(stop_pct=0.02))
    tight = make_intent(sig(), BTC_SYM, 100_000.0, p_tight)
    wide = make_intent(sig(), BTC_SYM, 100_000.0, p_wide)
    assert tight and wide
    assert wide.qty < tight.qty
    assert wide.risk == pytest.approx(tight.risk, rel=1e-3), "两者的单笔风险应当基本相等"


def test_fixed_and_notional_sizing() -> None:
    fixed = make_intent(sig(), BTC_SYM, 100_000.0, StrategyParams(mode="fixed", fixed_qty=3.0))
    assert fixed and fixed.qty == pytest.approx(3.0)
    notional = make_intent(sig(price=100.0), BTC_SYM, 100_000.0,
                           StrategyParams(mode="notional", notional_per_trade=500.0))
    assert notional and notional.notional == pytest.approx(500.0, rel=1e-2)


def test_lot_rounding_is_always_down() -> None:
    """向上取整会让单笔风险超出预算 —— 宁可少做一点。"""
    p = StrategyParams(mode="fixed", fixed_qty=2.7, default_lot=1.0)
    got = make_intent(sig(), RB_SYM, 100_000.0, p)
    assert got and got.qty == 2.0


def test_below_one_lot_produces_no_intent() -> None:
    """不足一手时不凑整 —— 凑整就是放大风险。"""
    p = StrategyParams(mode="risk", risk_per_trade=0.0001,
                       exits=OutcomeParams(stop_pct=0.02))
    assert make_intent(sig(price=4000.0), RB_SYM, 1000.0, p) is None


def test_lot_defaults_by_market() -> None:
    """期货按手（1），加密按交易所的最小变动（= Symbol.multiplier，即 ctVal）。"""
    assert lot_of(RB_SYM, StrategyParams()) == 1.0
    assert lot_of(BTC_SYM, StrategyParams()) == pytest.approx(0.01)


def test_atr_based_stops_flow_through_from_the_signal_context() -> None:
    """期货各品种波动率差异极大，固定百分比会让活跃品种全打止损。"""
    p = StrategyParams(mode="fixed", fixed_qty=1.0,
                       exits=OutcomeParams(atr_key="atr14", stop_atr=2.0, target_atr=4.0))
    got = make_intent(sig(price=100.0, ctx={"atr14": 3.0}), BTC_SYM, 100_000.0, p)
    assert got and got.stop == pytest.approx(94.0) and got.target == pytest.approx(112.0)


def test_short_intent_has_stop_above_entry() -> None:
    got = make_intent(sig(direction=Direction.SHORT), BTC_SYM, 100_000.0,
                      StrategyParams(mode="fixed"))
    assert got and got.side is Side.SELL
    assert got.stop is not None and got.stop > got.ref_price
    assert got.target is not None and got.target < got.ref_price


def test_zero_equity_produces_no_intent() -> None:
    assert make_intent(sig(), BTC_SYM, 0.0) is None


@pytest.mark.parametrize("bad", [{"risk_per_trade": 0.0}, {"risk_per_trade": 0.9},
                                 {"fixed_qty": 0.0}, {"notional_per_trade": -1.0}])
def test_strategy_params_are_validated(bad: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        StrategyParams(**bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 风控：一条规则一条测试


def loose() -> RiskParams:
    """全部放宽，好让每条测试**只**违反它要测的那一条。"""
    return RiskParams(max_risk_per_trade=1.0, max_notional_per_trade=0.0,
                      max_symbol_exposure=10.0, max_total_exposure=10.0,
                      daily_loss_limit=0.0, max_orders_per_window=0)


def test_reject_duplicate_signal() -> None:
    """幂等：重启补喂会重复产出同一条信号，交易侧必须挡住（FR-7.3）。"""
    gate = RiskGate(loose())
    i = intent()
    assert gate.check(i, account(), {BTC: 100.0}, "2026-08-31") is None
    gate.accept(i)
    rej = gate.check(i, account(), {BTC: 100.0}, "2026-08-31")
    assert rej and rej.reason is RejectReason.DUPLICATE


def test_reject_zero_quantity() -> None:
    gate = RiskGate(loose())
    rej = gate.check(intent(qty=0.0), account(), {BTC: 100.0}, "2026-08-31")
    assert rej and rej.reason is RejectReason.ZERO_QTY


def test_reject_when_equity_is_gone() -> None:
    gate = RiskGate(loose())
    rej = gate.check(intent(), account(cash=0.0), {BTC: 100.0}, "2026-08-31")
    assert rej and rej.reason is RejectReason.NO_EQUITY


def test_reject_per_trade_risk() -> None:
    gate = RiskGate(replace(loose(), max_risk_per_trade=0.001))
    # 权益 100000 × 0.1% = 100；这笔风险 = |100-99.5| × 1000 手 × 1 = 500
    rej = gate.check(intent(qty=1000.0), account(), {BTC: 100.0}, "2026-08-31")
    assert rej and rej.reason is RejectReason.PER_TRADE_RISK
    assert "500" in rej.detail


def test_reject_per_trade_notional() -> None:
    gate = RiskGate(replace(loose(), max_notional_per_trade=0.01))
    rej = gate.check(intent(qty=100.0), account(), {BTC: 100.0}, "2026-08-31")
    assert rej and rej.reason is RejectReason.PER_TRADE_NOTIONAL


def test_reject_symbol_exposure() -> None:
    gate = RiskGate(replace(loose(), max_symbol_exposure=0.01))
    acc = account()
    acc.positions[BTC] = Position(symbol=BTC, side=Side.BUY, qty=8.0, entry_price=100.0,
                                  multiplier=1.0, opened_at=0, signal_key="old")
    rej = gate.check(intent(qty=5.0, signal_key="k2"), acc, {BTC: 100.0}, "2026-08-31")
    assert rej and rej.reason is RejectReason.SYMBOL_EXPOSURE


def test_reject_total_exposure() -> None:
    gate = RiskGate(replace(loose(), max_total_exposure=0.01))
    acc = account()
    acc.positions[RB] = Position(symbol=RB, side=Side.BUY, qty=1.0, entry_price=900.0,
                                 multiplier=1.0, opened_at=0, signal_key="old")
    rej = gate.check(intent(qty=5.0), acc, {BTC: 100.0, RB: 900.0}, "2026-08-31")
    assert rej and rej.reason is RejectReason.TOTAL_EXPOSURE


def test_reject_daily_loss_circuit_breaker() -> None:
    """熔断是一票否决，且排在敞口检查之前 —— 日志里要显示最根本的原因。"""
    gate = RiskGate(replace(loose(), daily_loss_limit=0.02,
                                  max_risk_per_trade=0.0001))
    acc = account()
    acc.daily_realized["2026-08-31"] = -3000.0  # 权益 10 万，亏 3% > 2%
    rej = gate.check(intent(qty=1000.0), acc, {BTC: 100.0}, "2026-08-31")
    assert rej and rej.reason is RejectReason.DAILY_LOSS, "熔断应当先于单笔风险被报出"


def test_daily_loss_is_per_trading_day() -> None:
    """昨天亏爆不该影响今天开盘。"""
    gate = RiskGate(replace(loose(), daily_loss_limit=0.02))
    acc = account()
    acc.daily_realized["2026-08-30"] = -9000.0
    assert gate.check(intent(), acc, {BTC: 100.0}, "2026-08-31") is None


def test_reject_rate_limit_using_bar_time_not_wall_clock() -> None:
    """频率限制按 **bar 时间**算。用墙钟的话回放与实盘会拒不同的单，
    M2 好不容易挣来的逐条一致会在交易层丢掉。"""
    gate = RiskGate(replace(loose(), max_orders_per_window=2,
                                  rate_window_s=3600))
    for n, ts in enumerate([1000, 1060]):
        i = intent(signal_key=f"k{n}", created_at=ts)
        assert gate.check(i, account(), {BTC: 100.0}, "d") is None
        gate.accept(i)

    blocked = gate.check(intent(signal_key="k3", created_at=1120), account(), {BTC: 100.0}, "d")
    assert blocked and blocked.reason is RejectReason.RATE_LIMIT

    # 窗口滑过之后放行
    ok = gate.check(intent(signal_key="k4", created_at=1000 + 3700), account(), {BTC: 100.0}, "d")
    assert ok is None


def test_rejected_orders_do_not_consume_the_rate_budget() -> None:
    """被拒的单不该占额度 —— 否则一条坏规则能把整个窗口刷爆。"""
    gate = RiskGate(replace(loose(), max_orders_per_window=1))
    gate.check(intent(signal_key="bad", qty=0.0), account(), {BTC: 100.0}, "d")
    assert gate.check(intent(signal_key="good"), account(), {BTC: 100.0}, "d") is None


def test_gate_records_rejections_for_the_panel() -> None:
    gate = RiskGate(loose())
    gate.check(intent(qty=0.0), account(), {BTC: 100.0}, "d")
    assert len(gate.rejections) == 1
    assert gate.rejections[0].as_dict()["reason"] == "zero_qty"


def test_idempotency_survives_restart() -> None:
    """不恢复 seen 就会重复下单 —— 这是交易层的"不重报"。"""
    gate = RiskGate(loose())
    i = intent()
    gate.check(i, account(), {BTC: 100.0}, "d")
    gate.accept(i)

    revived = RiskGate(loose())
    revived.restore(gate.snapshot())
    rej = revived.check(i, account(), {BTC: 100.0}, "d")
    assert rej and rej.reason is RejectReason.DUPLICATE


def test_restore_without_seen_would_refire() -> None:
    """反证：说明上一条测的确实是 seen 在起作用。"""
    revived = RiskGate(loose())
    revived.restore({"seen": [], "recent": []})
    assert revived.check(intent(), account(), {BTC: 100.0}, "d") is None


# ---------------------------------------------------------------- 纸上撮合


def broker(**kw: float) -> PaperBroker:
    p = FillParams(fee_bps=kw.get("fee_bps", 0.0), slippage_bps=kw.get("slippage_bps", 0.0))
    return PaperBroker(account=account(), params=p)


def test_entry_fills_at_the_next_bar_open_not_the_signal_bar() -> None:
    """信号在 bar 收盘时才成立，那个价已经过去、成交不到 —— 与 ADR-0008 的入场口径一致。"""
    b = broker()
    b.submit(intent(created_at=600, ref_price=100.0, stop=None, target=None))

    same_bar = b.on_bar(bar(600, 100.0, 101.0, 99.0, 100.0))
    assert same_bar == [], "信号那一根不该成交"

    (fill,) = b.on_bar(bar(660, 105.0, 106.0, 104.0, 105.5))
    assert fill.kind is FillKind.ENTRY
    assert fill.price == pytest.approx(105.0), "成交价应当是次根开盘价"


def test_slippage_always_moves_against_you() -> None:
    """买贵一点、卖便宜一点。给自己算好价的滑点等于没有滑点。"""
    long_b = broker(slippage_bps=10.0)
    long_b.submit(intent(side=Side.BUY, stop=None, target=None))
    (buy,) = long_b.on_bar(bar(660, 100.0, 101.0, 99.0, 100.0))
    assert buy.price > 100.0

    short_b = broker(slippage_bps=10.0)
    short_b.submit(intent(side=Side.SELL, stop=None, target=None))
    (sell,) = short_b.on_bar(bar(660, 100.0, 101.0, 99.0, 100.0))
    assert sell.price < 100.0


def test_fees_are_charged_on_both_sides() -> None:
    b = broker(fee_bps=10.0)
    b.submit(intent(qty=10.0, multiplier=1.0))
    b.on_bar(bar(660, 100.0, 100.2, 99.9, 100.0))
    entry_fee = b.account.fees
    assert entry_fee > 0
    b.on_bar(bar(720, 100.0, 102.0, 99.9, 101.5))  # 触及止盈 101
    assert b.account.fees > entry_fee, "平仓也要收一次"
    assert not b.account.positions


def test_stop_wins_when_both_hit_on_the_same_bar() -> None:
    """bar 给不出高低点先后，取止盈就是在给自己发奖。"""
    b = broker()
    b.submit(intent(stop=99.5, target=101.0))
    b.on_bar(bar(660, 100.0, 100.1, 99.9, 100.0))
    (fill,) = b.on_bar(bar(720, 100.0, 102.0, 99.0, 100.0))
    assert fill.kind is FillKind.STOP
    assert fill.price == pytest.approx(99.5)


def test_entry_bar_can_also_stop_you_out() -> None:
    """以这根的**开盘价**入场后，这根 bar 随后真的走过了它的高低区间 ——
    触及止损是真实发生的，不是偷看未来。统计侧（ADR-0008）同样如此，两边必须一致。"""
    b = broker()
    b.submit(intent(stop=99.5, target=101.0))
    fills = b.on_bar(bar(660, 100.0, 105.0, 95.0, 100.0))
    assert [f.kind for f in fills] == [FillKind.ENTRY, FillKind.STOP]
    assert fills[1].price == pytest.approx(99.5)


def test_horizon_counts_the_entry_bar() -> None:
    """``horizon_bars=2`` 表示**含开仓那根在内**持有 2 根，与统计侧口径一致。

    如果这里从开仓的下一根开始数，同一条信号在统计里持有 2 根、在模拟盘里持有 3 根，
    两边的收益永远对不上。
    """
    b = broker()
    b.submit(intent(stop=1.0, target=1e9, horizon_bars=2))
    assert [f.kind for f in b.on_bar(bar(660, 100.0, 100.1, 99.9, 100.0))] == [FillKind.ENTRY]
    (fill,) = b.on_bar(bar(720, 100.0, 100.1, 99.9, 100.05))
    assert fill.kind is FillKind.HORIZON
    assert fill.price == pytest.approx(100.05)


def test_realized_pnl_and_daily_bucket() -> None:
    b = broker()
    b.submit(intent(qty=2.0, multiplier=10.0, stop=99.0, target=101.0))
    b.on_bar(bar(660, 100.0, 100.1, 99.9, 100.0, day="2026-08-31"))
    (fill,) = b.on_bar(bar(720, 100.0, 102.0, 99.5, 101.5, day="2026-08-31"))

    assert fill.kind is FillKind.TARGET
    assert fill.realized == pytest.approx((101.0 - 100.0) * 2 * 10)
    assert b.account.realized == pytest.approx(fill.realized)
    assert b.account.daily_realized["2026-08-31"] == pytest.approx(fill.realized)
    assert b.account.cash == pytest.approx(100_000.0 + fill.realized)


def test_short_position_pnl_is_signed_correctly() -> None:
    b = broker()
    b.submit(intent(side=Side.SELL, qty=1.0, multiplier=1.0, stop=101.0, target=99.0))
    b.on_bar(bar(660, 100.0, 100.1, 99.95, 100.0))
    (fill,) = b.on_bar(bar(720, 100.0, 100.1, 98.5, 99.0))
    assert fill.kind is FillKind.TARGET
    assert fill.realized == pytest.approx(1.0), "空头下跌应当是盈利"


def test_one_position_per_symbol() -> None:
    """一个标的同时只持有一笔仓位；加仓/对锁会让"成交与信号一一对应"失去意义。"""
    b = broker()
    assert b.submit(intent(signal_key="a")) is True
    assert b.submit(intent(signal_key="b")) is False, "已有挂单时不该再接"
    b.on_bar(bar(660, 100.0, 100.1, 99.9, 100.0))
    assert b.submit(intent(signal_key="c")) is False, "已有持仓时不该再接"


def test_close_all_forces_exit() -> None:
    b = broker()
    b.submit(intent(qty=1.0, multiplier=1.0))
    b.on_bar(bar(660, 100.0, 100.1, 99.9, 100.0))
    (fill,) = b.close_all({BTC: 103.0}, ts=999)
    assert fill.kind is FillKind.FORCED
    assert fill.realized == pytest.approx(3.0)
    assert not b.account.positions


def test_unclosed_bars_are_ignored() -> None:
    b = broker()
    b.submit(intent())
    tentative = Bar(BTC, Timeframe.M1, 600, 660, 100.0, 101.0, 99.0, 100.0, 1.0, closed=False)
    assert b.on_bar(tentative) == []


# ------------------------------------------- 撮合与统计必须是同一套口径


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ([(660, 100.0, 100.2, 99.9, 100.0), (720, 100.0, 102.0, 99.9, 101.5)],
         ExitReason.TARGET),
        ([(660, 100.0, 100.2, 99.9, 100.0), (720, 100.0, 100.1, 99.0, 99.2)],
         ExitReason.STOP),
        ([(660, 100.0, 100.2, 99.9, 100.0), (720, 100.0, 102.0, 99.0, 100.0)],
         ExitReason.STOP),  # 同根同时触及 -> 止损
        ([(660, 100.0, 100.2, 99.9, 100.0), (720, 100.0, 100.2, 99.9, 100.1),
          (780, 100.0, 100.2, 99.9, 100.05)], ExitReason.HORIZON),
    ],
)
def test_paper_exit_matches_the_statistics_engine(
    path: list[tuple[int, float, float, float, float]], want: ExitReason
) -> None:
    """**撮合与信号质量统计必须给出同一个出场。**

    两边分家的后果是"统计说赚钱、模拟盘说不赚"而无从排查 —— 那正是 ADR-0001 要消灭的。
    这里把滑点与费用归零，两边应当逐字段对上：出场原因、出场价、持有根数。
    """
    exits = OutcomeParams(horizon_bars=2, stop_pct=0.005, target_pct=0.01)
    s = sig(price=100.0)
    bars = [bar(*row) for row in path]

    stat = evaluate(s, bars, exits)

    b = broker()
    got = make_intent(s, BTC_SYM, 100_000.0,
                      StrategyParams(mode="fixed", fixed_qty=1.0, exits=exits))
    assert got is not None
    b.submit(got)
    fills = b.feed(bars)

    assert stat.reason is want
    entry = next(f for f in fills if f.kind is FillKind.ENTRY)
    assert entry.price == pytest.approx(stat.entry_price), "入场价口径不一致"

    closes = [f for f in fills if f.kind is not FillKind.ENTRY]
    if want is ExitReason.NO_DATA:
        assert not closes
        return
    assert closes, f"统计判定为 {want}，撮合却没有平仓"
    assert str(closes[0].kind) == str(want), "出场原因不一致"
    assert closes[0].price == pytest.approx(stat.exit_price), "出场价不一致"
    assert closes[0].ts == stat.exit_ts


# ---------------------------------------------------------------- 交易台（端到端）


def _desk(**kw: object) -> tuple[object, object]:
    from sigdesk.trade.desk import DeskParams, TradeDesk
    params = DeskParams(
        initial_cash=100_000.0, enabled=bool(kw.get("enabled", True)),
        strategy=StrategyParams(mode="fixed", fixed_qty=1.0,
                                exits=OutcomeParams(horizon_bars=3, stop_pct=0.01,
                                                    target_pct=0.02)),
        risk=RiskParams(max_risk_per_trade=1.0, max_notional_per_trade=0.0,
                        max_symbol_exposure=10.0, max_total_exposure=10.0,
                        daily_loss_limit=0.0, max_orders_per_window=0),
        fills=FillParams(fee_bps=0.0, slippage_bps=0.0),
    )
    return TradeDesk([BTC_SYM, RB_SYM], params), params


def test_desk_is_off_by_default() -> None:
    """没人明确打开之前，盯盘就只是盯盘 —— 不该因为跑了个规则就悄悄开始"交易"。"""
    from sigdesk.trade.desk import DeskParams, TradeDesk
    desk = TradeDesk([BTC_SYM], DeskParams())
    assert desk.params.enabled is False
    assert desk.on_signals([sig()]) == []


def test_desk_pipeline_fills_on_the_next_bar() -> None:
    desk, _ = _desk()
    desk.on_bars([bar(600, 100.0, 100.1, 99.9, 100.0)])  # type: ignore[attr-defined]
    assert desk.on_signals([sig(ts=600, price=100.0)])  # type: ignore[attr-defined]
    assert not desk.broker.account.positions, "信号那一根不该成交"  # type: ignore[attr-defined]

    fills = desk.on_bars([bar(660, 100.0, 100.2, 99.9, 100.1)])  # type: ignore[attr-defined]
    assert [f.kind for f in fills] == [FillKind.ENTRY]
    assert BTC in desk.broker.account.positions  # type: ignore[attr-defined]


def test_desk_fills_map_one_to_one_with_signals() -> None:
    """**M4 验收**：成交记录与信号一一对应。

    每条被接受的信号恰好产生一次开仓，且开仓的 signal_key 就是那条信号的去重键；
    每笔开仓最终恰好对应一次平仓。
    """
    desk, _ = _desk()
    signals = []
    ts = 600
    prices = [100.0, 101.0, 103.5, 104.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0]
    for i, price in enumerate(prices):
        desk.on_bars([bar(ts, price, price + 0.6, price - 0.6, price)])  # type: ignore[attr-defined]
        if i in (0, 5):
            s = sig(price=price, key=f"k{i}", ts=ts)
            signals.append(s)
            desk.on_signals([s])  # type: ignore[attr-defined]
        ts += 60
    desk.on_bars([bar(ts, 104.0, 104.5, 103.5, 104.0)])  # type: ignore[attr-defined]

    fills = desk.broker.fills  # type: ignore[attr-defined]
    entries = [f for f in fills if f.kind is FillKind.ENTRY]
    exits = [f for f in fills if f.kind is not FillKind.ENTRY]

    assert [f.signal_key for f in entries] == [s.dedup_key for s in signals]
    assert len(exits) == len(entries), "每笔开仓都应恰好对应一次平仓"
    assert {f.signal_key for f in exits} == {s.dedup_key for s in signals}


def test_desk_rejects_unregistered_symbol() -> None:
    """ADR-0002：映射缺失即拒绝，绝不猜。"""
    desk, _ = _desk()
    assert desk.on_signals([sig(symbol="CRYPTO.OKX.DOGE.PERP", key="x")]) == []  # type: ignore[attr-defined]


def test_desk_summary_reports_account_state() -> None:
    desk, _ = _desk()
    desk.on_bars([bar(600, 100.0, 100.1, 99.9, 100.0)])  # type: ignore[attr-defined]
    desk.on_signals([sig(ts=600)])  # type: ignore[attr-defined]
    desk.on_bars([bar(660, 100.0, 100.2, 99.9, 100.1)])  # type: ignore[attr-defined]

    s = desk.summary()  # type: ignore[attr-defined]
    assert s["enabled"] is True
    assert s["positions"] and s["positions"][0]["symbol"] == BTC
    assert s["exposure"] > 0
    assert s["equity"] == pytest.approx(100_000.0 + s["unrealized"], rel=1e-9)


def test_desk_survives_restart_without_duplicating_orders() -> None:
    """**FR-7.3**：重启补喂同一条信号不得重复下单。"""
    from sigdesk.trade.desk import TradeDesk
    desk, params = _desk()
    desk.on_bars([bar(600, 100.0, 100.1, 99.9, 100.0)])  # type: ignore[attr-defined]
    desk.on_signals([sig(ts=600, key="dup")])  # type: ignore[attr-defined]
    desk.on_bars([bar(660, 100.0, 100.2, 99.9, 100.1)])  # type: ignore[attr-defined]
    snap = desk.snapshot()  # type: ignore[attr-defined]

    revived = TradeDesk([BTC_SYM, RB_SYM], params)  # type: ignore[arg-type]
    revived.restore(snap)
    assert BTC in revived.broker.account.positions, "持仓没恢复"
    assert revived.on_signals([sig(ts=600, key="dup")]) == [], "重启后重复下单了"

    fresh = TradeDesk([BTC_SYM, RB_SYM], params)  # type: ignore[arg-type]
    fresh.on_bars([bar(600, 100.0, 100.1, 99.9, 100.0)])
    assert fresh.on_signals([sig(ts=600, key="dup")]), "反证：不恢复就会下单"


def test_desk_snapshot_round_trips() -> None:
    from sigdesk.trade.desk import TradeDesk
    desk, params = _desk()
    desk.on_bars([bar(600, 100.0, 100.1, 99.9, 100.0)])  # type: ignore[attr-defined]
    desk.on_signals([sig(ts=600)])  # type: ignore[attr-defined]
    snap = desk.snapshot()  # type: ignore[attr-defined]

    revived = TradeDesk([BTC_SYM, RB_SYM], params)  # type: ignore[arg-type]
    revived.restore(snap)
    assert revived.snapshot() == snap
