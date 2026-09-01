"""交易层的数据模型。纯值对象，无 IO。

链路是 ``Signal → Intent → RiskGate → Broker``（ARCHITECTURE §8）。
Intent 是"想做什么"，不是"已经做了什么" —— 它可能被风控拒掉，也可能因为没有对手价而作废。

**幂等锚是 ``signal_key``**（取自 ``Signal.dedup_key``）：重启补喂会重复产出同一条信号，
交易侧必须靠它去重，否则一次重启就是一次重复下单（FR-7.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.models import Timeframe
from ..rules.model import Direction


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> float:
        return 1.0 if self is Side.BUY else -1.0

    @staticmethod
    def of(direction: Direction) -> Side | None:
        """方向到买卖。``neutral`` 不产生交易 —— 它是"去看一眼"的提示，不是方向判断
        （与 ADR-0008 的统计口径一致：neutral 也不进胜率）。"""
        if direction is Direction.LONG:
            return Side.BUY
        if direction is Direction.SHORT:
            return Side.SELL
        return None


class RejectReason(StrEnum):
    """风控拒单原因。每一条都有针对性单测（M4 验收）。"""

    DUPLICATE = "duplicate"  # 同一信号已经下过单（幂等）
    NO_DIRECTION = "no_direction"  # neutral 信号
    ZERO_QTY = "zero_qty"  # 定量算下来不足一手
    PER_TRADE_RISK = "per_trade_risk"  # 单笔风险超限
    PER_TRADE_NOTIONAL = "per_trade_notional"  # 单笔名义金额超限
    SYMBOL_EXPOSURE = "symbol_exposure"  # 单品种持仓超限
    TOTAL_EXPOSURE = "total_exposure"  # 总持仓超限
    DAILY_LOSS = "daily_loss"  # 日亏损熔断
    RATE_LIMIT = "rate_limit"  # 下单频率超限
    NO_EQUITY = "no_equity"  # 权益不足


@dataclass(frozen=True, slots=True)
class Intent:
    """一条下单意图。数量为正；方向在 ``side`` 上。"""

    signal_key: str  # = Signal.dedup_key，幂等锚
    rule_id: str
    symbol: str
    side: Side
    qty: float  # 期货为手数（整数），加密为币量（可小数）
    created_at: int  # 触发 bar 的 close_ts，**不是墙钟**
    ref_price: float  # 信号触发价，仅供参考；成交价由 Broker 决定
    multiplier: float  # 合约乘数 / ctVal
    stop: float | None = None
    target: float | None = None
    horizon_bars: int = 0
    sizing: str = ""  # 这个量是怎么算出来的，便于复盘
    timeframe: Timeframe = Timeframe.M1

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.ref_price * self.multiplier

    @property
    def risk(self) -> float:
        """按止损距离算的最大亏损。没设止损时为 0 —— 那意味着风险不可度量。"""
        if self.stop is None:
            return 0.0
        return abs(self.ref_price - self.stop) * abs(self.qty) * self.multiplier

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_key": self.signal_key, "rule_id": self.rule_id, "symbol": self.symbol,
            "side": str(self.side), "qty": self.qty, "created_at": self.created_at,
            "ref_price": self.ref_price, "multiplier": self.multiplier,
            "stop": self.stop, "target": self.target, "horizon_bars": self.horizon_bars,
            "sizing": self.sizing, "notional": self.notional, "risk": self.risk,
        }


@dataclass(frozen=True, slots=True)
class Rejection:
    intent: Intent
    reason: RejectReason
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {**self.intent.as_dict(), "reason": str(self.reason), "detail": self.detail}


class FillKind(StrEnum):
    ENTRY = "entry"
    STOP = "stop"
    TARGET = "target"
    HORIZON = "horizon"
    FORCED = "forced"  # 收盘/停机强平


@dataclass(frozen=True, slots=True)
class Fill:
    """一笔成交。``price`` 已含滑点。"""

    signal_key: str
    symbol: str
    side: Side
    qty: float
    price: float
    ts: int  # 成交 bar 的 close_ts
    kind: FillKind
    fee: float = 0.0
    realized: float = 0.0  # 平仓时的已实现盈亏（含费用），开仓为 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal_key": self.signal_key, "symbol": self.symbol, "side": str(self.side),
            "qty": self.qty, "price": self.price, "ts": self.ts, "kind": str(self.kind),
            "fee": self.fee, "realized": self.realized,
        }


@dataclass(slots=True)
class Position:
    """一个标的的持仓。本项目一个标的同时只持有一个方向的一笔仓位 ——
    加仓/对锁属于策略复杂度，M4 不做（做了会让"成交与信号一一对应"这条验收失去意义）。"""

    symbol: str
    side: Side
    qty: float
    entry_price: float
    multiplier: float
    opened_at: int
    signal_key: str
    stop: float | None = None
    target: float | None = None
    horizon_left: int = 0

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.entry_price * self.multiplier

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) * self.side.sign * abs(self.qty) * self.multiplier

    def as_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "side": str(self.side), "qty": self.qty,
            "entry_price": self.entry_price, "multiplier": self.multiplier,
            "opened_at": self.opened_at, "signal_key": self.signal_key,
            "stop": self.stop, "target": self.target, "horizon_left": self.horizon_left,
        }

    @staticmethod
    def from_row(row: dict[str, Any]) -> Position:
        return Position(
            symbol=str(row["symbol"]), side=Side(row["side"]), qty=float(row["qty"]),
            entry_price=float(row["entry_price"]), multiplier=float(row["multiplier"]),
            opened_at=int(row["opened_at"]), signal_key=str(row["signal_key"]),
            stop=None if row.get("stop") is None else float(row["stop"]),
            target=None if row.get("target") is None else float(row["target"]),
            horizon_left=int(row.get("horizon_left") or 0),
        )


@dataclass(slots=True)
class Account:
    """纸上账户。权益 = 现金 + 浮动盈亏；现金只在平仓时变动。"""

    cash: float
    realized: float = 0.0
    fees: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    # 按交易日累计的已实现盈亏，日亏熔断读它
    daily_realized: dict[str, float] = field(default_factory=dict)

    def equity(self, marks: dict[str, float]) -> float:
        floating = sum(
            p.unrealized(marks[s]) for s, p in self.positions.items() if s in marks
        )
        return self.cash + floating

    def exposure(self, marks: dict[str, float]) -> float:
        return sum(
            abs(p.qty) * marks.get(s, p.entry_price) * p.multiplier
            for s, p in self.positions.items()
        )

    def as_row(self) -> dict[str, Any]:
        return {
            "cash": self.cash, "realized": self.realized, "fees": self.fees,
            "positions": [p.as_row() for p in self.positions.values()],
            "daily_realized": dict(self.daily_realized),
        }

    @staticmethod
    def from_row(row: dict[str, Any]) -> Account:
        acc = Account(cash=float(row.get("cash", 0.0)), realized=float(row.get("realized", 0.0)),
                      fees=float(row.get("fees", 0.0)))
        for p in row.get("positions") or []:
            pos = Position.from_row(p)
            acc.positions[pos.symbol] = pos
        acc.daily_realized = {
            str(k): float(v) for k, v in (row.get("daily_realized") or {}).items()
        }
        return acc


__all__ = [
    "Account", "Fill", "FillKind", "Intent", "Position", "RejectReason", "Rejection", "Side",
]
