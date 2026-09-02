#!/usr/bin/env python
"""批量回补：把注册表里的标的挨个过一遍 `backfill.py`。

    # 两年日线（推荐先跑这个：一个品种几秒，全量二十来分钟）
    python scripts/backfill_all.py --timeframe 1d --start 2024-01-01 --end 2026-09-01

    # 只补最近两个月的 1m（规则真正要用的精度，但很慢）
    python scripts/backfill_all.py --timeframe 1m --start 2026-07-01 --end 2026-09-01

    python scripts/backfill_all.py --dry-run          # 只列会补哪些
    python scripts/backfill_all.py --market CN        # 只补国内期货

三条设计：

1. **按标的降级**：一个品种失败不影响其余，末尾汇总。66 个品种跑一小时，
   中间挂一个就整体退出的话，前面的进度全白费。
2. **可续跑**：默认跳过"本地数据已经覆盖到 end"的标的（只看 Parquet 分区目录名，
   不读文件）。中断后重跑很便宜，不会把已经补好的again 拉一遍。
3. **串行 + 间隔**：行情接口跨度一大就读超时（CLAUDE.md 坑），并发只会更糟。
   宁可慢，也要跑得完。

**主连不补**：它是 build_continuous.py 从成分合约拼出来的派生序列。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import backfill  # noqa: E402  同目录，复用它的单标的逻辑

from sigdesk.core.models import Market, Timeframe  # noqa: E402
from sigdesk.core.registry import load_registry  # noqa: E402
from sigdesk.store.parquet_io import partition_span  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def already_covered(uid: str, timeframe: Timeframe, start: str, end: str) -> str | None:
    """本地数据是否已经覆盖 [start, end]。覆盖了返回 "首日~末日"，否则 None。

    **首尾都要看。** 只看末日的话，"补过近两个月"会被误判成"已覆盖两年"，
    拉长历史的请求就被静默跳过 —— 数据看着有、其实短一大截（实测撞上过：
    au/m/cu 三个只有二十几到四十几根日线，却被当成已覆盖）。

    起点用**宽容比较**：合约上市日晚于 start 是常态（近月合约本来就没那么长的历史），
    那时本地首日会晚于 start 而且永远补不到 —— 所以只要首日不晚于 start 就算覆盖，
    真正的判据是"再拉也拉不到更早的了"，由调用方用 --force 强制重补来兜底。
    """
    span = partition_span(ROOT / "data" / "bars", uid, timeframe)
    if not span:
        return None
    first, last = span
    return f"{first}~{last}" if (first <= start and last >= end) else None


async def run(args: argparse.Namespace) -> int:
    reg = load_registry(ROOT / "config")
    tf = Timeframe(args.timeframe)
    wanted = []
    for sym in reg.tradable():          # tradable() 已排除主连
        if args.market != "all" and str(sym.market) != args.market:
            continue
        if args.only and sym.uid not in args.only:
            continue
        wanted.append(sym)
    if not wanted:
        print("没有匹配的标的")
        return 1

    todo, skipped = [], []
    for sym in wanted:
        covered = None if args.force else already_covered(sym.uid, tf, args.start, args.end)
        (skipped if covered else todo).append((sym, covered))

    print(f"共 {len(wanted)} 个标的：待补 {len(todo)}，已覆盖跳过 {len(skipped)}")
    if skipped and args.verbose:
        for sym, day in skipped:
            print(f"    跳过 {sym.uid:28s} 已有 {day}")
    if args.dry_run:
        for sym, _ in todo:
            print(f"    待补 {sym.uid}")
        print("\n--dry-run：没有真的拉数据")
        return 0

    ok, failed = 0, []
    t0 = time.time()
    for i, (sym, _) in enumerate(todo, 1):
        print(f"\n[{i}/{len(todo)}] {sym.uid}")
        try:
            rc = await backfill.main(sym.uid, args.start, args.end, tf)
        except Exception as e:                    # noqa: BLE001 一个失败不该带走其余
            failed.append((sym.uid, f"{type(e).__name__}: {e}"))
            print(f"    ✗ {type(e).__name__}: {str(e)[:120]}")
        else:
            if rc == 0:
                ok += 1
            else:
                failed.append((sym.uid, f"退出码 {rc}"))
        if i < len(todo):
            await asyncio.sleep(args.gap)

    mins = (time.time() - t0) / 60
    print(f"\n{'=' * 56}\n完成 {ok}/{len(todo)}，用时 {mins:.1f} 分钟")
    if failed:
        print(f"失败 {len(failed)} 个（重跑本命令即可，已补好的会被跳过）：")
        for uid, why in failed:
            print(f"    {uid:28s} {why}")
    return 0 if not failed else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="批量回补历史")
    ap.add_argument("--timeframe", default="1d", choices=["1m", "5m", "15m", "30m", "1h", "1d"])
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=None, help="默认昨天（接口不含当日）")
    ap.add_argument("--market", default="all", choices=["all", "CN", "CRYPTO"])
    ap.add_argument("--only", nargs="*", default=[], help="只补这些 uid")
    ap.add_argument("--force", action="store_true", help="已覆盖的也重补")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--gap", type=float, default=1.0, help="每个标的之间的间隔秒数")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if a.end is None:
        import datetime as dt
        a.end = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    if a.market != "CRYPTO":
        a.market = a.market if a.market == "all" else Market.CN_FUTURES.value
    raise SystemExit(asyncio.run(run(a)))
