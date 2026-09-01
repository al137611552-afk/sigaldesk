"""Signal → Intent：把一条信号翻译成"下多少手"。纯逻辑，无 IO、无当前时间。

**仓位定量的口径与 ADR-0008 的统计口径共用同一个 `risk_distances`**，不是各写一份。
分家的后果是"统计说赚钱、模拟盘说不赚"而无从排查 —— 那正是 ADR-0001 要消灭的东西。

三种定量方式：
- ``risk``（默认）：按账户权益的固定风险比例反推手数。止损远则手数少，止损近则手数多，
  每笔的最大亏损大致相等。期货各品种波动率差异极大，固定手数会让活跃品种的单笔风险失控。
- ``fixed``：固定手数。简单，但单笔风险随品种与波动率乱飘。
- ``notional``：固定名义金额。加密常用。

**neutral 方向不产生 Intent** —— 它是"去看一眼"的提示，不是方向判断（与统计口径一致）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from ..core.models import Symbol
from ..rules.model import Signal
from ..stats.outcome import OutcomeParams, risk_distances
from .model import Intent, Side

SizingMode = Literal["risk", "fixed", "notional"]


@dataclass(frozen=True, slots=True)
class StrategyParams:
    """把信号变成手数的全部旋钮。改这里等于改风险敞口，所以每个字段都写清楚。"""

    mode: SizingMode = "risk"
    risk_per_trade: float = 0.005  # 账户权益的 0.5%
    fixed_qty: float = 1.0
    notional_per_trade: float = 1000.0
    # 止损止盈口径：直接复用统计侧的定义，保证两边算出来的是同一个止损位
    exits: OutcomeParams = OutcomeParams()
    # 数量粒度：期货是 1（整手），加密按交易所的 lotSz。0 表示不取整。
    default_lot: float = 0.0

    def __post_init__(self) -> None:
        if self.risk_per_trade <= 0 or self.risk_per_trade > 0.5:
            raise ValueError("risk_per_trade 应在 (0, 0.5] 之间；超过一半权益不是风控是赌博")
        if self.fixed_qty <= 0 or self.notional_per_trade <= 0:
            raise ValueError("fixed_qty 与 notional_per_trade 必须为正")


def _round_lot(qty: float, lot: float) -> float:
    """向下取整到最小变动单位。**必须向下** —— 向上取整会让单笔风险超出预算。"""
    if lot <= 0:
        return qty
    return math.floor(qty / lot + 1e-9) * lot


def lot_of(symbol: Symbol, params: StrategyParams) -> float:
    """该标的的数量粒度。期货按手（1），加密用 Symbol.multiplier（= ctVal）。"""
    if params.default_lot > 0:
        return params.default_lot
    return 1.0 if symbol.market.value == "CN" else symbol.multiplier


def make_intent(
    signal: Signal, symbol: Symbol, equity: float, params: StrategyParams | None = None,
    *, max_notional: float = 0.0, max_risk: float = 0.0,
) -> Intent | None:
    """把信号翻译成下单意图。返回 None 表示"这条信号不该产生交易"。

    入场参考价用 ``signal.trigger_price``，但**它不是成交价** ——
    成交价由 Broker 在下一根 bar 的开盘决定（ADR-0010），与统计口径一致。

    ``max_notional`` / ``max_risk`` 是**绝对金额**上限，定量时就截一刀（取小者），
    而不是算完再交给风控去拒。真跑发现的教训：止损很近时（BTC 的 ATR 止损约 0.1%），
    要吃满 0.5% 的风险预算需要 5 倍名义杠杆，必然撞穿名义上限 ——
    结果是 44 条信号一条都没成交。**风控闸应该是兜底，不该是日常拦路虎**：
    它天天拦，说明定量层没做好。
    """
    p = params or StrategyParams()
    side = Side.of(signal.direction)
    if side is None:
        return None
    price = signal.trigger_price
    if price <= 0 or equity <= 0:
        return None

    stop_d, target_d = risk_distances(signal.context, price, p.exits)
    stop = price - side.sign * stop_d
    target = price + side.sign * target_d
    mult = symbol.multiplier or 1.0

    if p.mode == "risk":
        risk_budget = equity * p.risk_per_trade
        per_unit = stop_d * mult
        raw = risk_budget / per_unit if per_unit > 0 else 0.0
        sizing = f"risk {p.risk_per_trade:.2%}×{equity:.0f} ÷ ({stop_d:.6g}×{mult:g})"
    elif p.mode == "fixed":
        raw = p.fixed_qty
        sizing = f"fixed {p.fixed_qty:g}"
    else:
        raw = p.notional_per_trade / (price * mult)
        sizing = f"notional {p.notional_per_trade:.0f} ÷ ({price:.6g}×{mult:g})"

    capped_by = ""
    if max_notional > 0:
        by_notional = max_notional / (price * mult)
        if by_notional < raw:
            raw, capped_by = by_notional, "名义上限"
    if max_risk > 0 and stop_d > 0:
        by_risk = max_risk / (stop_d * mult)
        if by_risk < raw:
            raw, capped_by = by_risk, "风险上限"
    if capped_by:
        sizing += f" → 受{capped_by}截断"

    qty = _round_lot(raw, lot_of(symbol, p))
    if qty <= 0:
        return None  # 不足一手：与其凑整放大风险，不如不做

    return Intent(
        signal_key=signal.dedup_key, rule_id=signal.rule_id, symbol=signal.symbol,
        side=side, qty=qty, created_at=signal.fired_at, ref_price=price, multiplier=mult,
        stop=stop, target=target, horizon_bars=p.exits.horizon_bars,
        sizing=sizing, timeframe=signal.timeframe,
    )


__all__ = ["SizingMode", "StrategyParams", "lot_of", "make_intent"]
