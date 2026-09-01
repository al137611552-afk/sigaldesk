"""OKX 实时 Feed：WebSocket 推送 + 断线后 REST 回补。

与期货轮询 Feed（feed/polling.py）的对称位置，但机制相反：
期货无推送只能定时轮询（坑#6），加密是事件驱动、7×24 不休。

实测契约（2026-08-28）：
- candle 频道在 **`/ws/v5/business`** 端点，不在 `/ws/v5/public`（订错端点会收到 60018 错误）。
- 同一根进行中 bar 每分钟被推送 30~80 次，**最后一次带 `confirm="1"`**，
  实测在该 bar 收盘后 **+0.5~1.2s** 到达 ⇒ 只认 confirm，不做本地时钟推断。
- 心跳：OKX 30s 无数据往来即断开。静默时发字符串 ``"ping"``（**不是 JSON**），
  对端回字符串 ``"pong"``（**不是 JSON**，用 json.loads 解会炸）。

分层同 CONVENTIONS：``parse_push`` / ``plan_backfill`` / ``reconnect_delay`` 是纯函数；
连接被抽象成 ``WsConnection`` 协议，因此重连与回补逻辑可以完全脱网单测。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import aiohttp

from ..core.models import Bar, Symbol, Timeframe
from .okx import OKX_BAR, OkxApiError, OkxRestClient, normalize_candles
from .polling import BarCursor

WS_BUSINESS_URL = "wss://ws.okx.com:8443/ws/v5/business"
# 心跳：官方要求 30s 内有往来，留出余量
PING_INTERVAL_S = 20.0
RECONNECT_BASE_S = 1.0
RECONNECT_CAP_S = 30.0


# ---------------------------------------------------------------- 纯逻辑部分


def subscribe_payload(inst_ids: Sequence[str], timeframe: Timeframe = Timeframe.M1) -> str:
    channel = f"candle{OKX_BAR[timeframe]}"
    args = [{"channel": channel, "instId": i} for i in inst_ids]
    return json.dumps({"op": "subscribe", "args": args})


def parse_push(text: str) -> tuple[str, list[list[str]]] | None:
    """一条 WS 文本 -> (instId, candle 原始行)。

    非行情消息返回 None：``"pong"`` 心跳回执、订阅回执 ``{"event":"subscribe"}``。
    服务端错误事件（如订错端点的 60018）抛 ``OkxApiError`` —— 静默忽略会变成"连着但永远没数据"。
    """
    if text == "pong":  # 心跳回执是裸字符串，不是 JSON
        return None
    try:
        msg: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        return None
    if msg.get("event") == "error":
        raise OkxApiError(f"OKX WS 错误 code={msg.get('code')} msg={msg.get('msg')}")
    if "data" not in msg:
        return None  # 订阅回执等
    inst_id = str(msg.get("arg", {}).get("instId", ""))
    return inst_id, [list(row) for row in msg["data"]]


def plan_backfill(
    last_close_ts: int | None, next_close_ts: int, period: int
) -> tuple[int, int] | None:
    """断线期间漏掉的区间 ``(start, end]``，无缺口返回 None。

    右端取 ``next_close_ts - period``：WS 已经把 next 那根带回来了，不必重复拉。
    区间约定与 ``OkxRestClient.fetch_range`` / ``parquet_io.read_range`` 一致（左开右闭）。
    """
    if last_close_ts is None or next_close_ts <= last_close_ts + period:
        return None
    return (last_close_ts, next_close_ts - period)


def reconnect_delay(
    attempt: int, *, base: float = RECONNECT_BASE_S, cap: float = RECONNECT_CAP_S
) -> float:
    """指数退避，封顶。attempt 从 0 起。"""
    return min(cap, base * 2.0**attempt)


# ---------------------------------------------------------------- IO 部分


class WsConnection(Protocol):
    """WS 连接的最小面。抽出来是为了让重连与回补逻辑可以用假连接单测。"""

    async def send_str(self, data: str) -> None: ...

    async def receive_str(self, timeout: float) -> str:
        """收一条文本。超时抛 ``TimeoutError``；对端关闭抛 ``ConnectionError``。"""
        ...


Connector = Callable[[], AbstractAsyncContextManager[WsConnection]]


class _AiohttpConnection:
    """把 aiohttp 的 WSMessage 适配成 ``WsConnection``。"""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws

    async def send_str(self, data: str) -> None:
        await self._ws.send_str(data)

    async def receive_str(self, timeout: float) -> str:
        msg = await self._ws.receive(timeout=timeout)
        if msg.type is aiohttp.WSMsgType.TEXT:
            return str(msg.data)
        raise ConnectionError(f"WS 连接结束: {msg.type}")


def default_connector(url: str = WS_BUSINESS_URL) -> Connector:
    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[WsConnection]:
        # 每次重连开一个新 session：重连是低频事件，换取连接状态干净
        async with aiohttp.ClientSession() as session, session.ws_connect(url) as ws:
            yield _AiohttpConnection(ws)

    return connect


class OkxWsFeed:
    """订阅一组永续合约的 candle 频道，产出已收盘 1m Bar。

    契约同 ``feed.base.Feed``：只产出 ``closed=True``、同一 (symbol, close_ts) 至多一次、
    按 close_ts 单调不减。断线重连后先用 REST 补齐缺口再续播，因此重连不会造成缺失，
    ``BarCursor`` 保证也不会造成重复。

    ``resume_from``（uid -> 已落盘的最新 close_ts）用于进程重启：不播种的话游标为空，
    第一根到达时判不出停机期间漏了什么，缺口就只能人工回补。
    """

    def __init__(
        self,
        symbols: Sequence[Symbol],
        *,
        rest: OkxRestClient | None = None,
        connect: Connector | None = None,
        timeframe: Timeframe = Timeframe.M1,
        ping_interval_s: float = PING_INTERVAL_S,
        max_reconnects: int | None = None,
        resume_from: dict[str, int] | None = None,
    ) -> None:
        for sym in symbols:
            if not sym.code:
                raise ValueError(f"{sym.uid} 缺少 code（OKX instId），无法订阅")
        self._by_inst = {s.code: s for s in symbols}
        self._rest = rest
        self._connect = connect or default_connector()
        self._tf = timeframe
        self._ping_interval_s = ping_interval_s
        self._max_reconnects = max_reconnects
        self._cursor = BarCursor()
        if resume_from:
            # 用已落盘的最新 close_ts 播种：进程重启造成的停机缺口与断线缺口走同一条回补路径
            self._cursor.seed(resume_from)
        self.reconnects = 0
        # 检测到的缺口一律记账；能补的（配了 rest）另记入 backfilled。
        # 两者分开，是为了让"有缺口但没补上"可见 —— 静默丢弃缺口是最难查的一类问题。
        self.gaps_detected: list[tuple[str, int, int]] = []
        self.backfilled: list[tuple[str, int, int]] = []

    async def _handle(self, text: str) -> list[Bar]:
        """一条消息 -> 本次应产出的 bar（含断线缺口的 REST 回补），已去重、已升序。"""
        parsed = parse_push(text)
        if parsed is None:
            return []
        inst_id, rows = parsed
        sym = self._by_inst.get(inst_id)
        if sym is None:
            return []  # 未订阅的标的，忽略
        period = self._tf.seconds
        out: list[Bar] = []
        for bar in normalize_candles(rows, symbol=sym.uid, timeframe=self._tf):
            span = plan_backfill(self._cursor.last_ts(sym.uid), bar.close_ts, period)
            if span is not None:
                self.gaps_detected.append((sym.uid, *span))
                if self._rest is not None:
                    filled = await self._rest.fetch_range(inst_id, sym.uid, self._tf, *span)
                    self.backfilled.append((sym.uid, *span))
                    out.extend(self._cursor.accept(filled))
            out.extend(self._cursor.accept([bar]))
        return out

    async def stream(self) -> AsyncIterator[Bar]:
        attempt = 0
        while True:
            try:
                async with self._connect() as conn:
                    await conn.send_str(subscribe_payload(list(self._by_inst), self._tf))
                    alive = False
                    while True:
                        try:
                            text = await conn.receive_str(self._ping_interval_s)
                        except TimeoutError:
                            await conn.send_str("ping")  # 静默期保活
                            continue
                        if not alive:
                            # 收到数据才算真连上才重置退避；否则"连上即断"会让退避永远归零，
                            # 变成猛敲对端的热循环
                            alive, attempt = True, 0
                        for bar in await self._handle(text):
                            yield bar
            except (ConnectionError, aiohttp.ClientError, OkxApiError):
                if self._max_reconnects is not None and self.reconnects >= self._max_reconnects:
                    return
                self.reconnects += 1
                await asyncio.sleep(reconnect_delay(attempt))
                attempt += 1


__all__ = [
    "PING_INTERVAL_S",
    "WS_BUSINESS_URL",
    "Connector",
    "OkxWsFeed",
    "WsConnection",
    "default_connector",
    "parse_push",
    "plan_backfill",
    "reconnect_delay",
    "subscribe_payload",
]
