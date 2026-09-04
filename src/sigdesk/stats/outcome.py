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

import bisect
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
    #
    # **默认就是 ATR，而且这里是全仓唯一的默认值来源。** 曾经不是：面板
    # /api/stats 和 /api/trial 都没传 atr_key（=百分比），而纸上撮合、
    # rule_eval 传了 atr14（=ATR），于是"面板上的胜率"和"模拟盘的胜率"
    # 算的根本不是同一件事 —— 正是 risk_distances 注释里说要消灭的那种分家，
    # 只不过分家的是**参数**而不是函数，共用函数一点都挡不住。
    # 改默认值之外，各调用点也一律不再自己写字面量（见 trade/loader.py、
    # scripts/rule_eval.py、web/api.py）。
    atr_key: str | None = "atr14"
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
    exit_basis: str = "pct"  # 这一条实际用的止损口径："atr" 或 "pct"（见 exit_basis()）

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
            "exit_basis": self.exit_basis,
        }


def _sign(direction: Direction) -> float:
    return -1.0 if direction is Direction.SHORT else 1.0


def exit_basis(context: Mapping[str, float | None], params: OutcomeParams) -> str:
    """这一条实际按哪套口径算止损止盈：``"atr"`` 或 ``"pct"``。

    **必须能被报出来。** ``atr_key`` 设了、但信号快照里没有那个值（规则的
    ``context:`` 没声明 atr14，或者预热期还是 None）时会**静默回落**到百分比：
    一批信号里混着两套口径，报告上看不出任何异常，横向比较却已经不成立。
    所以每条 Outcome 都记下自己用的是哪套，报告里给出混用计数。
    """
    if params.atr_key:
        atr = context.get(params.atr_key)
        if atr is not None and atr > 0:
            return "atr"
    return "pct"


def risk_distances(
    context: Mapping[str, float | None], entry: float, params: OutcomeParams
) -> tuple[float, float]:
    """(止损距离, 止盈距离)，都是正的绝对价格距离。

    **纸上撮合（trade/）也调这个函数**，不是各写一份。口径一旦分家，就会出现
    "统计说赚钱、模拟盘说不赚"而无从排查的局面 —— 那正是 ADR-0001 要消灭的东西。
    """
    if exit_basis(context, params) == "atr":
        atr = context[params.atr_key]  # type: ignore[index]  # exit_basis 已保证非空
        assert atr is not None
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
    basis = exit_basis(signal.context, p)
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
            return _make(signal, entry_bar, entry, bar, stop, ExitReason.STOP,
                         best, worst, i, p, basis)
        if hit_target:
            return _make(
                signal, entry_bar, entry, bar, target, ExitReason.TARGET,
                best, worst, i, p, basis
            )

    held = future[: p.horizon_bars]
    if not held:
        return _no_data(signal)
    last = held[-1]
    return _make(
        signal, entry_bar, entry, last, last.close, ExitReason.HORIZON,
        best, worst, len(held), p, basis
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
    basis: str,
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
        exit_basis=basis,
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
    p = params or OutcomeParams()
    # **二分定位，别每条信号都重扫整条序列。** 原来是
    # `[b for b in series if b.close_ts > fired_at]`，O(信号数 x bar 数) ——
    # 3 万根 1m 上算随机进场基准（3000 个抽样信号）要 5.8 秒。
    # `evaluate` 只用得到 `future[:horizon_bars]`，所以切一小段就够；
    # 多切两根是给"次根开盘入场"留的余量。
    keys: dict[str, list[int]] = {}
    out: list[Outcome] = []
    for signal in signals:
        series = bars_by_symbol.get(signal.symbol, [])
        if not series:
            out.append(evaluate(signal, [], params))
            continue
        ks = keys.get(signal.symbol)
        if ks is None:
            ks = keys[signal.symbol] = [b.close_ts for b in series]
        i = bisect.bisect_right(ks, signal.fired_at)
        out.append(evaluate(signal, list(series[i : i + p.horizon_bars + 2]), params))
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
