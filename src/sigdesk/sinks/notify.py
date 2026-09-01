"""信号推送出口（ARCHITECTURE §0：Sink 是唯一有副作用的地方）。

分两半：``format_signal`` 是纯函数、可单测；``TelegramNotifier`` / ``BarkNotifier``
只负责 HTTP。``MultiNotifier`` 保证**一个渠道挂掉不影响其他渠道** ——
推送失败绝不能反过来打断行情处理，所以异常在这里被吞掉并计数，不向上抛。
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import aiohttp

from ..core.models import CST, Market
from ..rules.model import Direction, Signal

_ARROW = {Direction.LONG: "▲ 多", Direction.SHORT: "▼ 空", Direction.NEUTRAL: "● 提示"}


def _fmt_value(value: float | None) -> str:
    if value is None:
        return "—"  # 预热期没有值，显示破折号而不是 0（ADR-0006）
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    return f"{value:.4g}"


def _fmt_time(signal: Signal) -> str:
    """期货用北京时间显示（用户看盘就是这个时区），加密用 UTC。"""
    if signal.symbol.startswith(Market.CRYPTO.value):
        return dt.datetime.fromtimestamp(signal.fired_at, dt.UTC).strftime("%m-%d %H:%M UTC")
    local = dt.datetime.fromtimestamp(signal.fired_at, CST).strftime("%m-%d %H:%M")
    return f"{local} CST" + (f"（交易日 {signal.trading_day}）" if signal.trading_day else "")


def format_signal(signal: Signal, description: str = "") -> str:
    """推送正文。**必带各关键值** —— 一条只说"触发了"的通知没法据以决策。"""
    lines = [
        f"{_ARROW[signal.direction]} {signal.symbol} [{signal.timeframe}]",
        f"规则: {signal.rule_id}" + (f" — {description}" if description else ""),
        f"时间: {_fmt_time(signal)}",
        f"触发价: {_fmt_value(signal.trigger_price)}",
    ]
    extra = [f"  {k} = {_fmt_value(v)}" for k, v in signal.context.items()]
    if extra:
        lines.append("关键值:")
        lines.extend(extra)
    if signal.tentative:
        lines.append("⚠️ 盘中预报（未收盘确认），不进统计")
    return "\n".join(lines)


class Notifier(Protocol):
    name: str

    async def send(self, text: str) -> bool:
        """发送。成功返回 True；失败返回 False 而**不抛异常**。"""
        ...


@dataclass(slots=True)
class TelegramNotifier:
    token: str
    chat_id: str
    session: aiohttp.ClientSession | None = None
    timeout_s: float = 10.0
    name: str = "telegram"

    async def send(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        return await _post_json(
            self.session, url, {"chat_id": self.chat_id, "text": text}, self.timeout_s
        )


@dataclass(slots=True)
class BarkNotifier:
    """Bark（iOS 推送）。base_url 形如 https://api.day.app/<key>。"""

    base_url: str
    session: aiohttp.ClientSession | None = None
    timeout_s: float = 10.0
    name: str = "bark"

    async def send(self, text: str) -> bool:
        title, _, body = text.partition("\n")
        url = f"{self.base_url.rstrip('/')}/{quote(title, safe='')}/{quote(body, safe='')}"
        return await _get(self.session, url, self.timeout_s)


@dataclass(slots=True)
class MultiNotifier:
    """并发发往多个渠道。单个渠道失败只计数，不影响其他渠道，更不向上抛。"""

    notifiers: Sequence[Notifier]
    failures: dict[str, int] = field(default_factory=dict)

    async def send(self, text: str) -> dict[str, bool]:
        if not self.notifiers:
            return {}
        results = await asyncio.gather(
            *(n.send(text) for n in self.notifiers), return_exceptions=True
        )
        out: dict[str, bool] = {}
        for notifier, result in zip(self.notifiers, results, strict=True):
            ok = result is True
            out[notifier.name] = ok
            if not ok:
                self.failures[notifier.name] = self.failures.get(notifier.name, 0) + 1
        return out


async def _post_json(
    session: aiohttp.ClientSession | None, url: str, body: dict[str, Any], timeout_s: float
) -> bool:
    own = session is None
    s = session or aiohttp.ClientSession()
    try:
        async with s.post(url, json=body, timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
            return r.status < 400
    except (aiohttp.ClientError, TimeoutError):
        return False  # 推送失败不得反过来打断行情处理
    finally:
        if own:
            await s.close()


async def _get(session: aiohttp.ClientSession | None, url: str, timeout_s: float) -> bool:
    own = session is None
    s = session or aiohttp.ClientSession()
    try:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
            return r.status < 400
    except (aiohttp.ClientError, TimeoutError):
        return False
    finally:
        if own:
            await s.close()


__all__ = [
    "BarkNotifier",
    "MultiNotifier",
    "Notifier",
    "TelegramNotifier",
    "format_signal",
]
