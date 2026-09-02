#!/usr/bin/env python
"""纸上回测：用历史行情跑完整链路 Signal → Intent → RiskGate → PaperBroker。

这是 M4 的验收手段，也是"这条规则按这套风控做下来到底赚不赚"的直接回答。
与实盘走**同一条代码路径**（ADR-0001）：只有 Feed 不同。

用法：
    .venv/bin/python scripts/paper_run.py                       # 用 data/ 里的历史
    .venv/bin/python scripts/paper_run.py --bars 1200 --fetch   # 先从 OKX 拉一段
    .venv/bin/python scripts/paper_run.py --rules-dir /tmp/rules --write-db data/paper.sqlite3
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import functools
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.core.models import Bar, Market, Timeframe  # noqa: E402
from sigdesk.core.registry import load_registry  # noqa: E402
from sigdesk.feed.okx import OkxRestClient  # noqa: E402
from sigdesk.feed.replay import ReplayFeed  # noqa: E402
from sigdesk.rules.engine import RuleEngine  # noqa: E402
from sigdesk.rules.loader import load_rules  # noqa: E402
from sigdesk.rules.model import Rule, store_timeframes  # noqa: E402
from sigdesk.store.bar_store import BarStore  # noqa: E402
from sigdesk.store.parquet_io import write_bars  # noqa: E402
from sigdesk.store.runtime_store import RuntimeStore  # noqa: E402
from sigdesk.trade.desk import TradeDesk  # noqa: E402
from sigdesk.trade.loader import load_trading  # noqa: E402
from sigdesk.trade.model import FillKind  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 脚本自己读 .env：`set -a; . ./.env` 是 bash 专有写法，Windows 上没有对应物。
# 查找顺序 SIGDESK_ENV -> ./.env -> ~/.signal-desk/.env（换新包也不用重配）。
ENV = load_env(ROOT)
DERIVED = [Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H1]


def _timeframes(rules: list[Rule]) -> list[Timeframe]:
    """规则要求的周期 ∪ 默认档。规则里多写一个 at('4h',...) 也不会漏派生。"""
    need = set(DERIVED) | set(store_timeframes(rules))
    return sorted(need, key=lambda t: t.rank)


async def fetch(uids: list[str], registry: object, root: pathlib.Path, bars: int) -> None:
    now = int(dt.datetime.now(dt.UTC).timestamp()) // 60 * 60
    async with OkxRestClient() as rest:
        for uid in uids:
            sym = registry.symbol(uid)  # type: ignore[attr-defined]
            if sym.market is not Market.CRYPTO:
                continue
            got = await rest.fetch_range(sym.code, uid, Timeframe.M1, now - bars * 60, now)
            print(f"  拉取 {uid}: {len(got)} 根")
            write_bars(root, got)


def main() -> int:
    ap = argparse.ArgumentParser(description="纸上回测")
    ap.add_argument("--data-root", type=pathlib.Path, default=ROOT / "data" / "bars")
    ap.add_argument("--rules-dir", type=pathlib.Path, default=ROOT / "config" / "rules")
    ap.add_argument("--trading", type=pathlib.Path, default=ROOT / "config" / "trading.yaml")
    ap.add_argument("--bars", type=int, default=1200, help="--fetch 时拉多少根 1m")
    ap.add_argument("--fetch", action="store_true", help="先从 OKX 拉一段历史")
    ap.add_argument("--write-db", type=pathlib.Path, default=None,
                    help="把成交与账户落到这个 SQLite（面板可读）")
    ap.add_argument("--force-enable", action="store_true",
                    help="忽略 trading.yaml 里的 enabled: false")
    args = ap.parse_args()

    registry = load_registry(ROOT / "config")
    rules = load_rules(args.rules_dir, registry)
    params = load_trading(args.trading)
    if args.force_enable:
        params.enabled = True
    if not params.enabled:
        print("纸上交易台未启用。在 config/trading.yaml 里把 enabled 改成 true，"
              "或加 --force-enable 临时打开。")
        return 1

    uids = sorted({u for r in rules for u in r.universe})
    known = [u for u in uids if u in registry.symbols]
    if args.fetch:
        print("拉取历史…")
        asyncio.run(fetch(known, registry, args.data_root, args.bars))

    feed = ReplayFeed(args.data_root, known, 0, 2**31)
    bars: list[Bar] = feed.bars()
    if not bars:
        print(f"{args.data_root} 下没有这些标的的 1m 数据：{known}\n"
              f"加 --fetch 先拉一段，或用 scripts/backfill.py 回补期货。")
        return 1
    span = feed.span()
    print(f"规则 {len(rules)} 条 · 标的 {len(known)} 个 · {len(bars)} 根 1m · 区间 {span}")
    print(f"口径: {params.strategy.mode} 定量 risk={params.strategy.risk_per_trade:.2%} · "
          f"止损 {params.strategy.exits.stop_pct:.2%}/ATR×{params.strategy.exits.stop_atr} · "
          f"费 {params.fills.fee_bps}bp 滑点 {params.fills.slippage_bps}bp")

    store = BarStore(timeframes=_timeframes(rules))
    engine = RuleEngine(rules, store)
    desk = TradeDesk([registry.symbol(u) for u in known], params)

    produced = []
    for bar in bars:
        derived = store.push(bar)
        desk.on_bars(derived)            # 先撮合：挂单按开盘成交、持仓判出场
        fired = engine.on_bars(derived)  # 再产信号
        produced.extend(fired)
        desk.on_signals(fired)           # 新意图挂上，等下一根
    signals = len(produced)

    marks = {b.symbol: b.close for b in bars}
    forced = desk.broker.close_all(marks, bars[-1].close_ts)
    if forced:
        print(f"回测结束强平 {len(forced)} 笔")

    s = desk.summary()
    fills = desk.broker.fills
    entries = [f for f in fills if f.kind is FillKind.ENTRY]
    by_kind: dict[str, int] = {}
    for f in fills:
        if f.kind is not FillKind.ENTRY:
            by_kind[str(f.kind)] = by_kind.get(str(f.kind), 0) + 1

    print(f"\n信号 {signals} 条 → 开仓 {len(entries)} 笔 → 平仓 {len(fills) - len(entries)} 笔")
    if desk.rejections:
        reasons: dict[str, int] = {}
        for r in desk.rejections:
            reasons[str(r.reason)] = reasons.get(str(r.reason), 0) + 1
        print("风控拒单: " + " · ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    print("平仓构成: " + (" · ".join(f"{k} {v}" for k, v in sorted(by_kind.items())) or "无"))
    print(f"\n权益 {s['equity']:,.2f}（{s['return_pct']:+.3%}）"
          f" · 已实现 {s['realized']:+,.2f} · 手续费 {s['fees']:,.2f}")
    wins = [f.realized for f in fills if f.kind is not FillKind.ENTRY and f.realized > 0]
    losses = [f.realized for f in fills if f.kind is not FillKind.ENTRY and f.realized < 0]
    total = len(wins) + len(losses)
    if total:
        print(f"胜率 {len(wins) / total:.1%}（{len(wins)} 胜 / {len(losses)} 负）"
              f" · 均盈 {sum(wins) / len(wins):+,.2f}" if wins else "")
    if s["positions"]:
        print(f"仍持仓 {len(s['positions'])} 笔")

    if args.write_db:
        with RuntimeStore(args.write_db) as rs:
            # 信号也要落 —— 只落成交的话，"成交与信号一一对应"在库里就无从核对，
            # 面板上的成交也会指向一条查不到的信号。
            rs.append_signals(produced)
            rs.append_fills(fills)
            rs.save_trade_state(desk.snapshot())
            print(f"\n已落库 {args.write_db}"
                  f"（{rs.count_signals()} 条信号 / {rs.count_fills()} 笔成交），面板可直接看")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
