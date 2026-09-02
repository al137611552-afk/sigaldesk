"""规则 YAML 加载。唯一的 IO 是读文件，读完之后全是纯值对象。

加载期就把表达式**编译并校验**（未注册的函数、未知变量、非法语法都在这里报），
不留到运行期 —— 一条打错字的规则应该在启动时炸掉，而不是在盘中静默不触发。

两种写法都支持，且是同一个模型：

    # 单级别
    timeframe: 15m
    conditions: [{on: 15m, mode: event, when: "..."}]

    # 多级别（§5.1 三段式）
    timeframes: {trend: 1h, setup: 15m, trigger: 5m}
    conditions:
      - {on: trend,   mode: state,  when: "..."}
      - {on: setup,   mode: window, within: 6, when: "..."}
      - {on: trigger, mode: event,  when: "..."}

条件的顺序**就是链路顺序**：前一段满足才轮到后一段，最后一段满足即触发。
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import yaml

from ..core.models import Timeframe
from ..patterns import functions  # noqa: F401  触发 A/B 档函数注册
from ..patterns.expr import CompiledExpr, ExprError, compile_expr
from .model import (
    DEFAULT_DEDUP_KEY,
    Direction,
    Mode,
    Priority,
    Rule,
    RuleCondition,
    RuleEmit,
    parse_duration,
)

_TTL = re.compile(r"^\s*(\d+)\s*(?:bars?)?\s*$", re.IGNORECASE)


def _priority(raw: object, rule_id: str) -> Priority:
    """档位必须是已知值。写错一个字母就静默多一个谁也没定义的档位，
    表现是"这条规则的信号偶尔被别的盖住"且毫无提示 —— 宁可拒绝启动。"""
    try:
        return Priority.parse(raw)
    except ValueError as exc:
        raise RuleError(f"规则 {rule_id} 的 {exc}") from None


def _get_on(cond: dict[Any, Any]) -> str:
    """取条件的 ``on``。

    **YAML 1.1 的坑**：PyYAML 会把裸键 ``on:`` 解析成**布尔 True**
    （``yes``/``no``/``off`` 同理），所以 ``{"on": ...}`` 实际是 ``{True: ...}``。
    规范里条件键就叫 ``on``，不兼容这一点的话每个规则文件都会报"缺少 on"。
    用户想显式一点可以写 ``"on": 15m``，两种都收。
    """
    value = cond.get("on", cond.get(True))
    return "" if value is None else str(value)


class RuleError(ValueError):
    """规则配置有误。消息里一定带规则 id，便于定位是哪个文件。"""


def parse_ttl_bars(value: str | int) -> int:
    """'8 bars' / '8' / 8 -> 8。

    TTL 只接受**根数**，不接受 30m 这种时长：用墙钟的话，休市与数据缺口会白白消耗 TTL，
    回测与实盘就对不上了（§5.3）。
    """
    if isinstance(value, int):
        return value
    m = _TTL.match(str(value))
    if not m:
        raise ValueError(f"ttl 只接受根数（如 `8 bars`），不接受时长，收到 {value!r}")
    return int(m.group(1))


def _compile(source: str, rule_id: str, where: str) -> CompiledExpr:
    try:
        return compile_expr(str(source))
    except ExprError as e:
        raise RuleError(f"规则 {rule_id} 的 {where} 编译失败: {e}") from e


def _timeframe(text: str, rule_id: str, where: str) -> Timeframe:
    try:
        tf = Timeframe(text)
    except ValueError as e:
        allowed = ", ".join(t.value for t in Timeframe)
        raise RuleError(f"规则 {rule_id} 的 {where} 周期 {text!r} 无效；可用: {allowed}") from e
    return tf


def _role_map(raw: dict[str, Any], rule_id: str) -> dict[str, Timeframe]:
    """角色 -> 周期。单级别写法里 timeframe: 15m 视为一个名为 "15m" 的角色。"""
    if "timeframes" in raw:
        mapping = raw["timeframes"] or {}
        if not isinstance(mapping, dict) or not mapping:
            raise RuleError(f"规则 {rule_id} 的 timeframes 必须是「角色: 周期」映射")
        return {
            str(role): _timeframe(str(tf), rule_id, f"timeframes.{role}")
            for role, tf in mapping.items()
        }
    if "timeframe" in raw:
        tf = _timeframe(str(raw["timeframe"]), rule_id, "timeframe")
        return {tf.value: tf}
    return {}


def _condition(
    cond: dict[str, Any], roles: dict[str, Timeframe], rule_id: str, index: int
) -> RuleCondition:
    on = _get_on(cond)
    if not on:
        raise RuleError(f"规则 {rule_id} 第 {index + 1} 个条件缺少 on")
    if on in roles:
        role, timeframe = on, roles[on]
    else:
        # 允许直接写周期（单级别常见写法）；角色名即周期名
        timeframe = _timeframe(on, rule_id, f"conditions[{index}].on")
        role = on
        if roles and timeframe not in roles.values():
            known = ", ".join(roles)
            raise RuleError(
                f"规则 {rule_id} 第 {index + 1} 个条件的 on={on!r} 既不是已声明的角色"
                f"（{known}），也不属于 timeframes 里的任何周期"
            )

    try:
        mode = Mode(str(cond.get("mode", "state")))
    except ValueError as e:
        allowed = ", ".join(m.value for m in Mode)
        raise RuleError(f"规则 {rule_id} 的 mode 必须是 {allowed} 之一") from e

    within = int(cond.get("within", 1))
    if mode is Mode.WINDOW:
        if within < 1:
            raise RuleError(f"规则 {rule_id} 的 within 必须 >= 1，收到 {within}")
    elif "within" in cond:
        raise RuleError(f"规则 {rule_id}: within 只对 mode: window 有意义（当前 mode={mode}）")

    if "plugin" in cond:
        raise RuleError(
            f"规则 {rule_id} 用了 plugin: 键；C 档插件直接在 when 里当函数调用即可，"
            f'例如 when: "my_setup(lookback=20)"'
        )
    when = cond.get("when")
    if not when:
        raise RuleError(f"规则 {rule_id} 的 {role} 条件缺少 when 表达式")

    return RuleCondition(
        role=role,
        on=timeframe,
        mode=mode,
        when=_compile(str(when), rule_id, f"{role}.when"),
        within=within,
    )


def _context(raw: Any, rule_id: str, where: str) -> tuple[tuple[str, CompiledExpr], ...]:
    if not raw:
        return ()
    if not isinstance(raw, dict):
        raise RuleError(f"规则 {rule_id} 的 {where} 必须是「名字: 表达式」映射")
    return tuple((str(k), _compile(str(v), rule_id, f"{where}.{k}")) for k, v in raw.items())


def load_rule(raw: dict[str, Any], registry: Any = None) -> Rule:
    rule_id = str(raw.get("id") or "")
    if not rule_id:
        raise RuleError("规则缺少 id")

    roles = _role_map(raw, rule_id)
    conditions_raw = raw.get("conditions") or []
    if not conditions_raw:
        raise RuleError(f"规则 {rule_id} 没有条件")
    if not isinstance(conditions_raw, list):
        raise RuleError(f"规则 {rule_id} 的 conditions 必须是列表（顺序即链路顺序）")

    conditions = tuple(
        _condition(c, roles, rule_id, i) for i, c in enumerate(conditions_raw)
    )
    seen_roles = [c.role for c in conditions]
    if len(set(seen_roles)) != len(seen_roles):
        raise RuleError(f"规则 {rule_id} 的条件角色重复: {seen_roles}")

    universe = _universe(raw.get("universe") or (), rule_id, registry)

    emit_raw = raw.get("emit") or {}
    try:
        direction = Direction(str(emit_raw.get("direction", "neutral")))
    except ValueError as e:
        raise RuleError(f"规则 {rule_id} 的 direction 必须是 long/short/neutral") from e
    try:
        ttl_bars = parse_ttl_bars(emit_raw["ttl"]) if "ttl" in emit_raw else 0
    except ValueError as e:
        raise RuleError(f"规则 {rule_id}: {e}") from e
    if ttl_bars and len(conditions) < 2:
        raise RuleError(
            f"规则 {rule_id} 只有一个条件却设了 ttl —— TTL 是「链路已推进、只差扳机」时的倒计时，"
            f"单条件规则没有这个中间状态"
        )

    confirm_raw = emit_raw.get("confirm_on_close", True)
    if not isinstance(confirm_raw, bool):
        # 别用 bool() 兜：bool("false") 是 True，写错的人会以为自己关掉了确认。
        raise RuleError(
            f"规则 {rule_id} 的 confirm_on_close 必须是布尔值，收到 {confirm_raw!r}"
        )
    if not confirm_raw:
        raise RuleError(
            f"规则 {rule_id} 想开盘中预报（confirm_on_close: false），但**尚未实现** —— "
            f"Signal.tentative 字段、落盘与推送分支都在，唯独没有任何一处会把它置为 True。"
            f"在实现之前宁可拒绝启动，也不要让你以为开了、实际还是只在收盘报。"
        )

    emit = RuleEmit(
        direction=direction,
        cooldown_s=parse_duration(emit_raw.get("cooldown", 0)),
        dedup_key=str(emit_raw.get("dedup_key", DEFAULT_DEDUP_KEY)),
        channels=tuple(str(c) for c in (emit_raw.get("channels") or ())),
        priority=_priority(emit_raw.get("priority", "normal"), rule_id),
        confirm_on_close=confirm_raw,
        ttl_bars=ttl_bars,
    )

    known_roles = {c.role for c in conditions}
    by_role_raw = raw.get("context_by_role") or {}
    if not isinstance(by_role_raw, dict):
        raise RuleError(f"规则 {rule_id} 的 context_by_role 必须是「角色: {{名字: 表达式}}」映射")
    for role in by_role_raw:
        if str(role) not in known_roles:
            raise RuleError(
                f"规则 {rule_id} 的 context_by_role 引用了未知角色 {role!r}；"
                f"可用: {', '.join(sorted(known_roles))}"
            )
    context_by_role = tuple(
        (str(role), _context(exprs, rule_id, f"context_by_role.{role}"))
        for role, exprs in by_role_raw.items()
    )

    return Rule(
        id=rule_id,
        universe=universe,
        conditions=conditions,
        emit=emit,
        enabled=bool(raw.get("enabled", True)),
        description=str(raw.get("description", "")),
        context=_context(raw.get("context"), rule_id, "context"),
        context_by_role=context_by_role,
    )


def _universe(raw: Any, rule_id: str, registry: Any) -> tuple[str, ...]:
    """解析 universe，**在这里就把通配符展开成具体清单**。

    支持 ``CN.*`` 这种前缀通配（"所有国内期货"）。展开放在加载期而不是让下游
    各自去解析：``Rule.universe`` 有十几处消费方（引擎、试算、面板、采集列表、
    纸上回测…），任何一处忘了处理通配符，表现都是"这个标的悄悄不被盯"。
    展开一次，下游拿到的永远是具体 uid。

    **主连不进**：它是拼接序列，可回测可看图但不可下单（CLAUDE.md 坑#9），
    不该产生预警。``registry.tradable()`` 本来就排除它们。

    没有 registry 却用了通配符时**直接报错**，不是静默当成空 —— 空 universe
    的规则一条都不会报，而且毫无提示。
    """
    items = [str(u) for u in raw]
    if not items:
        raise RuleError(f"规则 {rule_id} 的 universe 为空，不会作用于任何标的")
    out: list[str] = []
    for item in items:
        if not item.endswith(".*"):
            out.append(item)
            continue
        if registry is None:
            raise RuleError(
                f"规则 {rule_id} 用了通配符 {item}，但加载时没有传入 registry，"
                f"无法展开成具体标的"
            )
        prefix = item[:-1]          # "CN.*" -> "CN."
        hit = sorted(s.uid for s in registry.tradable() if s.uid.startswith(prefix))
        if not hit:
            raise RuleError(f"规则 {rule_id} 的 {item} 没有匹配到任何标的")
        out.extend(hit)
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:            # 去重但保序：通配符展开的和手写的可能重合
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return tuple(uniq)


def load_rules(directory: pathlib.Path, registry: Any = None) -> list[Rule]:
    """加载目录下所有 *.yaml。文件名与 id 无关，id 重复直接报错。

    **fail-fast**：任何一个文件坏了就整体拒绝启动。静默跳过坏规则会让人以为自己被覆盖了，
    其实没有 —— 那比启动失败危险得多。
    """
    rules: list[Rule] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RuleError(f"{path} 不是一个规则对象")
        try:
            rule = load_rule(raw, registry)
        except RuleError as e:
            raise RuleError(f"{path.name}: {e}") from e
        if rule.id in seen:
            raise RuleError(f"规则 id 重复: {rule.id}（{path.name}）")
        seen.add(rule.id)
        rules.append(rule)
    return rules


__all__ = ["RuleError", "load_rule", "load_rules", "parse_ttl_bars"]
