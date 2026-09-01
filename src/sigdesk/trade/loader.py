"""交易配置加载。唯一的 IO 是读 YAML，读完之后全是纯值对象。

加载期就把参数**校验掉**（比例越界、模式不认识都在这里报），不留到盘中 ——
一个写错的风控上限如果要等触发才发现，那时已经晚了。
"""

from __future__ import annotations

import pathlib
from typing import Any, get_args

import yaml

from ..stats.outcome import OutcomeParams
from .desk import DeskParams
from .paper import FillParams
from .risk import RiskParams
from .strategy import SizingMode, StrategyParams


class TradingConfigError(ValueError):
    pass


def _num(raw: dict[str, Any], key: str, default: float) -> float:
    if key not in raw or raw[key] is None:
        return default
    try:
        return float(raw[key])
    except (TypeError, ValueError) as e:
        raise TradingConfigError(f"{key} 必须是数字，收到 {raw[key]!r}") from e


def _int(raw: dict[str, Any], key: str, default: int) -> int:
    value = _num(raw, key, float(default))
    if value != int(value):
        raise TradingConfigError(f"{key} 必须是整数，收到 {raw[key]!r}")
    return int(value)


def load_trading(path: pathlib.Path) -> DeskParams:
    """读 config/trading.yaml。文件不存在时返回**默认且关闭**的配置，不报错 ——
    盯盘不该因为没配交易而起不来。"""
    if not path.exists():
        return DeskParams()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TradingConfigError(f"{path} 不是一个配置对象")

    s_raw = dict(raw.get("strategy") or {})
    mode = str(s_raw.get("mode", "risk"))
    if mode not in get_args(SizingMode):
        raise TradingConfigError(
            f"strategy.mode 必须是 {', '.join(get_args(SizingMode))} 之一，收到 {mode!r}"
        )
    e_raw = dict(s_raw.get("exits") or {})
    r_raw = dict(raw.get("risk") or {})
    f_raw = dict(raw.get("fills") or {})

    try:
        exits = OutcomeParams(
            horizon_bars=_int(e_raw, "horizon_bars", 20),
            stop_pct=_num(e_raw, "stop_pct", 0.005),
            target_pct=_num(e_raw, "target_pct", 0.010),
            cost_bps=_num(e_raw, "cost_bps", 0.0),
            atr_key=None if e_raw.get("atr_key") is None else str(e_raw["atr_key"]),
            stop_atr=_num(e_raw, "stop_atr", 1.5),
            target_atr=_num(e_raw, "target_atr", 3.0),
        )
        strategy = StrategyParams(
            mode=mode,  # type: ignore[arg-type]
            risk_per_trade=_num(s_raw, "risk_per_trade", 0.005),
            fixed_qty=_num(s_raw, "fixed_qty", 1.0),
            notional_per_trade=_num(s_raw, "notional_per_trade", 1000.0),
            exits=exits,
            default_lot=_num(s_raw, "default_lot", 0.0),
        )
        risk = RiskParams(
            max_risk_per_trade=_num(r_raw, "max_risk_per_trade", 0.01),
            max_notional_per_trade=_num(r_raw, "max_notional_per_trade", 0.0),
            max_symbol_exposure=_num(r_raw, "max_symbol_exposure", 0.25),
            max_total_exposure=_num(r_raw, "max_total_exposure", 1.0),
            daily_loss_limit=_num(r_raw, "daily_loss_limit", 0.03),
            max_orders_per_window=_int(r_raw, "max_orders_per_window", 10),
            rate_window_s=_int(r_raw, "rate_window_s", 3600),
        )
        fills = FillParams(
            fee_bps=_num(f_raw, "fee_bps", 2.0),
            slippage_bps=_num(f_raw, "slippage_bps", 1.0),
            close_on_horizon=bool(f_raw.get("close_on_horizon", True)),
        )
    except TradingConfigError:
        raise
    except ValueError as e:
        raise TradingConfigError(f"{path}: {e}") from e

    return DeskParams(
        initial_cash=_num(raw, "initial_cash", 100_000.0),
        strategy=strategy, risk=risk, fills=fills,
        enabled=bool(raw.get("enabled", False)),
    )


__all__ = ["TradingConfigError", "load_trading"]
