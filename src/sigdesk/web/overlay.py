"""K 线上的叠加线（均线）。纯逻辑，无 IO。

**为什么算在服务端**：图上画的均线必须和规则引擎看到的**是同一个数**。
自己在前端拿 close 数组重算一遍，看着一样，但口径一旦偏一点（ADR-0006：
EMA 用 SMA 播种、窗口未满返回 None 而不是 0），就会出现
「图上明明上穿了、规则却没触发」——这类问题查起来极其费劲。

所以这里直接复用 `indicators` 里引擎用的那两个类，不另写实现。
预热期返回 `None`（ADR-0006），前端**跳过**这些点，绝不能补 0 —— 补 0 会在图左端
画出一条从零飙起来的假线。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.models import Bar
from ..indicators.series import EMA, SMA

MAX_LINES = 6  # 图上画六条以上就没法看了，也防止有人用超长参数列表打服务端


@dataclass(frozen=True, slots=True)
class MovingAverage:
    kind: str  # "sma" | "ema"
    window: int
    values: list[float | None]  # 与传入的 bars 一一对应
    source: str = "close"  # "close" | "volume"

    @property
    def label(self) -> str:
        head = "V" if self.source == "volume" else ""
        return f"{head}{self.kind.upper()}{self.window}"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "window": self.window, "source": self.source,
                "label": self.label, "values": self.values}


def parse_spec(spec: str) -> list[tuple[str, int]]:
    """解析 `ma=5,10,20` 或 `ma=ema20,sma60`。默认 sma。

    非法项直接跳过而不是报错：图上少一条线，不该让整个取数请求失败。
    """
    out: list[tuple[str, int]] = []
    for raw in spec.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        kind = "sma"
        for prefix in ("sma", "ema"):
            if item.startswith(prefix):
                kind, item = prefix, item[len(prefix):]
                break
        if not item.isdigit():
            continue
        window = int(item)
        if 1 <= window <= 1000 and (kind, window) not in out:
            out.append((kind, window))
        if len(out) >= MAX_LINES:
            break
    return out


def moving_averages(
    bars: list[Bar], spec: str, source: str = "close"
) -> list[MovingAverage]:
    """按 spec 逐条算出均线。用引擎同款的 SMA/EMA，逐根 update，与实盘完全一致。

    ``source="volume"`` 出量能均线。**这条尤其要和引擎同源**：内置规则
    `volume-spike` 判的就是 `volume > sma(volume, 20) * 2.5` ——
    图上那条线要是算法不同，你就看不出它当时为什么触发。
    """
    wanted = parse_spec(spec)
    if not wanted or not bars:
        return []
    values = [b.volume for b in bars] if source == "volume" else [b.close for b in bars]
    out: list[MovingAverage] = []
    for kind, window in wanted:
        ind: SMA | EMA = SMA(window) if kind == "sma" else EMA(window)
        out.append(MovingAverage(kind, window, [ind.update(v) for v in values], source))
    return out


@dataclass(frozen=True, slots=True)
class RefMovingAverage:
    """**别的周期**的均线，对齐到当前图的 bar 上。

    用途：在 5m 图上叠一条 1h / 1d 均线，一眼看到大级别的方向和位置，
    不用切周期（"看大做小"最常做的那个动作）。
    """

    timeframe: str
    kind: str
    window: int
    values: list[float | None]   # 与当前图的 bars 一一对应

    @property
    def label(self) -> str:
        return f"{self.timeframe} {self.kind.upper()}{self.window}"

    def as_dict(self) -> dict[str, Any]:
        return {"timeframe": self.timeframe, "kind": self.kind, "window": self.window,
                "label": self.label, "values": self.values}


def parse_ref_spec(spec: str) -> list[tuple[str, str, int]]:
    """解析 `ref_ma=1h:ema20,1d:sma20` -> [(周期, 种类, 窗口)]。默认 sma。

    非法项跳过而不是报错 —— 与 parse_spec 同理：图上少一条线，
    不该让整个取数请求失败。
    """
    out: list[tuple[str, str, int]] = []
    for raw in spec.split(","):
        item = raw.strip().lower()
        if ":" not in item:
            continue
        tf, rest = item.split(":", 1)
        for kind, window in parse_spec(rest):
            if (tf, kind, window) not in out:
                out.append((tf, kind, window))
        if len(out) >= MAX_LINES:
            break
    return out[:MAX_LINES]


def align_as_of(
    bars: list[Bar], ref_bars: list[Bar], values: list[float | None]
) -> list[float | None]:
    """把高周期的均线值对齐到当前图的 bar 上，**严格 as-of**。

    一根 1h 均线的值，只有在那根 1h **收盘之后**才算已知。所以当前图上某根 bar
    取到的，是"收盘时刻 <= 它自己收盘时刻"的最后一根高周期 bar 的值。

    **不这样做就是在图上画未来**：把 1h 的值摊到它自己那一小时的每根 5m 上，
    等于让 09:00 的 5m bar 看到 09:59 才收盘的那根 1h 的均线 —— 复盘时会得出
    "当时明明看得出来"的错误结论，而实盘那一刻你根本看不到。
    这与 INV-1（as-of 视图物理截断未来）是同一条纪律。
    """
    out: list[float | None] = []
    i = -1
    n = len(ref_bars)
    for b in bars:
        while i + 1 < n and ref_bars[i + 1].close_ts <= b.close_ts:
            i += 1
        out.append(values[i] if i >= 0 else None)
    return out


def ref_moving_averages(
    bars: list[Bar], spec: str, load: Any
) -> list[RefMovingAverage]:
    """算出跨周期均线并对齐。``load(tf_value)`` 返回该周期的全部 bar。

    高周期序列由调用方提供（它才知道去哪儿读），这里保持无 IO。
    """
    wanted = parse_ref_spec(spec)
    if not wanted or not bars:
        return []
    out: list[RefMovingAverage] = []
    cache: dict[str, list[Bar]] = {}
    for tf, kind, window in wanted:
        if tf not in cache:
            cache[tf] = load(tf) or []
        ref = cache[tf]
        if not ref:
            continue
        ind: SMA | EMA = SMA(window) if kind == "sma" else EMA(window)
        vals = [ind.update(b.close) for b in ref]
        out.append(RefMovingAverage(tf, kind, window, align_as_of(bars, ref, vals)))
    return out


__all__ = [
    "MAX_LINES",
    "MovingAverage",
    "RefMovingAverage",
    "align_as_of",
    "moving_averages",
    "parse_ref_spec",
    "parse_spec",
    "ref_moving_averages",
]
