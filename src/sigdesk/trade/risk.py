"""风控闸（RiskGate）。纯逻辑：给一条 Intent 与当前账户，回答"放不放行"。

FR-7.2 的五条规则各自独立、各有单测：单笔上限、单品种上限、日亏损熔断、最大持仓、频率限制。
外加幂等去重（FR-7.3）—— 重启补喂会重复产出同一条信号，交易侧必须挡住。

**全部时间取自 bar 的 close_ts，一处不读墙钟**：频率限制若按墙钟算，
回放与实盘就会拒不同的单，M2 好不容易挣来的逐条一致会在交易层丢掉。

检查顺序是有讲究的：先幂等（最便宜）→ 再熔断（一票否决，挡住后面全部）→
再频率 → 最后才算敞口。这样日志里出现的拒单原因永远是"最根本的那个"。
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from .model import Account, Intent, Rejection, RejectReason

SEEN_MEMORY = 512


@dataclass(frozen=True, slots=True)
class RiskParams:
    """全部上限都以**账户权益的比例**表示，绝对金额随权益自动缩放。0 表示不限。"""

    max_risk_per_trade: float = 0.01  # 单笔最大亏损 ≤ 权益 1%
    max_notional_per_trade: float = 0.0  # 单笔名义金额上限（比例）；0 = 不限
    max_symbol_exposure: float = 0.25  # 单品种持仓 ≤ 权益 25%
    max_total_exposure: float = 1.0  # 总持仓 ≤ 权益 100%（即不加杠杆）
    daily_loss_limit: float = 0.03  # 当日已实现亏损达权益 3% 即熔断
    max_orders_per_window: int = 10
    rate_window_s: int = 3600  # 频率窗口，按 bar 时间算

    def __post_init__(self) -> None:
        for name in ("max_risk_per_trade", "max_symbol_exposure", "max_total_exposure",
                     "daily_loss_limit"):
            v = getattr(self, name)
            if v < 0:
                raise ValueError(f"{name} 不能为负")
        if self.max_orders_per_window < 0 or self.rate_window_s <= 0:
            raise ValueError("频率限制参数无效")


@dataclass(slots=True)
class RiskGate:
    params: RiskParams = RiskParams()
    seen: OrderedDict[str, None] = field(default_factory=OrderedDict)
    recent: deque[int] = field(default_factory=deque)  # 近期放行的 bar close_ts
    rejections: list[Rejection] = field(default_factory=list)

    # ------------------------------------------------------------ 检查

    def check(
        self, intent: Intent, account: Account, marks: dict[str, float], trading_day: str
    ) -> Rejection | None:
        """放行返回 None，否则返回带原因的 Rejection。**不修改任何状态**。"""
        p = self.params
        equity = account.equity(marks)

        if intent.signal_key in self.seen:
            return self._no(intent, RejectReason.DUPLICATE, "同一信号已经下过单")
        if intent.qty <= 0:
            return self._no(intent, RejectReason.ZERO_QTY, "定量结果不足一手")
        if equity <= 0:
            return self._no(intent, RejectReason.NO_EQUITY, f"权益 {equity:.2f} 不足以开新仓")

        # 熔断：一票否决，放在敞口检查之前，日志里才会显示最根本的原因
        if p.daily_loss_limit > 0:
            lost = -min(0.0, account.daily_realized.get(trading_day, 0.0))
            cap = equity * p.daily_loss_limit
            if lost >= cap:
                return self._no(
                    intent, RejectReason.DAILY_LOSS,
                    f"{trading_day} 已实现亏损 {lost:.2f} ≥ 熔断线 {cap:.2f}",
                )

        if p.max_orders_per_window > 0:
            window_start = intent.created_at - p.rate_window_s
            recent = sum(1 for ts in self.recent if ts > window_start)
            if recent >= p.max_orders_per_window:
                return self._no(
                    intent, RejectReason.RATE_LIMIT,
                    f"{p.rate_window_s}s 内已下 {recent} 单，上限 {p.max_orders_per_window}",
                )

        if p.max_risk_per_trade > 0:
            cap = equity * p.max_risk_per_trade
            if intent.risk > cap + 1e-9:
                return self._no(
                    intent, RejectReason.PER_TRADE_RISK,
                    f"单笔风险 {intent.risk:.2f} > 上限 {cap:.2f}",
                )
        if p.max_notional_per_trade > 0:
            cap = equity * p.max_notional_per_trade
            if intent.notional > cap + 1e-9:
                return self._no(
                    intent, RejectReason.PER_TRADE_NOTIONAL,
                    f"单笔名义 {intent.notional:.2f} > 上限 {cap:.2f}",
                )

        if p.max_symbol_exposure > 0:
            held = account.positions.get(intent.symbol)
            cur = 0.0 if held is None else abs(held.qty) * marks.get(
                intent.symbol, held.entry_price) * held.multiplier
            cap = equity * p.max_symbol_exposure
            if cur + intent.notional > cap + 1e-9:
                return self._no(
                    intent, RejectReason.SYMBOL_EXPOSURE,
                    f"{intent.symbol} 持仓 {cur:.2f}+{intent.notional:.2f} > 上限 {cap:.2f}",
                )

        if p.max_total_exposure > 0:
            cap = equity * p.max_total_exposure
            total = account.exposure(marks)
            if total + intent.notional > cap + 1e-9:
                return self._no(
                    intent, RejectReason.TOTAL_EXPOSURE,
                    f"总持仓 {total:.2f}+{intent.notional:.2f} > 上限 {cap:.2f}",
                )
        return None

    # ------------------------------------------------------------ 登记

    def accept(self, intent: Intent) -> None:
        """放行之后登记。**只有真的下单了才调** —— 被拒的单不该占频率额度。"""
        self.seen[intent.signal_key] = None
        while len(self.seen) > SEEN_MEMORY:
            self.seen.popitem(last=False)
        self.recent.append(intent.created_at)
        cutoff = intent.created_at - self.params.rate_window_s
        while self.recent and self.recent[0] <= cutoff:
            self.recent.popleft()

    def _no(self, intent: Intent, reason: RejectReason, detail: str) -> Rejection:
        rej = Rejection(intent=intent, reason=reason, detail=detail)
        self.rejections.append(rej)
        if len(self.rejections) > SEEN_MEMORY:
            del self.rejections[: len(self.rejections) - SEEN_MEMORY]
        return rej

    # ------------------------------------------------------------ 快照

    def snapshot(self) -> dict[str, Any]:
        return {"seen": list(self.seen), "recent": list(self.recent)}

    def restore(self, row: dict[str, Any]) -> None:
        """重启恢复。**不恢复 seen 就会重复下单** —— 这是交易层的"不重报"。"""
        self.seen.clear()
        for key in row.get("seen") or []:
            self.seen[str(key)] = None
        self.recent.clear()
        self.recent.extend(int(t) for t in (row.get("recent") or []))


__all__ = ["SEEN_MEMORY", "RiskGate", "RiskParams"]
