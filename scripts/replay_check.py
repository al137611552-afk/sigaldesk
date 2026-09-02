#!/usr/bin/env python
"""M2 红线验收：同一时段 replay 与 live 产出的信号逐条一致。

做法（端到端，不打桩）：

1. **live**：从 REST 拉一段历史 + WS 实时收 N 分钟。每根 bar 进 BarStore、进引擎，
   同时**落进 Parquet**。收集 live 信号。
2. **replay**：全新的 BarStore + 引擎，用 ``ReplayFeed`` 把刚才落盘的同一批 bar 读回来重放。
3. 逐条比对（按标的分组 —— 跨标的的到达先后本来就不是确定量）。

用法：
    .venv/bin/python scripts/replay_check.py --minutes 8 --rules-dir config/rules
    .venv/bin/python scripts/replay_check.py --offline    # 只用 REST 历史，不等实时
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import functools
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.core.models import Bar, Market, Timeframe  # noqa: E402
from sigdesk.core.registry import load_registry  # noqa: E402
from sigdesk.feed.okx import OkxRestClient  # noqa: E402
from sigdesk.feed.okx_ws import OkxWsFeed  # noqa: E402
from sigdesk.feed.replay import ReplayFeed  # noqa: E402
from sigdesk.rules.engine import RuleEngine  # noqa: E402
from sigdesk.rules.loader import load_rules  # noqa: E402
from sigdesk.rules.model import Rule, Signal, store_timeframes  # noqa: E402
from sigdesk.store.bar_store import BarStore  # noqa: E402
from sigdesk.store.parquet_io import write_bars  # noqa: E402

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


def make_engine(rules: list[Rule]) -> tuple[RuleEngine, BarStore]:
    store = BarStore(timeframes=_timeframes(rules))
    return RuleEngine(rules, store), store


def by_symbol(signals: list[Signal]) -> dict[str, list[dict[str, object]]]:
    out: dict[str, list[dict[str, object]]] = {}
    for s in signals:
        out.setdefault(s.symbol, []).append(s.as_dict())
    return out


async def collect_live(
    rules: list[Rule], symbols: list, data_root: pathlib.Path, history_bars: int, minutes: float
) -> tuple[list[Signal], int]:
    """跑 live 链路：历史 + 实时，全部过引擎并落盘。"""
    engine, store = make_engine(rules)
    signals: list[Signal] = []
    written = 0

    async with OkxRestClient() as rest:
        now = int(dt.datetime.now(dt.UTC).timestamp()) // 60 * 60
        for sym in symbols:
            history = await rest.fetch_range(
                sym.code, sym.uid, Timeframe.M1, now - history_bars * 60, now
            )
            print(f"  [live] {sym.uid} 历史 {len(history)} 根")
            for bar in history:
                signals.extend(engine.on_bars(store.push(bar)))
            write_bars(data_root, history)
            written += len(history)

        if minutes > 0:
            feed = OkxWsFeed(symbols, rest=rest, resume_from=store.resume_map())
            print(f"  [live] 实时采集 {minutes} 分钟…")
            deadline = asyncio.get_running_loop().time() + minutes * 60
            agen = feed.stream()
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    bar = await asyncio.wait_for(anext(agen), timeout=remaining)
                except (TimeoutError, StopAsyncIteration):
                    break
                print(f"  [live] {bar.symbol} {bar.close_ts} c={bar.close}")
                signals.extend(engine.on_bars(store.push(bar)))
                write_bars(data_root, [bar])
                written += 1
            await agen.aclose()

    return signals, written


def collect_replay(rules: list[Rule], uids: list[str], data_root: pathlib.Path) -> list[Signal]:
    engine, store = make_engine(rules)
    feed = ReplayFeed(data_root, uids, 0, 2**31)
    bars: list[Bar] = feed.bars()
    print(f"  [replay] 从 Parquet 读回 {len(bars)} 根，区间 {feed.span()}")
    return [s for bar in bars for s in engine.on_bars(store.push(bar))]


async def main(minutes: float, rules_dir: pathlib.Path, history_bars: int, keep: bool) -> int:
    registry = load_registry(ROOT / "config")
    rules = load_rules(rules_dir, registry)
    uids = sorted({u for r in rules for u in r.universe})
    symbols = [registry.symbol(u) for u in uids if registry.symbol(u).market is Market.CRYPTO]
    if not symbols:
        print("规则里没有加密标的；本工具只跑加密（期货非交易时段拿不到实时数据）")
        return 1

    print(f"规则 {len(rules)} 条: {', '.join(r.id for r in rules)}")
    print(f"标的: {[s.uid for s in symbols]}")

    data_root = pathlib.Path(tempfile.mkdtemp(prefix="sigdesk-replay-"))
    try:
        live, written = await collect_live(rules, symbols, data_root, history_bars, minutes)
        print(f"\nlive: {len(live)} 条信号，落盘 {written} 根 bar")

        replay = collect_replay(rules, [s.uid for s in symbols], data_root)
        print(f"replay: {len(replay)} 条信号")

        live_by, replay_by = by_symbol(live), by_symbol(replay)
        ok = live_by == replay_by
        print()
        if ok:
            print(f"✅ 红线通过：replay 与 live 逐条一致（共 {len(live)} 条）")
            for s in live[:5]:
                print(f"   {s.symbol} @{s.fired_at} {s.rule_id} 触发价={s.trigger_price}")
            if len(live) > 5:
                print(f"   …另有 {len(live) - 5} 条")
        else:
            print("❌ 红线不通过")
            for uid in sorted(set(live_by) | set(replay_by)):
                a, b = live_by.get(uid, []), replay_by.get(uid, [])
                if a == b:
                    continue
                print(f"  {uid}: live {len(a)} 条 / replay {len(b)} 条")
                for i, (x, y) in enumerate(zip(a, b, strict=False)):
                    if x != y:
                        print(f"    第 {i} 条不同:\n      live  ={x}\n      replay={y}")
                        break
        if not live:
            print("⚠️  本次一条信号都没触发 —— 通过与否说明不了什么，请换更容易触发的规则重跑")
            return 2
        return 0 if ok else 1
    finally:
        if keep:
            print(f"\n数据保留在 {data_root}")
        else:
            shutil.rmtree(data_root, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="M2 红线验收：replay vs live")
    ap.add_argument("--minutes", type=float, default=8.0, help="实时采集时长；0 = 只用历史")
    ap.add_argument("--rules-dir", type=pathlib.Path, default=ROOT / "config" / "rules")
    ap.add_argument("--history-bars", type=int, default=600, help="先拉多少根 1m 历史")
    ap.add_argument("--keep", action="store_true", help="保留临时 Parquet 目录")
    ap.add_argument("--offline", action="store_true", help="等价于 --minutes 0")
    args = ap.parse_args()
    mins = 0.0 if args.offline else args.minutes
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(main(mins, args.rules_dir, args.history_bars, args.keep)))
    raise SystemExit(130)
