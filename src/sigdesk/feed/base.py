"""Feed 抽象。期货轮询、加密 WS、历史回放三种实现共用同一出口（ADR-0001）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from ..core.models import Bar


class Feed(Protocol):
    """产出 1m Bar 的事件流。

    约定：
    - 只产出 ``closed=True`` 的 bar（INV-2）。盘中未收盘的值由各实现自行丢弃。
    - 同一 (symbol, close_ts) 至多产出一次；断线重连后的补齐不得重复产出。
    - 按 close_ts 单调不减产出，便于下游 BarBuilder 直接消费。
    """

    def stream(self) -> AsyncIterator[Bar]: ...
