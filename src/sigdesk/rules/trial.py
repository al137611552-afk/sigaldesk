"""规则历史试算（PRD FR-5.3）：把一条规则拿去跑历史 bar，看它会在哪触发、值不值。

**刻意复用同一个引擎**（ADR-0001 单引擎三模式）：试算就是 replay 模式，
`RuleEngine.on_bars(store.push(bar))` 一根不差地照跑。另起一套"试算引擎"
必然产生第二份求值时机/去重/冷却，那是 M2 就明确拒绝过的路。

纯逻辑：吃内存里的 1m bar，吐信号与统计。取数（读 Parquet）在调用方。
试算**不带持久化状态** —— 去重表、冷却、状态机全是新的。这正是"试算"该有的语义：
它回答"这条规则在这段历史上会怎样"，不是"接着上次的运行状态继续"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..core.models import Bar, Timeframe
from ..stats.outcome import Outcome, OutcomeParams, evaluate_all
from ..stats.report import QualityReport, build_report
from ..store.bar_builder import aggregate
from ..store.bar_store import BarStore
from .engine import RuleEngine
from .model import Rule, Signal, store_timeframes


@dataclass(frozen=True, slots=True)
class Band:
    """某条件连续成立（或连续未知）的一段。画在图上就是「各级别条件成立区间」。"""

    role: str
    timeframe: str
    value: str  # "true" | "unknown"（"false" 是背景，不占字节）
    from_ts: int  # 该段第一根 bar 的 close_ts
    to_ts: int  # 该段最后一根 bar 的 close_ts
    bars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role, "timeframe": self.timeframe, "value": self.value,
            "from_ts": self.from_ts, "to_ts": self.to_ts, "bars": self.bars,
        }


class ConditionRecorder:
    """收集引擎旁路吐出来的条件判定，聚合成区间。

    记两套：`raw` 是表达式自己的真值，`satisfied` 是套上 mode 之后链路真正看到的。
    两者常常不同（event 只认跳变、window 看最近 within 根），而
    「表达式明明成立，链路却不认」正是"为什么没触发"最常见的答案 —— 得能分开看。
    """

    def __init__(self) -> None:
        # (symbol, role) -> [(close_ts, raw, satisfied)]
        self._points: dict[tuple[str, str], list[tuple[int, bool | None, bool | None]]] = {}
        self._tf: dict[tuple[str, str], str] = {}

    def __call__(
        self, rule_id: str, symbol: str, role: str, timeframe: Timeframe,
        close_ts: int, raw: bool | None, satisfied: bool | None,
    ) -> None:
        key = (symbol, role)
        self._points.setdefault(key, []).append((close_ts, raw, satisfied))
        self._tf[key] = timeframe.value

    def bands(self, *, which: str = "satisfied") -> dict[str, list[Band]]:
        """按标的给出各角色的区间。`which` = "satisfied"（链路视角）或 "raw"（表达式视角）。

        单遍扫描：只有**紧邻的同值点**才并成一段，中间隔了 false 就断开，
        否则图上会画出一条"从来没断过"的假带子。
        """
        raw_view = which == "raw"
        out: dict[str, list[Band]] = {}
        for (symbol, role), points in sorted(self._points.items()):
            tf = self._tf[(symbol, role)]
            runs: list[Band] = []
            prev_ts: int | None = None
            for ts, raw, satisfied in sorted(points):
                value = raw if raw_view else satisfied
                label = "true" if value is True else ("unknown" if value is None else "")
                if not label:
                    prev_ts = ts
                    continue
                if runs and runs[-1].value == label and runs[-1].to_ts == prev_ts:
                    runs[-1] = replace(runs[-1], to_ts=ts, bars=runs[-1].bars + 1)
                else:
                    runs.append(Band(role, tf, label, ts, ts, 1))
                prev_ts = ts
            out.setdefault(symbol, []).extend(runs)
        return out

    def counts(self) -> dict[str, dict[str, dict[str, int]]]:
        """每个 (标的, 角色) 的 成立/不成立/未知 根数。
        "0 条信号"时先看这张表：某一级全是 unknown 就是指标还没预热完。"""
        out: dict[str, dict[str, dict[str, int]]] = {}
        for (symbol, role), points in sorted(self._points.items()):
            tally = {"true": 0, "false": 0, "unknown": 0}
            for _, _, satisfied in points:
                tally["true" if satisfied else "unknown" if satisfied is None else "false"] += 1
            out.setdefault(symbol, {})[role] = tally
        return out


@dataclass(frozen=True, slots=True)
class TrialResult:
    signals: list[Signal]
    outcomes: list[Outcome]
    report: QualityReport
    bars_scanned: int
    symbols_scanned: list[str]
    # 有数据但没触发的标的，和压根没数据的标的，要分开说 ——
    # "0 条信号"到底是规则太严还是数据没回补，是两个完全不同的结论。
    symbols_without_data: list[str] = field(default_factory=list)
    warmup_bars: int = 0
    # {symbol: [Band]}，链路视角。图上画成各级别的成立区间带
    condition_bands: dict[str, list[Band]] = field(default_factory=dict)
    # {symbol: {role: {true/false/unknown: 根数}}}
    condition_counts: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "signals": [s.as_dict() for s in self.signals],
            "outcomes": [o.as_dict() for o in self.outcomes],
            "report": self.report.as_dict(),
            "bars_scanned": self.bars_scanned,
            "symbols_scanned": list(self.symbols_scanned),
            "symbols_without_data": list(self.symbols_without_data),
            "warmup_bars": self.warmup_bars,
            "condition_bands": {
                sym: [b.as_dict() for b in bands] for sym, bands in self.condition_bands.items()
            },
            "condition_counts": self.condition_counts,
        }


def derived_timeframes(rule: Rule) -> list[Timeframe]:
    """规则用到的高周期（不含恒存的 1m）。含 ``at()`` 跨级别引用到的那些。"""
    return store_timeframes([rule])


def run_trial(
    rule: Rule,
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    *,
    outcome_params: OutcomeParams | None = None,
    report_params: dict[str, Any] | None = None,
) -> TrialResult:
    """在给定的 1m 序列上跑一条规则。

    `bars_by_symbol` 的每个序列必须是 **1m、升序、closed**（回补链路本来就是）。
    多标的时按 close_ts 归并推进，与实盘同刻收盘的一批一起进引擎 ——
    否则大级别会被读成上一根，信号晚一拍（M2 踩过）。
    """
    universe = [u for u in rule.universe if u in bars_by_symbol] or list(bars_by_symbol)
    without_data = [u for u in rule.universe if not bars_by_symbol.get(u)]
    series = {u: list(bars_by_symbol.get(u, ())) for u in universe if bars_by_symbol.get(u)}

    store = BarStore(timeframes=derived_timeframes(rule))
    recorder = ConditionRecorder()
    # **试算无视 enabled。** `RuleEngine` 会过滤掉 `enabled: false` 的规则，
    # 而试算问的是"如果开了会怎样"—— enabled 是给盯盘用的开关，不是给回测的。
    # 不这么做的话：变体规则按惯例都写 enabled: false（免得误入盯盘），
    # 试算就**静默返回 0 条**，被读成"这条规则不触发"。踩过一次，查了半天。
    engine = RuleEngine([replace(rule, enabled=True)], store, recorder=recorder)

    merged: list[Bar] = sorted(
        (b for bars in series.values() for b in bars if b.closed),
        key=lambda b: (b.close_ts, b.symbol),
    )
    signals: list[Signal] = []
    i = 0
    while i < len(merged):
        j = i
        while j < len(merged) and merged[j].close_ts == merged[i].close_ts:
            j += 1
        closed_now: list[Bar] = []
        for bar in merged[i:j]:
            closed_now.extend(store.push(bar))
        if closed_now:
            signals.extend(engine.on_bars(closed_now))
        i = j

    # 评价用扳机周期的**完整**序列，从原始输入现聚合 —— **不能从 store 取**。
    #
    # 原来这里写的是 `store.view(uid, end).bars(trigger_tf)`，注释还说"给全量是
    # 安全的"。它不是全量：BarStore 有 MAX_BARS=5000 的内存上限，超出就裁掉最旧的。
    # 本地 cu2610 有 20460 根 1m，只剩最后 5000 根，于是早于窗口的信号在
    # evaluate_all 里二分落到 0，**静默拿窗口开头那几根当"未来"** ——
    # 三条相隔两周的信号算出一模一样的 entry/exit，报告上毫无异常。
    # 扳机是 1m 时最严重（5000 根只有三天半），5m 上加密也照样越界。
    #
    # aggregate 与 store 内部用的是同一个 builder，所以聚合结果一致；
    # 只是这里不受内存上限约束。evaluate_all 仍按 close_ts > fired_at 物理截断。
    trigger_tf = rule.timeframe
    future = {
        uid: bars if trigger_tf is Timeframe.M1 else aggregate(uid, bars, trigger_tf)
        for uid, bars in series.items()
    }
    outcomes = evaluate_all(signals, future, outcome_params)

    return TrialResult(
        signals=signals,
        outcomes=outcomes,
        report=build_report(outcomes, report_params),
        bars_scanned=len(merged),
        symbols_scanned=sorted(series),
        symbols_without_data=without_data,
        condition_bands=recorder.bands(),
        condition_counts=recorder.counts(),
    )


__all__ = ["Band", "ConditionRecorder", "TrialResult", "derived_timeframes", "run_trial"]
