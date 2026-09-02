#!/usr/bin/env python
"""用**真实成交时段**核对每个品种归的交易日历。

    python scripts/check_calendars.py            # 只报不符
    python scripts/check_calendars.py --all      # 全部列出

**为什么必须验而不是猜**：归错是静默的，而且后果比想象的重 ——
夜盘跨零点的品种若被归成"无夜盘"，它周五夜盘次日 00:xx 那根会被算成**周六**的
交易日，日线聚合当场抛"时间倒流"，盯盘进程在首次喂历史时直接崩掉。
实测 `ad2611`（铸造铝合金）就是这么崩的。

首次跑这个脚本一次查出 6 个错，其中 2 个（PF 短纤、SA 纯碱）是很早就存在的。
**换月、加新品种、改日历之后都该跑一遍。**
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.core.models import CST, Timeframe  # noqa: E402
from sigdesk.core.registry import load_registry  # noqa: E402
from sigdesk.feed.quote_api import QuoteApiClient, QuoteApiConfig  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = load_env(ROOT)


def infer(hours: set[int]) -> str:
    """从实际成交小时（北京时间）推断该属于哪一档夜盘。

    只看**最晚**收到几点：见到 2 点 -> 02:30 档；见到 0/1 点 -> 01:00 档；
    只有 21~23 点 -> 23:00 档；都没有 -> 无夜盘。
    """
    if 2 in hours:
        return "cn_night_0230"
    if 0 in hours or 1 in hours:
        return "cn_night_01"
    if 21 in hours or 22 in hours or 23 in hours:
        return "cn_night_23"
    return "cn_no_night"


async def run(show_all: bool) -> int:
    reg = load_registry(ROOT / "config")
    cfg = QuoteApiConfig(
        base_url=os.environ["QUOTE_API_BASE"],
        api_key=os.environ["QUOTE_API_KEY"],
        tls_fingerprint=os.environ.get("QUOTE_API_TLS_FINGERPRINT", ""),
    )
    bad: list[tuple[str, str, str, list[int]]] = []
    checked = 0
    async with QuoteApiClient(cfg) as client:
        for sym in reg.tradable():
            if str(sym.market) != "CN" or not sym.quote_code:
                continue
            # 股指/国债的**日盘**时段就与商品不同，这里只判夜盘档，对它们没意义
            if sym.calendar.startswith("cffex"):
                continue
            try:
                rows = await client.kline_by_count(sym.quote_code, Timeframe.M1, 2000)
            except Exception as e:                # noqa: BLE001 一个失败不该带走其余
                print(f"  ⚠️  {sym.uid:24s} 取数失败 {type(e).__name__}: {str(e)[:40]}")
                continue
            checked += 1
            hours = {dt.datetime.fromtimestamp(int(r["time_stamp"]), CST).hour for r in rows}
            want = infer(hours)
            night = sorted(h for h in hours if h >= 21 or h <= 3)
            if want != sym.calendar:
                bad.append((sym.uid, sym.calendar, want, night))
            elif show_all:
                print(f"  ✅ {sym.uid:24s} {sym.calendar:14s} 夜盘 {night or '无'}")

    print(f"\n核对 {checked} 个标的，不符 {len(bad)} 个")
    for uid, cur, want, night in bad:
        print(f"  ❌ {uid:24s} {cur:14s} -> {want:14s} 实际夜盘小时 {night or '无'}")
    if bad:
        print("\n改法：在 config/calendars/cn_futures.yaml 里把品种挪到正确的 products 列表，"
              "然后**重跑 scripts/sync_symbols.py** ——\n"
              "     symbols.yaml 里存的是同步时解析好的日历 id，改 products 不会回溯。")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="用真实成交时段核对交易日历")
    ap.add_argument("--all", action="store_true", help="连对的也列出来")
    raise SystemExit(asyncio.run(run(ap.parse_args().all)))
