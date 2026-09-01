"""标的与日历注册表。唯一的 IO 是读 YAML 配置，读完之后全是纯查询。"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import yaml

from .calendar import MarketCalendar
from .models import Market, Symbol

CRYPTO_CALENDAR = MarketCalendar.from_config(
    "crypto_24x7", ["00:00-23:59"], [], trades_on_weekends=True
)


@dataclass(frozen=True, slots=True)
class Registry:
    symbols: dict[str, Symbol]
    calendars: dict[str, MarketCalendar]

    def symbol(self, uid: str) -> Symbol:
        try:
            return self.symbols[uid]
        except KeyError:
            raise KeyError(f"未注册的标的 {uid}；请补进 config/symbols.yaml") from None

    def calendar_of(self, uid: str) -> MarketCalendar:
        sym = self.symbol(uid)
        try:
            return self.calendars[sym.calendar]
        except KeyError:
            raise KeyError(f"标的 {uid} 引用了未定义的日历 {sym.calendar}") from None

    def tradable(self) -> list[Symbol]:
        """可用于预警与回测的标的：排除主连/指数这类合成序列。"""
        return [s for s in self.symbols.values() if not s.is_continuous]

    def require_mapping(self, uid: str, field: str) -> str:
        """交易模块启动期校验用：取出必需的三方代码，缺失即报错（ADR-0002）。"""
        value = getattr(self.symbol(uid), field, None)
        if not value:
            raise ValueError(f"标的 {uid} 缺少 {field} 映射，拒绝启动交易模块")
        return str(value)


def load_calendars(path: pathlib.Path) -> dict[str, MarketCalendar]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    holidays: list[str] = list(raw.get("holidays") or [])
    out = {
        cid: MarketCalendar.from_config(
            cid, list(cfg["sessions"]), holidays,
            trades_on_weekends=bool(cfg.get("trades_on_weekends", False)),
        )
        for cid, cfg in (raw.get("calendars") or {}).items()
    }
    out[CRYPTO_CALENDAR.calendar_id] = CRYPTO_CALENDAR
    return out


def load_symbols(path: pathlib.Path) -> dict[str, Symbol]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, Symbol] = {}
    for item in raw.get("symbols") or []:
        sym = Symbol(
            uid=item["uid"],
            market=Market(item["market"]),
            exchange=item["exchange"],
            code=item["code"],
            calendar=item["calendar"],
            quote_code=item.get("quote_code"),
            ctp_code=item.get("ctp_code"),
            ccxt_symbol=item.get("ccxt_symbol"),
            price_tick=float(item.get("price_tick", 0.0)),
            multiplier=float(item.get("multiplier", 1.0)),
            product=item.get("product"),
            main_code=item.get("main_code"),
            is_continuous=bool(item.get("is_continuous", False)),
        )
        if sym.uid in out:
            raise ValueError(f"symbols.yaml 中 uid 重复: {sym.uid}")
        out[sym.uid] = sym
    return out


def load_registry(config_dir: pathlib.Path) -> Registry:
    calendars = load_calendars(config_dir / "calendars" / "cn_futures.yaml")
    symbols = load_symbols(config_dir / "symbols.yaml")
    for sym in symbols.values():
        if sym.calendar not in calendars:
            raise ValueError(f"标的 {sym.uid} 引用了未定义的日历 {sym.calendar}")
    return Registry(symbols=symbols, calendars=calendars)


__all__ = ["Registry", "load_calendars", "load_registry", "load_symbols"]
