"""K 线标记的分组与配对。纯逻辑，输入是 RuntimeStore 取出的行，输出是前端直接画的结构。

**为什么在服务端**：与 `/api/markers` 的分桶同理 —— 放前端 JS 里就没法单测，
而"密集处留谁当代表"和"哪两笔成交算同一笔交易"都是有口径的判断，不是画法。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..core.models import Timeframe
from ..rules.model import Priority, rank_key

# 离场类成交。entry 之外的都是离场 —— 用白名单会在新增 FillKind 时静默漏掉一种，
# 那笔交易就永远配不上对、图上只剩一个孤零零的开仓点。
_ENTRY = "entry"


def collapse(
    rows: Iterable[Mapping[str, Any]],
    chain_len: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """把落在同一根 bar、**同一方向**上的多条信号折成一枚标记。

    分组键是 ``(bucket_ts, direction)``，**方向不折叠**：同一根 bar 上多空同时触发
    是矛盾信息，红绿各留一枚才看得见；折成一个代表等于把矛盾藏起来。

    代表由 ``rank_key`` 选出（定义见 rules/model.py）。``members`` 保留全部成员并按
    同一顺序排好，前端展开时不用再排一遍 —— 两处各排一次迟早排出两种顺序。
    """
    lens = chain_len or {}
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((int(r["bucket_ts"]), str(r["direction"])), []).append(dict(r))

    out: list[dict[str, Any]] = []
    for (bucket, direction), members in groups.items():
        members.sort(key=lambda m: rank_key(
            tentative=bool(m.get("tentative")),
            priority=Priority.coerce(m.get("priority", "normal")),
            timeframe=Timeframe(str(m.get("timeframe", "1m"))),
            chain_len=lens.get(str(m["rule_id"]), 1),
            rule_id=str(m["rule_id"]),
        ))
        lead = members[0]
        out.append({
            "bucket_ts": bucket,
            "direction": direction,
            "fired_at": int(lead["fired_at"]),
            "rule_id": lead["rule_id"],
            "dedup_key": lead["dedup_key"],
            "trigger_price": float(lead["trigger_price"]),
            "priority": str(Priority.coerce(lead.get("priority", "normal"))),
            "count": len(members),
            "members": [{
                "rule_id": m["rule_id"],
                "dedup_key": m["dedup_key"],
                "fired_at": int(m["fired_at"]),
                "trigger_price": float(m["trigger_price"]),
                "priority": str(Priority.coerce(m.get("priority", "normal"))),
                "timeframe": str(m.get("timeframe", "")),
            } for m in members],
        })
    out.sort(key=lambda m: (m["bucket_ts"], m["direction"], m["dedup_key"]))
    return out


def pair_trades(
    fills: Iterable[Mapping[str, Any]],
    notional: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    """把成交配成"一笔交易"：开仓 + 它的离场。

    按 ``signal_key`` 分组 —— 一条信号进场、之后无论以止损/止盈/到期/强平哪种方式
    离场，都是同一笔。只有开仓、还没离场的算**持仓中**（``exit`` 为 None），照样返回：
    图上要画出"这笔还开着"，而不是等它平了才出现。

    ``pnl_pct`` 用 ``realized / 名义本金`` 而不是 ``(出-进)/进``：后者不含成本，
    与 ADR-0008 "成本按双边扣"的口径对不上，两个数放在一起会互相打脸。
    名义本金算不出来（独立只读模式没有 registry、拿不到合约乘数）时返回 **None**，
    前端显示破折号 —— 不是 0。算不出来显示成 0 是这个项目栽过好几次的坑。
    """
    by_signal: dict[str, list[dict[str, Any]]] = {}
    for f in fills:
        by_signal.setdefault(str(f["signal_key"]), []).append(dict(f))

    trades: list[dict[str, Any]] = []
    for key, group in by_signal.items():
        group.sort(key=lambda f: int(f["ts"]))
        entry = next((f for f in group if str(f["kind"]) == _ENTRY), None)
        if entry is None:
            continue  # 只有离场没有开仓：数据不全，画不出一笔交易
        exit_ = next(
            (f for f in group if str(f["kind"]) != _ENTRY and int(f["ts"]) >= int(entry["ts"])),
            None,
        )
        realized = float(exit_["realized"]) if exit_ else 0.0
        base = (notional or {}).get(key)
        trades.append({
            "signal_key": key,
            "side": str(entry["side"]),
            "entry": _leg(entry),
            "exit": _leg(exit_) if exit_ else None,
            "open": exit_ is None,
            "realized": realized if exit_ else None,
            "pnl_pct": (realized / base * 100.0) if (exit_ and base) else None,
        })
    trades.sort(key=lambda t: (t["entry"]["ts"], t["signal_key"]))
    return trades


def _leg(f: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ts": int(f["ts"]),
        "bucket_ts": int(f["bucket_ts"]),
        "price": float(f["price"]),
        "kind": str(f["kind"]),
        "side": str(f["side"]),
    }


__all__ = ["collapse", "pair_trades"]
