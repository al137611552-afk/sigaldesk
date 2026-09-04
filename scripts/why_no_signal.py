#!/usr/bin/env python3
"""为什么这个标的没报信号 —— 逐级别把原因摊开。

「某某为什么没报」有三种完全不同的答案，混在一起就只能猜：

  1. **没数据**：规则要 1h/5m，本地只有日线 -> 永远不会求值，也不报错，就是安静。
  2. **预热不够**：指标要 N 根才出值，之前一律是 None（三值逻辑判为不成立）。
  3. **条件确实不成立**：这才是"行情不符合"。

而第 3 种还要再分：表达式自己成立没有（raw）、链路认不认（satisfied）。
`mode: event` 只认跳变、`window` 只看最近 N 根 —— **表达式明明成立、链路却不认**
是最常见的答案，所以两者必须分开看。

用法：
    .venv/bin/python scripts/why_no_signal.py CN.CFFEX.IM2609
    .venv/bin/python scripts/why_no_signal.py CN.CFFEX.IM2609 --rule kdzx-short
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.models import Bar, Timeframe  # noqa: E402
from sigdesk.core.registry import load_registry  # noqa: E402
from sigdesk.rules.loader import load_rules  # noqa: E402
from sigdesk.rules.model import Rule  # noqa: E402
from sigdesk.rules.trial import run_trial  # noqa: E402
from sigdesk.store.parquet_io import read_bars  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_1m(data_root: pathlib.Path, uid: str) -> list[Bar]:
    """逐分区读 1m。**`read_bars(path, symbol, tf)` 的 symbol 是贴标签不是筛选** ——
    给它目录会把整棵树读进来还都标成这个 uid，看不出任何异常（踩过）。"""
    base = data_root / uid.split(".", 1)[0] / uid / Timeframe.M1.value
    if not base.exists():
        return []
    out: list[Bar] = []
    for f in sorted(base.glob("*.parquet")):
        out.extend(b for b in read_bars(f, uid, Timeframe.M1) if b.closed)
    out.sort(key=lambda b: b.close_ts)
    return out


def coverage(data_root: pathlib.Path, uid: str, tf: Timeframe) -> int:
    base = data_root / uid.split(".", 1)[0] / uid / tf.value
    if not base.exists():
        return 0
    import contextlib

    import pyarrow.parquet as pq
    n = 0
    for f in base.glob("*.parquet"):
        with contextlib.suppress(Exception):
            n += pq.read_metadata(f).num_rows   # 只读元数据，不读行数据
    return n


def explain(rule: Rule, uid: str, bars: list[Bar], data_root: pathlib.Path) -> None:
    print(f"\n── {rule.id}（{rule.emit.direction}）")
    if uid not in rule.universe:
        print("   这条规则的 universe 不含该标的 —— 根本不会盯它。")
        return

    # ① 数据够不够
    need = sorted({tf for tf in rule.timeframes.values()}, key=lambda t: t.rank)
    missing = []
    for tf in need:
        n = coverage(data_root, uid, tf)
        derived = "（由 1m 聚合）" if not n and bars else ""
        print(f"   需要 {tf.value:<5} 本地 {n:>6} 根 {derived}")
        if not n and not bars:
            missing.append(tf.value)
    if not bars:
        print(f"\n   ⇒ **没有 1m 数据**，{'、'.join(missing) or '高周期'} 也就无从聚合。")
        print("      规则永远不会被求值 —— 不是「行情不符合」，是采不到数据。")
        print("      查：这个标的在不在某条规则的 universe 里（不在就不采集）；")
        print("          watch.py 跑了多久；scripts/backfill.py 回补过没有。")
        return

    # ② 跑一遍，看逐级别的判定
    res = run_trial(rule, {uid: bars})
    counts = res.condition_counts.get(uid, {})
    print(f"\n   在 {len(bars):,} 根 1m 上跑了一遍，触发 {len(res.signals)} 条")
    if not counts:
        print("   引擎一次都没求值 —— 多半是扳机周期一根都没聚合出来。")
        return

    print(f"\n   {'级别':<10}{'周期':<6}{'成立':>8}{'不成立':>8}{'预热/未知':>10}   卡点")
    order = [c.role for c in rule.conditions]
    for role in order:
        c = counts.get(role)
        if not c:
            print(f"   {role:<10}{rule.timeframes[role].value:<6}{'（没求值）':>26}")
            continue
        t, f, u = c.get("true", 0), c.get("false", 0), c.get("unknown", 0)
        tot = t + f + u
        note = ""
        if t == 0 and u == tot:
            note = "← **全是预热**，指标还没出值"
        elif t == 0:
            note = "← **一次都没成立**，卡在这一级"
        elif t < tot * 0.02:
            note = f"← 只成立 {t/tot*100:.1f}%，很稀有"
        print(f"   {role:<10}{rule.timeframes[role].value:<6}{t:>8}{f:>8}{u:>10}   {note}")

    blocked = [r for r in order if counts.get(r, {}).get("true", 0) == 0]
    print()
    if res.signals:
        print("   ⇒ 有触发。上面各级别的数字说明它有多稀有。")
    elif blocked:
        print(f"   ⇒ **卡在「{blocked[0]}」这一级**：它一次都没成立，后面的自然轮不到。")
    else:
        print("   ⇒ 各级别单独都成立过，但**没有按顺序凑齐过** ——")
        print("      链路要求先后成立且 state 型的中途不能失效（ttl 内）。这才是「行情不符合」。")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol", help="标的 uid，如 CN.CFFEX.IM2609")
    ap.add_argument("--rule", help="只看这一条规则；不给就看全部启用的")
    ap.add_argument("--data-root", default=str(ROOT / "data" / "bars"))
    # 与 rule_eval.py 一致：试变体时指向副本，**别动 config/rules**（盯盘进程正在用它）
    ap.add_argument("--rules-dir", default=str(ROOT / "config" / "rules"),
                    help="规则目录；试变体时指向副本，别动 config/rules")
    args = ap.parse_args()

    reg = load_registry(ROOT / "config")
    rules_dir = pathlib.Path(args.rules_dir)
    rules = [r for r in load_rules(rules_dir, registry=reg)
             if not args.rule or r.id == args.rule]
    if not rules:
        print(f"{rules_dir} 里没有这条规则：{args.rule}")
        return 2

    uid = args.symbol
    if uid not in reg.symbols:
        near = [s for s in reg.symbols if uid.split(".")[-1].lower() in s.lower()]
        print(f"注册表里没有 {uid}" + (f"\n你是不是想找：{', '.join(near[:5])}" if near else ""))
        return 2

    data_root = pathlib.Path(args.data_root)
    bars = load_1m(data_root, uid)
    print(f"标的 {uid}　本地 1m：{len(bars):,} 根" +
          (f"（{bars[0].trading_day} ~ {bars[-1].trading_day}）" if bars else "（无）"))
    for rule in rules:
        explain(rule, uid, bars, data_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
