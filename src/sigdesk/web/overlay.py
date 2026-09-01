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


__all__ = ["MAX_LINES", "MovingAverage", "moving_averages", "parse_spec"]
