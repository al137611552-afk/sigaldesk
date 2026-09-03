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

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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


def standard_error(outcomes: Sequence[Outcome]) -> float:
    """每条信号净收益的标准误。

    **面板必须给它。** 「胜率 45.5%」看着像事实，但 44 条信号的 95% 区间约 ±15 个
    百分点 —— 不给不确定性，就会把抽样噪声当成结论（这一轮反复发生过）。
    """
    rets = [o.ret for o in outcomes if o.reason is not ExitReason.NO_DATA]
    if len(rets) < 2:
        return float("nan")
    return statistics.stdev(rets) / math.sqrt(len(rets))


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


__all__ = ["Baseline", "random_entry_expectation", "standard_error", "weighted_baseline"]
