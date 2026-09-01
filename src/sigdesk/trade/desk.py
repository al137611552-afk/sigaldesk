"""交易台：把 Signal → Intent → RiskGate → PaperBroker 串成一条链路。纯逻辑，不读墙钟。

每根 bar 的处理顺序是**有讲究的**，反了就会偷看未来：

1. ``on_bars(bars)`` —— 先用这根 bar 撮合：已有挂单按开盘价成交、已有持仓判出场。
2. 规则引擎在同一批 bar 上产出信号。
3. ``on_signals(signals)`` —— 新信号变成意图挂上，等**下一根** bar 才成交。

若把第 3 步放到第 1 步之前，新意图就会用当前这根的开盘价成交 ——
而这根 bar 收盘时信号才刚成立，那是拿收盘后的信息去吃开盘价。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.models import Bar, Symbol
from ..rules.model import Signal
from .model import Account, Fill, Intent, Rejection
from .paper import FillParams, PaperBroker
from .risk import RiskGate, RiskParams
from .strategy import StrategyParams, make_intent


@dataclass(slots=True)
class DeskParams:
    initial_cash: float = 100_000.0
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskParams = field(default_factory=RiskParams)
    fills: FillParams = field(default_factory=FillParams)
    enabled: bool = False  # 默认**关闭**：没人明确打开之前，盯盘就只是盯盘


class TradeDesk:
    """纸上交易台。一个进程一个实例，与规则引擎并列挂在 bar 流上。"""

    def __init__(self, symbols: Sequence[Symbol], params: DeskParams | None = None) -> None:
        self.params = params or DeskParams()
        self.symbols = {s.uid: s for s in symbols}
        self.broker = PaperBroker(
            account=Account(cash=self.params.initial_cash), params=self.params.fills
        )
        self.gate = RiskGate(self.params.risk)
        self.marks: dict[str, float] = {}
        self.trading_day: str = ""
        self.intents: list[Intent] = []
        self.rejections: list[Rejection] = []

    # ------------------------------------------------------------ 驱动

    def on_bars(self, bars: Sequence[Bar]) -> list[Fill]:
        """撮合。只认 1m 之外的任何周期都可以 —— 用哪个周期撮合由调用方决定，
        但必须与规则的扳机周期一致，否则出场判定的粒度和信号对不上。"""
        out: list[Fill] = []
        for bar in bars:
            if not bar.closed:
                continue
            self.marks[bar.symbol] = bar.close
            self.trading_day = bar.trading_day or _utc_day(bar.close_ts)
            out.extend(self.broker.on_bar(bar))
        return out

    def on_signals(self, signals: Sequence[Signal]) -> list[Intent]:
        """信号 → 意图 → 风控 → 挂单。返回**真正挂上去**的意图。"""
        if not self.params.enabled:
            return []
        accepted: list[Intent] = []
        for signal in signals:
            symbol = self.symbols.get(signal.symbol)
            if symbol is None:
                continue  # 未注册的标的不交易（ADR-0002：映射缺失即拒绝）
            equity = self.equity()
            headroom = self._notional_headroom(signal.symbol, equity)
            if headroom <= 0:
                continue  # 额度已满，连意图都不必造
            intent = make_intent(
                signal, symbol, equity, self.params.strategy,
                max_notional=headroom,
                max_risk=equity * self.params.risk.max_risk_per_trade,
            )
            if intent is None:
                continue
            rejection = self.gate.check(intent, self.broker.account, self.marks, self.trading_day)
            if rejection is not None:
                self.rejections.append(rejection)
                continue
            if not self.broker.submit(intent):
                continue  # 已有持仓/挂单
            self.gate.accept(intent)
            self.intents.append(intent)
            accepted.append(intent)
        return accepted

    def _notional_headroom(self, symbol: str, equity: float) -> float:
        """这一笔最多还能开多大名义金额。

        取三个硬上限里**最紧的那个**：单笔名义、该品种的剩余额度、总敞口的剩余额度。
        定量层拿到它就能先截一刀，风控闸因此只在异常时才响 —— 它天天响就说明这里算漏了。
        真跑教训：只传单笔上限时，单品种上限（25%）比单笔上限（30%）还紧，
        于是 44 条信号又被换个理由全拒掉。
        """
        risk = self.params.risk
        acc = self.broker.account
        caps: list[float] = []
        if risk.max_notional_per_trade > 0:
            caps.append(equity * risk.max_notional_per_trade)
        if risk.max_symbol_exposure > 0:
            held = acc.positions.get(symbol)
            used = 0.0 if held is None else abs(held.qty) * self.marks.get(
                symbol, held.entry_price) * held.multiplier
            caps.append(equity * risk.max_symbol_exposure - used)
        if risk.max_total_exposure > 0:
            caps.append(equity * risk.max_total_exposure - acc.exposure(self.marks))
        return min(caps) if caps else 0.0

    # ------------------------------------------------------------ 账户

    def equity(self) -> float:
        return self.broker.account.equity(self.marks)

    def summary(self) -> dict[str, Any]:
        acc = self.broker.account
        equity = self.equity()
        return {
            "enabled": self.params.enabled,
            "cash": acc.cash,
            "equity": equity,
            "initial_cash": self.params.initial_cash,
            "return_pct": (equity / self.params.initial_cash - 1.0)
            if self.params.initial_cash else 0.0,
            "realized": acc.realized,
            "fees": acc.fees,
            "unrealized": equity - acc.cash,
            "exposure": acc.exposure(self.marks),
            "positions": [
                {**p.as_row(), "mark": self.marks.get(s), "unrealized":
                    p.unrealized(self.marks[s]) if s in self.marks else None}
                for s, p in sorted(acc.positions.items())
            ],
            "pending": [i.as_dict() for i in self.broker.pending.values()],
            "daily_realized": dict(sorted(acc.daily_realized.items())),
            "trading_day": self.trading_day,
            "fills": len(self.broker.fills),
            "rejections": [r.as_dict() for r in self.rejections[-20:]],
        }

    @staticmethod
    def summary_from_snapshot(row: dict[str, Any]) -> dict[str, Any] | None:
        """从**落盘的**快照复原一份账户概览，供独立只读模式展示。

        复盘时正是要看账户 —— 只因为引擎没在跑就把权益、收益率全部留白，
        等于把已经存下来的东西藏起来。标记价用快照里的 marks（最后一次运行时的收盘价）。
        """
        if not row:
            return None
        acc = Account.from_row(row.get("account") or {})
        marks = {str(k): float(v) for k, v in (row.get("marks") or {}).items()}
        initial = float(row.get("initial_cash") or 0.0)
        equity = acc.equity(marks)
        return {
            "enabled": False, "stale": True,  # 这是快照，不是当下
            "cash": acc.cash, "equity": equity,
            # 旧快照可能没存初始资金。算不出就是 None，**不能显示成 0%** ——
            # 那会被当成"不赚不赔"读，而真实情况可能是亏了 1%（ADR-0006 的老道理）。
            "initial_cash": initial or None,
            "return_pct": (equity / initial - 1.0) if initial else None,
            "realized": acc.realized, "fees": acc.fees, "unrealized": equity - acc.cash,
            "exposure": acc.exposure(marks),
            "positions": [
                {**p.as_row(), "mark": marks.get(s),
                 "unrealized": p.unrealized(marks[s]) if s in marks else None}
                for s, p in sorted(acc.positions.items())
            ],
            "pending": list(row.get("pending") or []),
            "daily_realized": dict(sorted((row.get("account") or {})
                                          .get("daily_realized", {}).items())),
            "trading_day": str(row.get("trading_day") or ""),
            "fills": 0, "rejections": [],
        }

    # ------------------------------------------------------------ 快照

    def snapshot(self) -> dict[str, Any]:
        return {
            "initial_cash": self.params.initial_cash,  # 只读模式要靠它算收益率
            "account": self.broker.account.as_row(),
            "pending": [i.as_dict() for i in self.broker.pending.values()],
            "gate": self.gate.snapshot(),
            "marks": dict(self.marks),
            "trading_day": self.trading_day,
        }

    def restore(self, row: dict[str, Any]) -> None:
        """重启恢复。**不恢复 gate.seen 就会重复下单**（FR-7.3）。"""
        if not row:
            return
        self.params.initial_cash = float(row.get("initial_cash") or self.params.initial_cash)
        self.broker.account = Account.from_row(row.get("account") or {})
        self.broker.pending.clear()
        for raw in row.get("pending") or []:
            intent = _intent_from_row(raw)
            self.broker.pending[intent.symbol] = intent
        self.gate.restore(row.get("gate") or {})
        self.marks = {str(k): float(v) for k, v in (row.get("marks") or {}).items()}
        self.trading_day = str(row.get("trading_day") or "")


def _intent_from_row(row: dict[str, Any]) -> Intent:
    from ..core.models import Timeframe
    from .model import Side

    return Intent(
        signal_key=str(row["signal_key"]), rule_id=str(row["rule_id"]),
        symbol=str(row["symbol"]), side=Side(row["side"]), qty=float(row["qty"]),
        created_at=int(row["created_at"]), ref_price=float(row["ref_price"]),
        multiplier=float(row["multiplier"]),
        stop=None if row.get("stop") is None else float(row["stop"]),
        target=None if row.get("target") is None else float(row["target"]),
        horizon_bars=int(row.get("horizon_bars") or 0), sizing=str(row.get("sizing") or ""),
        timeframe=Timeframe(row.get("timeframe", "1m")),
    )


def _utc_day(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).date().isoformat()


__all__ = ["DeskParams", "TradeDesk"]
