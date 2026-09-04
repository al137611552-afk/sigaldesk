#!/usr/bin/env python3
"""规则评估：毛期望 + **随机进场基准** + 超额。

**为什么不能只看毛收益。** 样本区间本身有漂移（这份本地样本等权 +10.05%，
加密两个标的 +21% / +27%），任何多头规则在里面都显得很好、任何空头都显得很差。
判据必须是**超额** = 规则期望 − 随机进场期望。

**基准必须按该条规则自己的信号分布加权。** 规则 A 的信号 80% 打在 BTC 上、
规则 B 的 80% 打在黄金上，拿同一个"全品种等权基准"去减，等于拿别人的行情当尺子。
所以这里按 {标的: 该标的上的信号数} 做权重，对每个标的单独算随机进场期望。

随机进场 = 在该标的扳机周期的每一根 bar 上，以同方向、同一套出场口径开一笔，
取平均。它回答的是"在这段行情里闭着眼睛做，期望是多少"。

用法：
    .venv/bin/python scripts/rule_eval.py kdzx-short
    .venv/bin/python scripts/rule_eval.py kdzx-short --cost-bps 1
    .venv/bin/python scripts/rule_eval.py --all
"""

from __future__ import annotations

import argparse
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.models import Bar, Timeframe  # noqa: E402
from sigdesk.core.registry import load_registry  # noqa: E402
from sigdesk.rules.loader import load_rules  # noqa: E402
from sigdesk.rules.model import Rule  # noqa: E402
from sigdesk.rules.trial import run_trial  # noqa: E402
from sigdesk.stats.baseline import (  # noqa: E402
    effective_n,
    excess_se,
    random_entry_expectation,
    standard_error,
)
from sigdesk.stats.outcome import ExitReason, OutcomeParams  # noqa: E402
from sigdesk.stats.report import summarize  # noqa: E402
from sigdesk.store.bar_builder import aggregate  # noqa: E402
from sigdesk.store.parquet_io import read_bars  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_1m(data_root: pathlib.Path, uid: str) -> list[Bar]:
    """读一个标的的全部 1m。没有 1m 的标的直接跳过 —— 试算的输入必须是 1m。

    **必须逐个分区文件读。** `read_bars(path, symbol, tf)` 里的 `symbol` 是
    **贴标签**不是筛选：给它一个目录，它会把整棵树读进来、然后统统标成你要的那个
    uid —— 七个标的返回一模一样的根数（全库总行数），而且看不出任何异常。
    """
    base = data_root / uid.split(".", 1)[0] / uid / Timeframe.M1.value
    if not base.exists():
        return []
    out: list[Bar] = []
    for f in sorted(base.glob("*.parquet")):
        out.extend(b for b in read_bars(f, uid, Timeframe.M1) if b.closed)
    out.sort(key=lambda b: b.close_ts)
    return out


