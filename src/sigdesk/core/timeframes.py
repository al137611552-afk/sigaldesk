"""周期分桶。纯函数，无 IO、无当前时间依赖。

分桶规则由实测确定（docs/ARCHITECTURE.md §3.3）：

    **纯墙钟对齐 + 跳过空桶**

即一根 1m bar（close_ts = T）归属的高周期桶，其收盘时刻为
``ceil(T / period) * period``；某个桶内若没有任何 1m bar 则不产出该桶。

实测依据（rb8888，2026-08-27 交易日）：
  - 5m  在 10:15 休盘后的首根标 10:35（覆盖 10:30-10:35）
  - 15m 在 10:15 休盘后的首根标 10:45（覆盖 10:30-10:45）
  - 60m 日盘序列为 10:00 / 11:00 / 12:00 / 14:00 / 15:00，
    其中 12:00 桶实际只含 11:00-11:30 的 30 分钟数据，13:00 桶因无交易被跳过。

因北京时间是 UTC+8 整小时偏移、且各周期均整除 1 小时，可直接在 UTC epoch 上取整，
不需要时区换算 —— 这也让加密（UTC 对齐）与期货共用同一套代码。
"""

from __future__ import annotations

from .models import Timeframe


def bucket_close_ts(close_ts: int, timeframe: Timeframe) -> int:
    """返回 close_ts 所属高周期桶的收盘时间戳。

    右闭：恰好落在边界上的 bar 属于以该边界收盘的桶。
    """
    period = timeframe.seconds
    if period <= 0:
        raise ValueError(f"{timeframe} 不是固定长度周期，不能用墙钟分桶")
    return -(-close_ts // period) * period


def bucket_open_ts(bucket_close: int, timeframe: Timeframe) -> int:
    """桶的名义开始时间。注意这是**名义**区间左端，实际含有的交易时间可能更短
    （例：rb 的 12:00 这根 60m 桶只有 11:00-11:30 有交易）。"""
    period = timeframe.seconds
    if period <= 0:
        raise ValueError(f"{timeframe} 不是固定长度周期，不能用墙钟分桶")
    return bucket_close - period


def is_closed(close_ts: int, now_ts: int) -> bool:
    """INV-2：bar 已收盘 ⟺ 其收盘时刻已到达。

    数据源返回数组的最后一根恒为进行中 bar，数值还会变（CLAUDE.md 坑#2）。
    """
    return close_ts <= now_ts
