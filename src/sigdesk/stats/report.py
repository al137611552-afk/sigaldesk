"""信号质量报告：把一堆 Outcome 汇总成能下判断的数字。纯逻辑。

**可复现是硬要求**（M3 验收）：本模块不读时钟、不用集合迭代顺序、分组键一律排序输出，
因此同一批输入必然得到逐字节相同的报告。
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.models import CST, Market
from ..rules.model import Direction
from .outcome import ExitReason, Outcome


def _pct(part: int, whole: int) -> float:
    return 0.0 if whole == 0 else part / whole


def _mean(xs: Sequence[float]) -> float:
    return 0.0 if not xs else math.fsum(xs) / len(xs)


def _median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    ordered = sorted(xs)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


@dataclass(frozen=True, slots=True)
class Stats:
    """一组信号的质量指标。收益率均为小数（0.01 = 1%）。"""

    signals: int = 0  # 触发次数（含无法评价的）
    evaluated: int = 0  # 能评价的（信号之后有足够 bar）
    directional: int = 0  # 参与胜率统计的（排除 neutral）
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0  # 期望收益（每条信号的平均净收益率）
    median_return: float = 0.0
    total_return: float = 0.0  # 等权累加，不是复利
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff: float = 0.0  # 盈亏比 = |avg_win / avg_loss|
    false_rate: float = 0.0  # 假信号率 = 先打止损的比例
    target_rate: float = 0.0
    horizon_rate: float = 0.0
    avg_mfe: float = 0.0
    avg_mae: float = 0.0
    avg_bars_held: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize(outcomes: Sequence[Outcome]) -> Stats:
    """汇总。**neutral 信号不参与胜率与收益**（它是"去看一眼"的提示，不是方向判断），
    但仍计入 signals/evaluated 与 MFE/MAE —— 那些对它是有意义的。"""
    if not outcomes:
        return Stats()
    evaluated = [o for o in outcomes if o.evaluated]
    directional = [o for o in evaluated if o.direction is not Direction.NEUTRAL]
    rets = [o.ret for o in directional]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    avg_win, avg_loss = _mean(wins), _mean(losses)
    return Stats(
        signals=len(outcomes),
        evaluated=len(evaluated),
        directional=len(directional),
        wins=len(wins),
        losses=len(losses),
        win_rate=_pct(len(wins), len(directional)),
        avg_return=_mean(rets),
        median_return=_median(rets),
        total_return=math.fsum(rets),
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff=abs(avg_win / avg_loss) if avg_loss else 0.0,
        false_rate=_pct(sum(o.reason is ExitReason.STOP for o in directional), len(directional)),
        target_rate=_pct(sum(o.reason is ExitReason.TARGET for o in directional), len(directional)),
        horizon_rate=_pct(
            sum(o.reason is ExitReason.HORIZON for o in directional), len(directional)
        ),
        avg_mfe=_mean([o.mfe for o in evaluated]),
        avg_mae=_mean([o.mae for o in evaluated]),
        avg_bars_held=_mean([float(o.bars_held) for o in evaluated]),
    )


def local_hour(outcome: Outcome) -> int:
    """信号触发的**市场本地**小时（NFR-5：展示层按市场本地时区渲染）。

    期货看北京时间才有意义（夜盘 21:00 与日盘 09:00 是完全不同的时段）；加密 7×24 用 UTC。
    """
    tz = dt.UTC if outcome.symbol.startswith(Market.CRYPTO.value) else CST
    return dt.datetime.fromtimestamp(outcome.fired_at, tz).hour


def group_by(
    outcomes: Sequence[Outcome], key: Callable[[Outcome], Any]
) -> dict[Any, Stats]:
    """按 key 分组汇总。**输出按键排序**，保证报告逐字节可复现。"""
    buckets: dict[Any, list[Outcome]] = {}
    for o in outcomes:
        buckets.setdefault(key(o), []).append(o)
    return {k: summarize(buckets[k]) for k in sorted(buckets)}


@dataclass(frozen=True, slots=True)
class QualityReport:
    """完整报告：总体 + 三个维度的分布。"""

    overall: Stats
    by_rule: dict[str, Stats] = field(default_factory=dict)
    by_symbol: dict[str, Stats] = field(default_factory=dict)
    by_hour: dict[int, Stats] = field(default_factory=dict)
    by_direction: dict[str, Stats] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall.as_dict(),
            "by_rule": {k: v.as_dict() for k, v in self.by_rule.items()},
            "by_symbol": {k: v.as_dict() for k, v in self.by_symbol.items()},
            "by_hour": {str(k): v.as_dict() for k, v in self.by_hour.items()},
            "by_direction": {k: v.as_dict() for k, v in self.by_direction.items()},
            "params": dict(self.params),
        }


def build_report(
    outcomes: Sequence[Outcome], params: dict[str, Any] | None = None
) -> QualityReport:
    """把 Outcome 列表汇总成报告。同一批输入必然得到相同结果（M3 验收：可复现）。

    ``params`` 是生成这批结果时用的统计口径，原样带进报告 ——
    一份不写明口径的胜率没有意义（20 根持有期和 200 根能差出天壤之别）。
    """
    return QualityReport(
        overall=summarize(outcomes),
        by_rule=group_by(outcomes, lambda o: o.rule_id),
        by_symbol=group_by(outcomes, lambda o: o.symbol),
        by_hour=group_by(outcomes, local_hour),
        by_direction=group_by(outcomes, lambda o: str(o.direction)),
        params=dict(params or {}),
    )


def format_report(report: QualityReport, top: int = 10) -> str:
    """给终端看的纯文本报告。"""
    o = report.overall
    lines = [
        "信号质量报告",
        f"  口径: {report.params or '默认'}",
        f"  触发 {o.signals} 条，可评价 {o.evaluated} 条，方向性 {o.directional} 条",
        f"  胜率 {o.win_rate:.1%}  期望收益 {o.avg_return:+.3%}  中位 {o.median_return:+.3%}",
        f"  盈亏比 {o.payoff:.2f}（均盈 {o.avg_win:+.3%} / 均亏 {o.avg_loss:+.3%}）",
        f"  假信号率(先打止损) {o.false_rate:.1%}  止盈 {o.target_rate:.1%}  "
        f"到期 {o.horizon_rate:.1%}",
        f"  平均最大浮盈 {o.avg_mfe:+.3%}  平均最大浮亏 {o.avg_mae:+.3%}  "
        f"平均持有 {o.avg_bars_held:.1f} 根",
    ]
    for title, group in (
        ("分规则", report.by_rule),
        ("分品种", report.by_symbol),
        ("分方向", report.by_direction),
    ):
        if not group:
            continue
        lines.append(f"  {title}:")
        for k, st in list(group.items())[:top]:
            lines.append(
                f"    {k:<32} {st.signals:>4} 条  胜率 {st.win_rate:>6.1%}  "
                f"期望 {st.avg_return:>+8.3%}"
            )
    if report.by_hour:
        lines.append("  分时段（市场本地时间）:")
        for hour, st in report.by_hour.items():
            lines.append(
                f"    {hour:02d}:00  {st.signals:>4} 条  胜率 {st.win_rate:>6.1%}  "
                f"期望 {st.avg_return:>+8.3%}"
            )
    return "\n".join(lines)


__all__ = [
    "QualityReport",
    "Stats",
    "build_report",
    "format_report",
    "group_by",
    "local_hour",
    "summarize",
]
