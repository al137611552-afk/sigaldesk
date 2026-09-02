"""预警组。纯逻辑：给定「钉住的」和「最近触发的」，算出九个格子里放什么。

**这个组不是一份需要维护的名单。** 最容易写坏的版本是"维护一个数组，新信号 push，
满了 shift"，那样立刻要处理重复加入、手动移除后又被自动加回、重启后错位、淘汰顺序……
一堆状态同步 bug。

这里改成：

    组 = 钉住的（按钉住先后）∪ 最近触发信号的标的（按时间倒序），取前 N，钉住的在前

于是**只有「钉住」是持久状态**，其余每次算出来。淘汰是自然发生的，没有淘汰逻辑，
也就没有淘汰 bug；重启后自动重建，不会状态漂移。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

SLOTS = 9


def latest_by_symbol(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把信号流压成「一个标的一条」，只留最新的那条，按时间倒序。

    一个标的连着触发五次，在预警组里仍然只占一格 —— 它占五格的话，
    九格会被一个躁动的品种吃光，而那恰恰是最不需要盯的情况。
    """
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        uid = str(r["symbol"])
        fired = int(r["fired_at"])
        cur = best.get(uid)
        if cur is None or fired > int(cur["fired_at"]):
            best[uid] = dict(r)
    return sorted(best.values(), key=lambda r: (-int(r["fired_at"]), str(r["symbol"])))


def build_group(
    pinned: Sequence[str],
    recent: Sequence[Mapping[str, Any]],
    slots: int = SLOTS,
) -> list[dict[str, Any]]:
    """算出预警组。``recent`` 需已按时间倒序、一个标的一条（见 ``latest_by_symbol``）。

    钉住的**永远排在前面且永不被挤掉** —— 那是人工判断"还需要观察"的唯一表达，
    被新信号挤掉的话这个功能就白做了。钉住的数量超过槽位时，多出来的**照样返回**，
    由调用方如实告诉用户"钉住的比格子多"，而不是在这里悄悄砍掉一半。
    """
    by_uid = {str(r["symbol"]): r for r in recent}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for uid in pinned:
        if uid in seen:
            continue          # 同一个标的钉两次：容错，不重复占格
        seen.add(uid)
        out.append(_entry(uid, by_uid.get(uid), pinned=True))

    for r in recent:
        uid = str(r["symbol"])
        if uid in seen:
            continue
        if len(out) >= slots:
            break
        seen.add(uid)
        out.append(_entry(uid, r, pinned=False))

    return out


def _entry(uid: str, row: Mapping[str, Any] | None, *, pinned: bool) -> dict[str, Any]:
    """一格的内容。没有信号的钉住项 ``rule_id`` 为 None ——
    前端据此显示「手动钉住」，而不是编一条不存在的规则。"""
    return {
        "symbol": uid,
        "pinned": pinned,
        "rule_id": None if row is None else str(row["rule_id"]),
        "direction": None if row is None else str(row["direction"]),
        "fired_at": None if row is None else int(row["fired_at"]),
        "dedup_key": None if row is None else str(row["dedup_key"]),
        "trigger_price": None if row is None else float(row["trigger_price"]),
    }


__all__ = ["SLOTS", "build_group", "latest_by_symbol"]
