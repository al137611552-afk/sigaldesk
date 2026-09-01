"""期货主连拼接：用 `main-by-date` 的换月区间 + **真实合约**数据合成连续序列。

为什么要自己拼（CLAUDE.md 坑#9）：数据源的 rb8888 主连，同一时段 1m 数据
by-count 与 by-timerange 有 272/345 根不一致（75 根价格不一致），而真实合约
rb2610 仅 2/345 根微差 —— 主连口径不可复现，**禁止进入回测与统计**。
所以跨换月的回测只有一条路：拿真实合约自己拼。

本模块是**纯逻辑**：吃「换月区间 + 各合约的 bar」，吐拼好的 bar。
取数（调接口、读 Parquet）在 `scripts/build_continuous.py`。

拼接口径：
- **后复权价差**（默认 `AdjustMode.BACK_DIFF`）：最新一段保留真实价格，
  历史各段整体平移，使换月锚点处两合约收盘价相等。指标（EMA/ATR/BOLL）
  看到的是连续价格，不会在换月处吃到一个假跳空。
- `AdjustMode.NONE`：原样拼接，换月处有真实跳空。只适合看图，不适合跑指标。

**平移量记进产物元数据**（`StitchResult.rollovers`），不是算完就扔 ——
回测出了怪结果要能回答"是不是换月平移造成的"。

已知限制（别拿它当真实可交易序列）：
- 平移后的历史价格**不是当时的真实成交价**，绝对价位无意义，只有形态与价差有意义。
- 拼接序列**不可下单**：它不对应任何一个真实合约。`Registry.tradable()` 已排除。
- `volume/money/open_interest` 取源合约原值**不平移** —— money 与平移后的价格
  自然对不上，这是价差复权的固有代价，不是 bug。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from ..core.models import Bar


class StitchError(ValueError):
    """拼接前提不成立。一律 fail-fast —— 拼错的序列会静默污染整段回测。"""


class AdjustMode(StrEnum):
    BACK_DIFF = "back_diff"  # 后复权价差平移（默认）
    NONE = "none"  # 原样拼接，换月处保留真实跳空


@dataclass(frozen=True, slots=True)
class MainSegment:
    """某真实合约作为主力的生效区间（交易日，两端均含）。"""

    contract: str  # 数据源合约代码，如 rb2610
    start_day: str  # YYYY-MM-DD
    end_day: str  # YYYY-MM-DD

    def __post_init__(self) -> None:
        if self.end_day < self.start_day:
            raise StitchError(f"{self.contract} 的生效区间倒置: {self.start_day}~{self.end_day}")


@dataclass(frozen=True, slots=True)
class Rollover:
    """一次换月，以及它引入的平移量。产物元数据，用于事后归因。"""

    at_day: str  # 新合约生效的第一个交易日
    from_contract: str
    to_contract: str
    anchor_ts: int  # 用来测价差的那根 bar 的 close_ts（两合约都有成交）
    diff: float  # 新 - 旧，本次换月单独的价差
    cum_offset: float  # 施加到**旧合约及其之前所有段**的累计平移量


@dataclass(frozen=True, slots=True)
class StitchResult:
    bars: list[Bar]
    rollovers: list[Rollover]  # 时间升序
    adjust: AdjustMode


def parse_main_segments(rows: Iterable[Mapping[str, object]], main_code: str) -> list[MainSegment]:
    """把 `main-by-date` 的响应行转成换月区间。

    实测响应形如：
    `{"main_variety_code": "rb9999", "variety_code": "rb2610",
      "start_date": "2026-04-08 00:00:00", "end_date": "2026-08-31 00:00:00"}`
    —— 多品类是**一个平铺列表**，按 `main_variety_code` 自行分组。

    坑：`main_variety_codes` 要传 **`rb9999`**，传 `rb` 或 `rb8888` 会返回
    `{"code": 0, "data": null}` —— 成功状态码 + 空数据，**不报错**。
    所以这里对空结果直接抛，不返回空列表。
    """
    segs = [
        MainSegment(
            contract=str(r["variety_code"]),
            start_day=str(r["start_date"])[:10],
            end_day=str(r["end_date"])[:10],
        )
        for r in rows
        if str(r.get("main_variety_code")) == main_code
    ]
    if not segs:
        raise StitchError(
            f"main-by-date 没有返回 {main_code} 的换月区间。注意该接口要的是 9999 指数代码"
            f"（如 rb9999），传 rb / rb8888 会返回 code:0 + data:null，不报错。"
        )
    segs.sort(key=lambda s: s.start_day)
    for prev, cur in zip(segs, segs[1:], strict=False):
        if cur.start_day <= prev.end_day:
            raise StitchError(
                f"{main_code} 的换月区间重叠: {prev.contract}({prev.end_day}) "
                f"与 {cur.contract}({cur.start_day})"
            )
    return segs


def _closes_by_ts(bars: Sequence[Bar]) -> dict[int, float]:
    return {b.close_ts: b.close for b in bars}


def _anchor(old: Sequence[Bar], new: Sequence[Bar], boundary_day: str) -> tuple[int, float]:
    """换月锚点：**两个合约都有成交**的最后一根 bar（在新合约生效日之前）。

    必须同根比，不能拿"旧合约最后一根"对"新合约第一根" —— 那两根差着一整夜，
    价差里混进了隔夜跳空，平移量就错了。
    """
    new_closes = _closes_by_ts(new)
    common = sorted(
        ts
        for b in old
        if (ts := b.close_ts) in new_closes and (b.trading_day or "") < boundary_day
    )
    if not common:
        raise StitchError(
            f"换月到 {boundary_day} 时，新旧合约没有共同的 bar，算不出价差。"
            f"请先把两个合约在换月前的重叠区间都回补齐（新合约在成为主力前就已经在交易）。"
        )
    ts = common[-1]
    old_close = next(b.close for b in old if b.close_ts == ts)
    return ts, new_closes[ts] - old_close


def _slice(bars: Sequence[Bar], seg: MainSegment) -> list[Bar]:
    """取该合约在生效区间内的 bar。按 trading_day 切，不按自然日 ——
    夜盘归属下一交易日，按自然日切会把换月当晚的夜盘切错边。"""
    return [b for b in bars if b.trading_day and seg.start_day <= b.trading_day <= seg.end_day]


def stitch(
    segments: Sequence[MainSegment],
    bars_by_contract: Mapping[str, Sequence[Bar]],
    *,
    symbol: str,
    adjust: AdjustMode = AdjustMode.BACK_DIFF,
) -> StitchResult:
    """按换月区间拼接真实合约，产出连续序列。

    `bars_by_contract` 的 bar 必须已按 close_ts 升序（回补链路本来就是升序）。
    """
    if not segments:
        raise StitchError("没有换月区间，无法拼接")
    missing = [s.contract for s in segments if not bars_by_contract.get(s.contract)]
    if missing:
        raise StitchError(
            f"缺少这些合约的数据: {', '.join(missing)}。"
            f"拼接不会跳过缺失段 —— 跳过等于凭空产生一个大跳空。"
        )

    pieces = [_slice(bars_by_contract[s.contract], s) for s in segments]
    empty = [s.contract for s, p in zip(segments, pieces, strict=True) if not p]
    if empty:
        raise StitchError(f"这些合约在其主力区间内没有任何 bar: {', '.join(empty)}")

    # 后复权：从最新一段往回累加价差，最新一段偏移为 0（保留真实价格）
    rollovers: list[Rollover] = []
    offsets = [0.0] * len(segments)
    if adjust is AdjustMode.BACK_DIFF:
        cum = 0.0
        for i in range(len(segments) - 2, -1, -1):
            boundary = segments[i + 1].start_day
            ts, diff = _anchor(
                bars_by_contract[segments[i].contract],
                bars_by_contract[segments[i + 1].contract],
                boundary,
            )
            cum += diff
            offsets[i] = cum
            rollovers.append(
                Rollover(
                    at_day=boundary,
                    from_contract=segments[i].contract,
                    to_contract=segments[i + 1].contract,
                    anchor_ts=ts,
                    diff=diff,
                    cum_offset=cum,
                )
            )
        rollovers.reverse()

    out: list[Bar] = []
    for piece, offset in zip(pieces, offsets, strict=True):
        for b in piece:
            if offset and b.low + offset <= 0:
                raise StitchError(
                    f"{b.trading_day} 平移后价格变成非正数（{b.low:.2f} {offset:+.2f}）—— "
                    f"价差复权在长历史上会击穿零点。请缩短区间，或改用 adjust=none。"
                )
            out.append(
                replace(
                    b,
                    symbol=symbol,
                    open=b.open + offset,
                    high=b.high + offset,
                    low=b.low + offset,
                    close=b.close + offset,
                )
                if offset
                else replace(b, symbol=symbol)
            )

    # 拼完必须严格递增：区间算错会表现为时间倒流，宁可当场炸
    for prev, cur in zip(out, out[1:], strict=False):
        if cur.close_ts <= prev.close_ts:
            raise StitchError(f"拼接结果时间未严格递增: {prev.close_ts} -> {cur.close_ts}")
    return StitchResult(bars=out, rollovers=rollovers, adjust=adjust)


def rollover_index(rollovers: Sequence[Rollover]) -> list[int]:
    """换月锚点时间戳（升序），便于回测标注换月位置。"""
    return sorted(r.anchor_ts for r in rollovers)


def contracts_needed(segments: Sequence[MainSegment]) -> list[str]:
    seen: list[str] = []
    for s in segments:
        if s.contract not in seen:
            seen.append(s.contract)
    return seen


__all__ = [
    "AdjustMode",
    "MainSegment",
    "Rollover",
    "StitchError",
    "StitchResult",
    "contracts_needed",
    "parse_main_segments",
    "rollover_index",
    "stitch",
]
