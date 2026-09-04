"""随机进场基准与超额。

**为什么不能只看毛收益。** 样本区间本身有漂移（本地这份等权 +10.05%，
加密两个标的 +21% / +27%），任何多头规则在里面都显得好、任何空头都显得差。
判据必须是 **超额 = 规则期望 − 随机进场期望**。

**基准必须按该规则自己的信号分布加权。** 规则 A 的信号 80% 打在 BTC 上、
规则 B 的 80% 打在黄金上，拿同一个"全品种等权基准"去减，等于拿别人的行情当尺子。

随机进场 = 在该标的扳机周期的每一根 bar 上、以同方向、**同一套出场口径**开一笔，
取平均。它回答的是"在这段行情里闭着眼睛做，期望是多少"。
用同一个 `evaluate_all` 是关键 —— 两者之差才只包含"选时"，不掺口径差异。
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..core.models import Bar
from ..rules.model import Direction, Signal
from .outcome import ExitReason, Outcome, OutcomeParams, evaluate_all
from .report import summarize

# 抽样步长。全量要 O(bar 数 × 持有期)，几十万根在开发机上跑不完；
# 期望值抽样估计即可 —— 步长 10 仍有数千样本，标准误远小于我们关心的差异量级。
DEFAULT_STRIDE = 10


def random_entry_expectation(
    bars: Sequence[Bar], direction: Direction | str, params: OutcomeParams,
    stride: int = DEFAULT_STRIDE,
) -> tuple[float, int]:
    """在抽样到的每一根 bar 上开一笔同方向的单，返回 (平均收益, 样本数)。"""
    if not bars:
        return 0.0, 0
    picked = list(bars[::max(1, stride)])
    fake = [
        Signal(
            rule_id="__random__", symbol=b.symbol, direction=direction,  # type: ignore[arg-type]
            timeframe=b.timeframe, fired_at=b.close_ts, trigger_price=b.close,
            dedup_key=f"r{i}",
        )
        for i, b in enumerate(picked)
    ]
    if not fake:
        return 0.0, 0
    st = summarize(evaluate_all(fake, {bars[0].symbol: list(bars)}, params))
    return st.avg_return, st.evaluated


def effective_n(outcomes: Sequence[Outcome]) -> float:
    """按**持仓重叠**折算的有效样本量。

    `stdev/sqrt(n)` 假设每条信号相互独立，但**持有期比冷却期长时它们不独立** ——
    同一段价格变动会被好几条信号同时吃到。实例：冷却 30 分钟、持有 100 分钟，
    同一段行情最多被 3.3 条信号共用，名义 n=250 的真实信息量只有约 76。

    做法：对每条信号数一数「同一标的、持仓区间与它相交」的条数（含自己），
    取平均得到重叠倍数 m，`n_eff = n / m`。这是 Newey-West/HAC 的一个朴素近似 ——
    它只算**同标的**的重叠，跨标的的相关性（行情齐涨齐跌）没有计入，
    所以给出的仍是**乐观**估计，只是没原来那么乐观。
    """
    usable = [o for o in outcomes if o.reason is not ExitReason.NO_DATA]
    if len(usable) < 2:
        return float(len(usable))
    by_sym: dict[str, list[tuple[int, int]]] = {}
    for o in usable:
        by_sym.setdefault(o.symbol, []).append((o.entry_ts, max(o.exit_ts, o.entry_ts)))
    total = 0
    for spans in by_sym.values():
        spans.sort()
        starts = [a for a, _ in spans]
        for a, b in spans:
            # 与 [a, b] 相交 = 起点 <= b **且** 终点 >= a。
            # 起点已排序，所以候选是前 lo 个；终点没排序，逐个判。
            lo = bisect.bisect_right(starts, b)
            total += sum(1 for k in range(lo) if spans[k][1] >= a)
    m = total / len(usable)
    return len(usable) / max(1.0, m)


def standard_error(outcomes: Sequence[Outcome]) -> float:
    """每条信号净收益的标准误，**按持仓重叠折算过有效样本量**。

    **面板必须给它。** 「胜率 45.5%」看着像事实，但 44 条信号的 95% 区间约 ±15 个
    百分点 —— 不给不确定性，就会把抽样噪声当成结论（这一轮反复发生过）。

    而不折算重叠的话它会**系统性偏小**：扳机换到 1m 后名义 n=250、SE ±0.0073%，
    看着像 6.7 个标准误的强证据，按重叠折算后只有 3.7 个。
    """
    rets = [o.ret for o in outcomes if o.reason is not ExitReason.NO_DATA]
    if len(rets) < 2:
        return float("nan")
    return statistics.stdev(rets) / math.sqrt(max(1.0, effective_n(outcomes)))


@dataclass(frozen=True, slots=True)
class Baseline:
    """随机进场基准。``excess`` 才是判据。"""

    avg_return: float = 0.0          # 按信号分布加权后的随机进场期望
    excess: float = 0.0              # 规则期望 − 基准
    samples: int = 0                 # 参与估计的随机进场笔数
    se: float = float("nan")         # 规则期望的标准误
    by_symbol: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "avg_return": self.avg_return,
            "excess": self.excess,
            "samples": self.samples,
            "se": None if math.isnan(self.se) else self.se,
            "by_symbol": dict(self.by_symbol),
        }


def weighted_baseline(
    outcomes: Sequence[Outcome],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    params: OutcomeParams,
    stride: int = DEFAULT_STRIDE,
) -> Baseline:
    """按信号分布加权的随机进场基准。

    权重是 {标的: 该标的上的信号数}。方向按每个标的上**该标的信号的多数方向**取
    —— 同一标的上多空混杂时，用多数方向近似（精确做法要按方向分开算基准，
    但那会让权重更碎、估计更不稳）。
    """
    usable = [o for o in outcomes if o.reason is not ExitReason.NO_DATA]
    if not usable:
        return Baseline()

    counts: dict[str, int] = {}
    dirs: dict[str, dict[Direction, int]] = {}
    for o in usable:
        counts[o.symbol] = counts.get(o.symbol, 0) + 1
        dirs.setdefault(o.symbol, {})[o.direction] = dirs.setdefault(o.symbol, {}).get(
            o.direction, 0
        ) + 1

    total = sum(counts.values())
    base, samples, per_symbol = 0.0, 0, {}
    for uid, n in counts.items():
        bars = bars_by_symbol.get(uid) or []
        if not bars:
            continue
        direction = max(dirs[uid].items(), key=lambda kv: kv[1])[0]
        exp, k = random_entry_expectation(bars, direction, params, stride)
        per_symbol[uid] = exp
        base += exp * n / total
        samples += k

    rule_exp = summarize(usable).avg_return
    return Baseline(
        avg_return=base, excess=rule_exp - base, samples=samples,
        se=standard_error(usable), by_symbol=per_symbol,
    )


# 持有期敏感性用的持有期梯子。前密后疏 —— 短持有期之间的差别更值得看，
# 而 60 根之后曲线通常已经平了。
HORIZON_LADDER: tuple[int, ...] = (1, 2, 3, 5, 8, 10, 15, 20, 30, 40, 60, 80, 100)

# 扫梯子时用的抽样步长。比单点评估的 DEFAULT_STRIDE 粗 —— 曲线只需要形状，
# 不需要每一点都精确。实测 13 个持有期：步长 10 要 1.6s，步长 40 只要 0.44s。
CURVE_STRIDE = 40


def horizon_curve(
    signals: Sequence[Signal],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    params: OutcomeParams,
    ladder: Sequence[int] = HORIZON_LADDER,
) -> list[dict[str, Any]]:
    """期望随持有期怎么变。

    **同时给毛期望和超额**：基准本身也随持有期变（持得越久，行情漂移累积得越多），
    只画毛期望会把"市场在涨"误读成"规则在长持有期上更好"。

    梯子里超过实际可评价范围的点自然会样本变少，如实带上 `evaluated` 让前端标出来。
    """
    out: list[dict[str, Any]] = []
    for h in ladder:
        p = replace(params, horizon_bars=h)
        outs = evaluate_all(list(signals), {k: list(v) for k, v in bars_by_symbol.items()}, p)
        b = weighted_baseline(outs, bars_by_symbol, p, CURVE_STRIDE)
        st = summarize([o for o in outs if o.reason is not ExitReason.NO_DATA])
        out.append({
            "bars": h, "avg_return": st.avg_return,
            "baseline": b.avg_return, "excess": b.excess, "evaluated": st.evaluated,
        })
    return out


__all__ = [
    "CURVE_STRIDE", "HORIZON_LADDER", "Baseline", "effective_n", "horizon_curve",
    "random_entry_expectation", "standard_error", "weighted_baseline",
]
