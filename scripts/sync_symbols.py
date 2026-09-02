#!/usr/bin/env python
"""从行情接口同步国内期货标的到 `config/symbols.yaml`。

    python scripts/sync_symbols.py --with-options          # 只要有期权的品种（推荐）
    python scripts/sync_symbols.py --with-options --dry-run

**为什么需要这个脚本**：规则里写 `CN.*`（国内期货全品种），但"全"的定义是
`symbols.yaml` 里登记了的标的 —— 手工登记 60 多个品种、每次换月还要手工改，
不现实。这个脚本把可自动获取的部分自动化。

**能自动拿的**（都从接口来，不猜）：
  - 品种清单：`search(keyword="主连")` 里的 9999 代码
  - 有期权的品种：`search(keyword="期权")` 里的标的品种码（交易所只在流动性够的
    标的上挂期权，所以这是个比"我觉得哪些活跃"可靠得多的筛子）
  - price_tick / multiplier：搜索结果自带。**这两个绝不能猜** ——
    乘数错一个量级，持仓和盈亏就全错，而且看着像模像样
  - 当前主力合约：`main-by-date`（把 end_date 设成今天）

**不能自动拿的**：交易日历（夜盘到几点）。接口不给，按品种映射写在
`config/calendars/cn_futures.yaml` 的 `products:` 里。**没有日历的品种直接跳过并列出**，
不猜一个 —— 猜错的后果是轮询在错误的时段跑、健康面板误报"数据滞后"。
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import os
import pathlib
import re
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from sigdesk.core.env import load_env  # noqa: E402
from sigdesk.feed.quote_api import QuoteApiClient, QuoteApiConfig  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = load_env(ROOT)
CN_EX = {"SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"}
# 股指期权（IO/MO/HO）本身不是期货，它们的标的是 IF/IM/IH
INDEX_OPTION_UNDERLYING = {"IO": "IF", "MO": "IM", "HO": "IH"}


def calendars() -> dict[str, str]:
    """品种码 -> 日历 id。来自 cn_futures.yaml 里每个日历的 products 列表。"""
    raw = yaml.safe_load((ROOT / "config" / "calendars" / "cn_futures.yaml")
                         .read_text(encoding="utf-8"))
    return {p: cid for cid, c in (raw.get("calendars") or {}).items()
            for p in (c.get("products") or [])}


async def discover(client: QuoteApiClient, only_options: bool) -> dict[str, dict[str, Any]]:
    """品种码 -> {exchange, price_tick, multiplier, name}。"""
    products: dict[str, dict[str, Any]] = {}
    incomplete: list[str] = []
    for row in await client.search(keyword="主连"):
        code = str(row.get("code") or "")
        if row.get("exchange_code") not in CN_EX or not code.endswith("9999"):
            continue
        tick, mult = row.get("price_tick"), row.get("multiplier")
        # **规格缺失就跳过，绝不填默认值。** 乘数错一个量级，持仓和盈亏全错，
        # 而且数字看着像模像样 —— 这比缺一个品种危险得多。
        if not tick or not mult:
            incomplete.append(code[:-4])
            continue
        products[code[:-4]] = {
            "exchange": row["exchange_code"],
            "price_tick": tick,
            "multiplier": mult,
            "name": str(row.get("name") or "").replace("主连", ""),
        }
    if incomplete:
        print(f"⚠️  接口没给全 tick/乘数，已跳过 {len(incomplete)} 个: "
              f"{' '.join(sorted(incomplete))}")
    if not only_options:
        return products

    wanted: set[str] = set()
    for row in await client.search(keyword="期权"):
        if row.get("exchange_code") not in CN_EX:
            continue
        m = re.match(r"^([A-Za-z]+)\d{3,4}-", str(row.get("code") or ""))
        if not m:
            continue
        p = m.group(1)
        wanted.add(INDEX_OPTION_UNDERLYING.get(p, p))
    return {p: v for p, v in products.items() if p in wanted}


async def run(only_options: bool, dry_run: bool) -> int:
    cfg = QuoteApiConfig(
        base_url=os.environ["QUOTE_API_BASE"],
        api_key=os.environ["QUOTE_API_KEY"],
        tls_fingerprint=os.environ.get("QUOTE_API_TLS_FINGERPRINT", ""),
    )
    cal = calendars()
    today = dt.date.today().isoformat()
    week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()

    async with QuoteApiClient(cfg) as client:
        products = await discover(client, only_options)
        have_cal = {p: v for p, v in products.items() if p in cal}
        missing = sorted(set(products) - set(have_cal))
        print(f"发现 {len(products)} 个品种，其中 {len(have_cal)} 个有日历")
        if missing:
            print(f"⚠️  缺日历，已跳过 {len(missing)} 个: {' '.join(missing)}")
            print("    补法：在 config/calendars/cn_futures.yaml 里把它们加进对应日历的"
                  " products 列表（按夜盘收盘时间分组）")
        if not have_cal:
            return 1
        mains = await client.main_by_date(
            [f"{p}9999" for p in sorted(have_cal)], week_ago, today)

    latest: dict[str, str] = {}
    for row in mains:              # 同一品种可能返回多段，取最后生效的那段
        p = str(row["main_variety_code"])[:-4]
        latest[p] = str(row["variety_code"])
    no_main = sorted(set(have_cal) - set(latest))
    if no_main:
        print(f"⚠️  取不到当前主力，已跳过 {len(no_main)} 个: {' '.join(no_main)}")

    entries = []
    for p in sorted(latest):
        info = have_cal[p]
        code = latest[p]
        entries.append({
            "uid": f"CN.{info['exchange']}.{code}",
            "market": "CN",
            "exchange": info["exchange"],
            "code": code,
            "calendar": cal[p],
            "quote_code": code,
            "price_tick": info["price_tick"],
            "multiplier": info["multiplier"],
            "product": p,
        })
    print(f"\n将写入 {len(entries)} 个国内期货标的：")
    by_ex: dict[str, list[str]] = collections.defaultdict(list)
    for e in entries:
        by_ex[e["exchange"]].append(str(e["code"]))
    for ex in sorted(by_ex):
        print(f"  {ex:6s} {len(by_ex[ex]):2d}  {' '.join(sorted(by_ex[ex]))}")

    if dry_run:
        print("\n--dry-run：没有写文件")
        return 0
    return write(entries)


def write(entries: list[dict[str, Any]]) -> int:
    """重写 symbols.yaml 的**国内期货部分**，其余原样保留。

    加密标的不来自这个接口，主连是本地拼出来的 —— 两者都必须保住，
    所以不是整个文件重生成。
    """
    path = ROOT / "config" / "symbols.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    kept = [s for s in (raw.get("symbols") or [])
            if str(s.get("market")) != "CN" or s.get("is_continuous")]
    raw["symbols"] = entries + kept
    path.write_text(
        "# 由 scripts/sync_symbols.py 生成国内期货部分；加密与主连条目手工维护。\n"
        "# 换月后重跑这个脚本即可 —— 主力合约会跟着变。\n"
        + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    print(f"\n已写入 {path}（国内期货 {len(entries)} + 保留 {len(kept)}）")
    print("下一步：回补历史，否则规则永远预热不完")
    print("  python scripts/backfill.py <uid> <起> <止> --timeframe 1d")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="同步国内期货标的到 symbols.yaml")
    ap.add_argument("--with-options", action="store_true",
                    help="只要有期权的品种（交易所只在流动性够的标的上挂期权）")
    ap.add_argument("--dry-run", action="store_true", help="只看会写什么，不落盘")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(run(a.with_options, a.dry_run)))