def evaluate_rule(
    rule: Rule, series_1m: dict[str, list[Bar]], params: OutcomeParams, stride: int = 10
) -> dict[str, object]:
    res = run_trial(rule, series_1m, outcome_params=params)
    st = res.report.overall if hasattr(res.report, "overall") else summarize(res.outcomes)

    # 扳机周期的序列：基准要在同一个周期上算，否则"一根"的含义都不一样
    trig_tf = rule.timeframes[rule.conditions[-1].role]
    counts: dict[str, int] = {}
    for s in res.signals:
        counts[s.symbol] = counts.get(s.symbol, 0) + 1

    total = sum(counts.values())
    base, parts, base_var = 0.0, [], 0.0
    for uid, n in sorted(counts.items()):
        trig_bars = aggregate(uid, series_1m[uid], trig_tf)
        exp, k, se_uid = random_entry_expectation(
            trig_bars, rule.emit.direction, params, stride)
        w = n / total if total else 0.0
        base += exp * w
        # 加权和的方差 = Σ wᵢ²·varᵢ。基准也是估计量，它的不确定性必须进区间。
        if not math.isnan(se_uid):
            base_var += (w * se_uid) ** 2
        parts.append((uid, n, exp, k))

    # **必须给标准误**：几十条信号时，"超额 +0.02% vs -0.04%" 可能全在噪声里。
    # 不给的话，很容易把一次抽样波动当成"改好了/改坏了"。
    #
    # **用库里那一份**（stats/baseline.py），它按持仓重叠折算了有效样本量。
    # 这里原来自己算了一遍 `stdev/sqrt(n)`，于是修了库里那份、CLI 还在报旧值 ——
    # 同一个量两处各写一套，迟早分家。
    rets = [o.ret for o in res.outcomes if o.reason is not ExitReason.NO_DATA]
    se_rule = standard_error(res.outcomes)
    # **区间画在超额上，所以标准误也得是超额的** —— 合成用库里那一份 excess_se，
    # 别在这里再写一遍 math.hypot（本文件上一次就是这么分家的）。
    se = excess_se(se_rule, math.sqrt(base_var))
    n_eff = effective_n(res.outcomes)

    return {"signals": total, "gross": st.avg_return, "win": st.win_rate,
            "base": base, "excess": st.avg_return - base, "parts": parts,
            "se": se, "se_rule": se_rule, "se_base": math.sqrt(base_var),
            "n_eval": len(rets), "n_eff": n_eff,
            "scanned": res.symbols_scanned, "trig_tf": trig_tf}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rule_id", nargs="?", help="规则 id；不给就配合 --all")
    ap.add_argument("--all", action="store_true", help="跑全部已启用的规则")
    ap.add_argument("--data-root", default=str(ROOT / "data" / "bars"))
    # **试变体不要改 config/rules/** —— 那是盯盘进程正在用的配置。
    # 把变体放别处、用这个参数指过去，实盘配置一个字都不用动
    # （我自己就因为忘了恢复，差点拿改过的配置报了一组错数）。
    ap.add_argument("--rules-dir", default=str(ROOT / "config" / "rules"),
                    help="规则目录；试变体时指向副本，别动 config/rules")
    ap.add_argument("--cost-bps", type=float, default=0.0, help="单边成本（基点）")
    ap.add_argument("--horizon", type=int, default=20, help="持有期（扳机周期根数）")
    ap.add_argument("--stride", type=int, default=10, help="基准抽样步长（越小越准越慢）")
    args = ap.parse_args()

    reg = load_registry(ROOT / "config")
    rules = {r.id: r for r in load_rules(pathlib.Path(args.rules_dir), registry=reg)}
    if args.all:
        targets = list(rules.values())
    elif args.rule_id in rules:
        targets = [rules[args.rule_id]]
    else:
        print(f"没有这条规则：{args.rule_id}；有的是 {sorted(rules)}")
        return 2

    data_root = pathlib.Path(args.data_root)
    # **只用有 1m 的标的**。没回补的标的不是"没触发"，是"没数据"，
    # 混在一起会让人以为规则变严了（这两个结论完全不同）。
    wanted = {u for r in targets for u in r.universe}
    series = {u: bars for u in sorted(wanted) if (bars := load_1m(data_root, u))}
    print(f"数据：{len(series)}/{len(wanted)} 个标的有 1m —— {', '.join(sorted(series))}\n")
    if not series:
        print("没有任何标的有 1m 数据，无法试算。")
        return 1

    # 止损口径走 OutcomeParams 的默认（ATR×1.5/×3.0）——
    # 这里曾经硬编码同样的值，面板那边却没传，两边算的不是一回事。
    params = OutcomeParams(horizon_bars=args.horizon, cost_bps=args.cost_bps)
    for rule in targets:
        r = evaluate_rule(rule, series, params, args.stride)
        print(f"── {rule.id}（{rule.emit.direction}，扳机 {r['trig_tf'].value}）")
        if not r["signals"]:
            print("   0 条信号\n")
            continue
        # **stats 里的比率一律是小数**（win_rate 0.5 = 50%、ret 0.001 = 0.1%），
        # 打印时统一 x100。少乘一次就会把 -0.07% 显示成 -0.0007%，看着像"几乎没差别"。
        print(f"   {r['signals']:>4} 条   胜率 {r['win'] * 100:.1f}%   "
              f"毛期望 {r['gross'] * 100:+.4f}%   加权基准 {r['base'] * 100:+.4f}%   "
              f"**超额 {r['excess'] * 100:+.4f}%**")
        # **区间必须画在超额上，不是毛期望上。** 判据是超额（CLAUDE.md），
        # 而这里曾经打印 `毛期望 ± 2·SE` —— 中心是另一个量。真被读错过：
        # breakout-short 的毛期望区间 +0.0015%~+0.0673% 不跨零，被当成
        # "超额显著为正"；平移到超额上是 -0.0020%~+0.0638%，其实跨零。
        # 面板（app.js renderExcess）一直是按超额画的，是这里跟它分了家。
        lo, hi = r["excess"] - 2 * r["se"], r["excess"] + 2 * r["se"]
        verdict = "跨零 —— 这份样本分辨不出" if lo < 0 < hi else (
            "**不跨零**" + ("（显著为正）" if lo > 0 else "（显著为负）"))
        print(f"        超额标准误 ±{r['se'] * 100:.4f}%"
              f"（规则 ±{r['se_rule'] * 100:.4f}% ⊕ 基准 ±{r['se_base'] * 100:.4f}%）"
              f"（n={r['n_eval']}，按持仓重叠折算后有效 n={r['n_eff']:.0f}）"
              f"　→ **超额**的 95% 区间约 {lo * 100:+.4f}% ~ {hi * 100:+.4f}%"
              f"　{verdict}")
        # 区间是保守的：规则的信号本来就是随机进场样本的子集，两者正相关，
        # 差值的真实标准误比这个小。贴着零的那种（如上例下界 -0.0020%）
        # 属于"这份样本还判不了"，不是"已经证伪"。
        for uid, n, exp, k in r["parts"]:
            print(f"        {uid:<28} {n:>3} 条   随机进场 {exp * 100:+.4f}%（{k} 个样本）")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
