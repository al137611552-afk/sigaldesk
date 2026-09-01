"""信号结果标注：一条信号发出之后，到底赚没赚。纯逻辑，无 IO、无当前时间。

这是 PRD 的第二个目标——"一条形态到底赚不赚钱，用历史数据跑一遍给出统计，而不是凭感觉"。
统计口径见 **ADR-0008**，概括成四条最容易把结论做假的地方：

1. **入场价默认取信号次根 bar 的开盘价**，不是信号那根的收盘价。
   信号在 bar 收盘时才成立，那个收盘价已经过去了，成交不到 —— 用它统计会系统性偏乐观。
2. **同一根 bar 同时触及止损与止盈时，一律记止损**。bar 数据给不出先后，
   取止盈就是在给自己发奖，取止损才是保守的下界。
3. **成本显式传入**（单边基点），默认 0 —— 但那时得到的是**毛收益**，报告里会标明。
4. **方向为 neutral 的信号不参与胜率**：它是"去看一眼"的提示，不是方向判断。
   这类信号只统计触发次数与后续波动幅度。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from ..core.models import Bar
from ..rules.model import Direction, Signal

BPS: Final = 1e-4


class ExitReason(StrEnum):
    STOP = "stop"  # 触及止损
    TARGET = "target"  # 触及止盈
    HORIZON = "horizon"  # 持有到期
    NO_DATA = "no_data"  # 信号之后没有足够的 bar，无法评价


@dataclass(frozen=True, slots=True)
class OutcomeParams:
    """统计口径。改这里等于改结论，所以每个字段都写清楚含义。"""

    horizon_bars: int = 20  # 固定持有期：最多持有多少根扳机周期 bar
    stop_pct: float = 0.005  # 止损距离（占入场价的比例）
    target_pct: float = 0.010  # 止盈距离
    cost_bps: float = 0.0  # **单边**成本（手续费+滑点，基点）；0 = 毛收益
    entry_on_next_open: bool = True  # False = 用信号那根的收盘价入场（偏乐观）
    # 若设了 atr_key 且信号 context 里有该值，就用 ATR 倍数替代百分比 ——
    # 期货各品种波动率差异极大，固定百分比会让活跃品种全打止损、呆滞品种永不触发。
    atr_key: str | None = None
    stop_atr: float = 1.5
    target_atr: float = 3.0

    def __post_init__(self) -> None:
        if self.horizon_bars < 1:
            raise ValueError("horizon_bars 必须 >= 1")
        if self.stop_pct <= 0 or self.target_pct <= 0:
            raise ValueError("止损/止盈距离必须为正")


@dataclass(frozen=True, slots=True)
class Outcome:
    """一条信号的评价结果。方向已归一化：``ret > 0`` 恒表示"这条信号是对的"。"""

    rule_id: str
    symbol: str
    direction: Direction
    fired_at: int
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    reason: ExitReason
    ret: float  # 净收益率（已扣成本、已按方向取号）
    gross_ret: float  # 毛收益率
    mfe: float  # 最大有利偏移（持有期内最好的浮盈比例）
    mae: float  # 最大不利偏移（持有期内最差的浮亏比例，<= 0）
    bars_held: int

    @property
    def is_win(self) -> bool:
        return self.ret > 0

    @property
    def evaluated(self) -> bool:
        return self.reason is not ExitReason.NO_DATA

    def as_dict(self) -> dict[str, Any]:
        """面板 /api/stats 与规则试算共用同一份序列化 —— 两处分家就会出现
        "统计页和试算页字段对不上"这种查半天的问题。"""
        return {
            "rule_id": self.rule_id,
            "symbol": self.symbol,
            "direction": str(self.direction),
            "fired_at": self.fired_at,
            "entry_ts": self.entry_ts,
            "entry_price": self.entry_price,
            "exit_ts": self.exit_ts,
            "exit_price": self.exit_price,
            "reason": str(self.reason),
            "ret": self.ret,
            "gross_ret": self.gross_ret,
            "mfe": self.mfe,
            "mae": self.mae,
            "bars_held": self.bars_held,
        }


def _sign(direction: Direction) -> float:
    return -1.0 if direction is Direction.SHORT else 1.0


def risk_distances(
    context: Mapping[str, float | None], entry: float, params: OutcomeParams
) -> tuple[float, float]:
    """(止损距离, 止盈距离)，都是正的绝对价格距离。

    **纸上撮合（trade/）也调这个函数**，不是各写一份。口径一旦分家，就会出现
    "统计说赚钱、模拟盘说不赚"而无从排查的局面 —— 那正是 ADR-0001 要消灭的东西。
    """
    if params.atr_key:
        atr = context.get(params.atr_key)
        if atr is not None and atr > 0:
            return params.stop_atr * atr, params.target_atr * atr
    return entry * params.stop_pct, entry * params.target_pct


def evaluate(signal: Signal, future: list[Bar], params: OutcomeParams | None = None) -> Outcome:
    """评价一条信号。

    ``future`` 是**信号那根之后**、同一标的同一周期、按时间升序的 bar。
    调用方负责截取 —— 这样这个函数不需要认识 BarStore，可以纯粹地单测。
    """
    p = params or OutcomeParams()
    if not future:
        return _no_data(signal)

    entry_bar = future[0]
    entry = entry_bar.open if p.entry_on_next_open else signal.trigger_price
    if entry <= 0:
        return _no_data(signal)

    sign = _sign(signal.direction)
    stop_d, target_d = risk_distances(signal.context, entry, p)
    stop = entry - sign * stop_d
    target = entry + sign * target_d

    best = worst = 0.0
    for i, bar in enumerate(future[: p.horizon_bars], start=1):
        # 方向已归一化：多头时 up 来自 high、空头时来自 low，
        # 所以 max/min 对两个方向都是"最有利/最不利"。
        up = (bar.high - entry) / entry * sign
        down = (bar.low - entry) / entry * sign
        best = max(best, up, down)
        worst = min(worst, up, down)
        hit_stop = bar.low <= stop if sign > 0 else bar.high >= stop
        hit_target = bar.high >= target if sign > 0 else bar.low <= target
        if hit_stop:  # 同一根同时触及时保守取止损（bar 数据给不出先后）
            return _make(signal, entry_bar, entry, bar, stop, ExitReason.STOP, best, worst, i, p)
        if hit_target:
            return _make(
                signal, entry_bar, entry, bar, target, ExitReason.TARGET, best, worst, i, p
            )

    held = future[: p.horizon_bars]
    if not held:
        return _no_data(signal)
    last = held[-1]
    return _make(
        signal, entry_bar, entry, last, last.close, ExitReason.HORIZON, best, worst, len(held), p
    )


def _make(
    signal: Signal,
    entry_bar: Bar,
    entry: float,
    exit_bar: Bar,
    exit_price: float,
    reason: ExitReason,
    mfe: float,
    mae: float,
    bars_held: int,
    p: OutcomeParams,
) -> Outcome:
    sign = _sign(signal.direction)
    gross = (exit_price - entry) / entry * sign
    net = gross - 2.0 * p.cost_bps * BPS  # 一进一出，双边成本
    return Outcome(
        rule_id=signal.rule_id,
        symbol=signal.symbol,
        direction=signal.direction,
        fired_at=signal.fired_at,
        entry_ts=entry_bar.close_ts,
        entry_price=entry,
        exit_ts=exit_bar.close_ts,
        exit_price=exit_price,
        reason=reason,
        ret=net,
        gross_ret=gross,
        mfe=mfe,
        mae=mae,
        bars_held=bars_held,
    )


def _no_data(signal: Signal) -> Outcome:
    return Outcome(
        rule_id=signal.rule_id,
        symbol=signal.symbol,
        direction=signal.direction,
        fired_at=signal.fired_at,
        entry_ts=0,
        entry_price=0.0,
        exit_ts=0,
        exit_price=0.0,
        reason=ExitReason.NO_DATA,
        ret=0.0,
        gross_ret=0.0,
        mfe=0.0,
        mae=0.0,
        bars_held=0,
    )


def evaluate_all(
    signals: list[Signal], bars_by_symbol: dict[str, list[Bar]],
    params: OutcomeParams | None = None,
) -> list[Outcome]:
    """批量评价。``bars_by_symbol`` 是各标的在**扳机周期**上的完整序列（升序）。

    对每条信号取 ``close_ts > fired_at`` 的部分作为未来 —— 物理截断，
    与 INV-1 同一个道理：评价用的数据必须完全落在信号之后。
    """
    out: list[Outcome] = []
    for signal in signals:
        series = bars_by_symbol.get(signal.symbol, [])
        future = [b for b in series if b.close_ts > signal.fired_at]
        out.append(evaluate(signal, future, params))
    return out


__all__ = [
    "BPS",
    "ExitReason",
    "Outcome",
    "OutcomeParams",
    "evaluate",
    "evaluate_all",
    "risk_distances",
]
