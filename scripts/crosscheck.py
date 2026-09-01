#!/usr/bin/env python
"""对拍工具：本地 1m 聚合 vs 接口原生高周期线，逐根比对。

这是 M0-A 的验收手段，也是日后换数据源/改分桶逻辑时的回归工具。
默认只比对**归档区间**（当日 00:00 之前）—— 数据源当日数据自身不自洽。

用法：
    .venv/bin/python scripts/crosscheck.py CN.SHFE.rb2610 CN.SHFE.au2612
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.core.models import CST, Bar, Timeframe  # noqa: E402
from sigdesk.core.registry import load_registry  # noqa: E402
from sigdesk.feed.quote_api import QuoteApiClient, QuoteApiConfig, normalize_klines  # noqa: E402
from sigdesk.store.bar_builder import aggregate  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001  长任务实时输出进度

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 脚本自己读 .env：`set -a; . ./.env` 是 bash 专有写法，Windows 上没有对应物。
# 查找顺序 SIGDESK_ENV -> ./.env -> ~/.signal-desk/.env（换新包也不用重配）。
ENV = load_env(ROOT)
DERIVED = [Timeframe.M5, Timeframe.M15, Timeframe.H1]
FIELDS = ("open", "high", "low", "close", "volume")


def _hhmm(t: int) -> str:
    return dt.datetime.fromtimestamp(t, CST).strftime("%m-%d %H:%M")


def compare(built: list[Bar], native: list[Bar]) -> tuple[int, list[str]]:
    """返回 (可比对根数, 差异描述)。只比对两边都有、且成分完整的桶。"""
    nat = {b.close_ts: b for b in native}
    bui = {b.close_ts: b for b in built}
    common = sorted(set(nat) & set(bui))
    bad = [
        f"{_hhmm(t)} {f}: 接口={getattr(nat[t], f)} 聚合={getattr(bui[t], f)}"
        for t in common
        for f in FIELDS
        if getattr(nat[t], f) != getattr(bui[t], f)
    ]
    # 只在 built 的时间跨度之内追究"漏产出"：
    # 首桶可能成分不全，末桶按设计不吐出（未确认收盘），两端都不算漏。
    if bui:
        lo, hi = min(bui), max(bui)
        bad += [f"{_hhmm(t)} 漏产出整根" for t in sorted(set(nat) - set(bui)) if lo < t < hi]
    return len(common), bad


async def check(client: QuoteApiClient, uid: str, quote_code: str, cutoff: int) -> tuple[int, int]:
    raw_1m = await client.kline_by_count(quote_code, Timeframe.M1, 2000)
    m1 = [
        b
        for b in normalize_klines(raw_1m, symbol=uid, timeframe=Timeframe.M1, now_ts=cutoff)
        if b.close_ts < cutoff
    ]
    if len(m1) < 100:
        print(f"  {uid}: 1m 数据不足（{len(m1)} 根），跳过")
        return (0, 0)

    compared = diffs = 0
    print(f"\n{uid}  1m {len(m1)} 根  {_hhmm(m1[0].close_ts)} ~ {_hhmm(m1[-1].close_ts)}")
    for tf in DERIVED:
        need = min(2000, len(raw_1m) * 60 // tf.seconds + 10)
        raw_hi = await client.kline_by_count(quote_code, tf, need)
        native = [
            b
            for b in normalize_klines(raw_hi, symbol=uid, timeframe=tf, now_ts=cutoff)
            if b.close_ts < cutoff and b.open_ts >= m1[0].open_ts
        ]
        built = [b for b in aggregate(uid, m1, tf) if b.open_ts >= m1[0].open_ts]
        n, bad = compare(built, native)
        mark = "OK  " if not bad else "FAIL"
        print(f"  [{mark}] {tf.value:>3}: 比对 {n:>4} 根，差异 {len(bad)} 处")
        for line in bad[:5]:
            print(f"         {line}")
        compared += n
        diffs += len(bad)
    return (compared, diffs)


async def main(uids: list[str]) -> int:
    cfg = QuoteApiConfig(
        base_url=os.environ["QUOTE_API_BASE"],
        api_key=os.environ["QUOTE_API_KEY"],
        tls_fingerprint=os.environ.get("QUOTE_API_TLS_FINGERPRINT", ""),
        timeout_s=90.0,
    )
    reg = load_registry(ROOT / "config")
    today = dt.datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = int(today.timestamp())
    print(f"对拍区间：当日 00:00 之前的归档数据（cutoff = {today:%Y-%m-%d %H:%M} CST）")

    totals = [0, 0]  # [比对根数, 差异处数]
    async with QuoteApiClient(cfg) as client:
        for uid in uids:
            sym = reg.symbol(uid)
            if not sym.quote_code:
                print(f"  {uid}: 无 quote_code，跳过")
                continue
            n, bad = await check(client, uid, sym.quote_code, cutoff)
            totals[0] += n
            totals[1] += bad
    rate = totals[1] / totals[0] if totals[0] else 0.0
    print(f"\n合计：比对 {totals[0]} 根，差异 {totals[1]} 处（{rate:.3%}）")
    if totals[1] == 0:
        print("结论：全部一致 ✅")
    else:
        print("结论：存在差异。判别方法 ——")
        print("  · 差异成片出现        -> 分桶/聚合逻辑缺陷，必须修")
        print("  · 零星单点、同一时刻其他周期一致 -> 数据源自身不自洽（已知，见 CLAUDE.md 坑#8）")
    return 0 if totals[1] == 0 else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(args)))
