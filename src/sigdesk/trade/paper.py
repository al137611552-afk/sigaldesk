"""纸上撮合（PaperBroker）。纯逻辑：喂 bar，吐成交。无 IO、无当前时间。

## 撮合口径（ADR-0010），与 ADR-0008 的统计口径**刻意保持一致**

1. **意图挂着，下一根 bar 用开盘价成交**。信号在 bar 收盘时才成立，那个收盘价已经过去、
   成交不到。用它成交会系统性偏乐观 —— 统计侧早就为此把入场价定成次根开盘，
   撮合这边如果各搞一套，"统计说赚钱、模拟盘说不赚"就无从排查。
2. **同一根 bar 同时触及止损与止盈时，一律记止损**。bar 数据（OHLC）给不出高低点先后，
   取止盈就是在给自己发奖。
3. **滑点按方向恶化**：买入成交价上浮、卖出下浮。给自己算好价的滑点等于没有滑点。
4. **费用双边**：开仓一次、平仓一次。

## 与真实撮合的差距（不要拿它当实盘预期）

- 没有盘口，不看深度：默认你要多少有多少。大单在真实市场会吃穿几档。
- bar 内价格路径不可见，止损滑点无从体现 —— 触及止损就按止损价成交，实盘往往更差。
- 不模拟拒单、断线、部分成交。那些属于 M5 的真实网关。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core.models import Bar
from .model import Account, Fill, FillKind, Intent, Position, Side

BPS = 1e-4


@dataclass(frozen=True, slots=True)
class FillParams:
    fee_bps: float = 2.0  # **单边**手续费（基点），一进一出各收一次
    slippage_bps: float = 1.0  # 单边滑点，按方向恶化
    close_on_horizon: bool = True  # 持有期满是否强制平仓


def _slip(price: float, side: Side, params: FillParams) -> float:
    """滑点永远往不利方向走：买贵一点、卖便宜一点。"""
    return price * (1.0 + side.sign * params.slippage_bps * BPS)


def _fee(price: float, qty: float, mult: float, params: FillParams) -> float:
    return abs(qty) * price * mult * params.fee_bps * BPS


@dataclass(slots=True)
class PaperBroker:
    """纸上账户 + 撮合。挂单在 ``pending`` 里，等下一根 bar 开盘成交。"""

    account: Account
    params: FillParams = FillParams()
    pending: dict[str, Intent] = field(default_factory=dict)  # symbol -> 待成交意图
    fills: list[Fill] = field(default_factory=list)

    # ------------------------------------------------------------ 下单

    def submit(self, intent: Intent) -> bool:
        """接受一条意图，挂到下一根 bar 成交。

        同一标的已有持仓或已有挂单时拒绝 —— 本项目一个标的同时只持有一笔仓位
        （加仓/对锁属于策略复杂度，做了会让"成交与信号一一对应"这条验收失去意义）。
        """
        if intent.symbol in self.account.positions or intent.symbol in self.pending:
            return False
        self.pending[intent.symbol] = intent
        return True

    # ------------------------------------------------------------ 撮合

    def on_bar(self, bar: Bar) -> list[Fill]:
        """一根**已收盘**的 bar 到达。先平旧仓，再开新仓，**新仓在同一根上也要判出场**。

        顺序有讲究：先平旧仓再开新仓，否则"平仓释放的额度"会被同一根的新仓提前占用，
        敞口统计会短暂穿帮。

        新仓当根就判出场，是因为你以这根的**开盘价**入场，随后这根 bar 真的走过了
        它的高低区间 —— 触及止损是真实发生的，不是偷看未来。统计侧（ADR-0008）同样如此，
        两边必须一致。
        """
        if not bar.closed:
            return []
        out: list[Fill] = []
        out.extend(self._exits(bar))
        entry = self._entry(bar)
        if entry is not None:
            out.append(entry)
            out.extend(self._exits(bar))  # 新仓当根也要判
        self.fills.extend(out)
        return out

    def feed(self, bars: Sequence[Bar]) -> list[Fill]:
        return [f for bar in bars for f in self.on_bar(bar)]

    def _entry(self, bar: Bar) -> Fill | None:
        intent = self.pending.get(bar.symbol)
        if intent is None or bar.close_ts <= intent.created_at:
            return None  # 还没到下一根：意图在信号那根 bar 上不成交
        del self.pending[bar.symbol]
        price = _slip(bar.open, intent.side, self.params)
        fee = _fee(price, intent.qty, intent.multiplier, self.params)
        self.account.cash -= fee
        self.account.fees += fee
        self.account.positions[bar.symbol] = Position(
            symbol=bar.symbol, side=intent.side, qty=intent.qty, entry_price=price,
            multiplier=intent.multiplier, opened_at=bar.close_ts,
            signal_key=intent.signal_key, stop=intent.stop, target=intent.target,
            horizon_left=intent.horizon_bars,
        )
        return Fill(
            signal_key=intent.signal_key, symbol=bar.symbol, side=intent.side, qty=intent.qty,
            price=price, ts=bar.close_ts, kind=FillKind.ENTRY, fee=fee,
        )

    def _exits(self, bar: Bar) -> list[Fill]:
        pos = self.account.positions.get(bar.symbol)
        if pos is None:
            return []
        long = pos.side is Side.BUY
        hit_stop = pos.stop is not None and (bar.low <= pos.stop if long else bar.high >= pos.stop)
        hit_target = pos.target is not None and (
            bar.high >= pos.target if long else bar.low <= pos.target)

        if hit_stop:  # 同根同时触及时保守取止损（bar 给不出先后）
            return [self._close(bar, pos, float(pos.stop or bar.close), FillKind.STOP)]
        if hit_target:
            return [self._close(bar, pos, float(pos.target or bar.close), FillKind.TARGET)]
        if pos.horizon_left > 0:
            pos.horizon_left -= 1
            if pos.horizon_left == 0 and self.params.close_on_horizon:
                return [self._close(bar, pos, bar.close, FillKind.HORIZON)]
        return []

    def close_all(self, marks: dict[str, float], ts: int) -> list[Fill]:
        """按给定价格强平全部持仓（收盘、停机、换月）。"""
        out = [
            self._close_at(pos, marks[s], ts, FillKind.FORCED)
            for s, pos in list(self.account.positions.items()) if s in marks
        ]
        self.fills.extend(out)
        return out

    def _close(self, bar: Bar, pos: Position, price: float, kind: FillKind) -> Fill:
        return self._close_at(pos, price, bar.close_ts, kind, trading_day=bar.trading_day)

    def _close_at(
        self, pos: Position, price: float, ts: int, kind: FillKind, trading_day: str | None = None
    ) -> Fill:
        exit_side = Side.SELL if pos.side is Side.BUY else Side.BUY
        fill_price = _slip(price, exit_side, self.params)
        fee = _fee(fill_price, pos.qty, pos.multiplier, self.params)
        gross = (fill_price - pos.entry_price) * pos.side.sign * abs(pos.qty) * pos.multiplier
        realized = gross - fee
        self.account.cash += realized
        self.account.fees += fee
        self.account.realized += realized
        day = trading_day or _utc_day(ts)
        self.account.daily_realized[day] = self.account.daily_realized.get(day, 0.0) + realized
        del self.account.positions[pos.symbol]
        return Fill(
            signal_key=pos.signal_key, symbol=pos.symbol, side=exit_side, qty=pos.qty,
            price=fill_price, ts=ts, kind=kind, fee=fee, realized=realized,
        )

    # ------------------------------------------------------------ 快照

    def snapshot(self) -> dict[str, Any]:
        return {
            "account": self.account.as_row(),
            "pending": [i.as_dict() for i in self.pending.values()],
        }


def _utc_day(ts: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts, dt.UTC).date().isoformat()


__all__ = ["BPS", "FillParams", "PaperBroker"]
