"""白名单 AST 表达式引擎（形态 A/B 档，ADR-0003）。

规则来自 YAML，也就是**配置即代码**。因此这里不是 `eval` 加个黑名单，而是：
``ast.parse`` 之后**按节点类型白名单**逐个放行，并自己走一遍求值 ——
没有出现在白名单里的语法一律在**编译期**拒绝，运行期不可能出现意外节点。

被明确挡在外面的（各有单测）：
- ``Attribute``（``x.__class__``）—— 属性访问是绝大多数沙箱逃逸的第一步
- ``Subscript``、``Lambda``、推导式、``NamedExpr``（海象）、``Starred``
- ``**`` 幂运算 —— ``2**(10**9)`` 能把求值线程挂死，而表达式里用不到它
- 未注册的函数名与未知变量名 —— **编译期**报错，不是运行期静默为假

三值逻辑：任一操作数为 None（指标预热期）⇒ 结果 None ⇒ 规则判定为"不成立"。
详见 values.py 与 ADR-0006。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from ..core.models import Timeframe
from .context import FIELDS, EvalContext
from .functions import REGISTRY, FuncSpec
from .values import scalar, truthy

# 允许出现的节点类型。改这张表 = 改攻击面，必须同步加单测。
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
    ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
    ast.Call, ast.keyword,
    ast.Name, ast.Load,
    ast.Constant,
)
_ALLOWED_CONSTANTS = (int, float, str, bool, type(None))


class ExprError(ValueError):
    """表达式编译或求值失败。消息里带原始表达式，便于定位是哪条规则写错了。"""


@dataclass(frozen=True, slots=True)
class CompiledExpr:
    """编译好的表达式。可反复求值，本身无状态（状态在 EvalContext 的指标缓存里）。"""

    source: str
    tree: ast.Expression
    functions: frozenset[str]  # 用到的函数名，供规则做依赖说明/文档
    registry: dict[str, FuncSpec]  # 编译时校验用的那张表，求值时必须是同一张
    # 通过 at() 引用到的**其它**周期。调用方据此决定 BarStore 要派生哪些周期 ——
    # 少派生一个就是空序列 -> 指标恒 None -> 条件恒不成立，且毫无提示
    timeframes: frozenset[Timeframe] = frozenset()

    def evaluate(self, ctx: EvalContext) -> bool | None:
        """求值。返回 True/False，或 None 表示"未知"（指标尚在预热期）。"""
        try:
            return truthy(_eval(self.tree.body, ctx, self.registry))
        except ExprError:
            raise
        except Exception as e:  # 求值期的意外一律附上表达式原文，否则无从查起
            raise ExprError(f"表达式求值失败: {self.source} -> {type(e).__name__}: {e}") from e

    def value(self, ctx: EvalContext) -> Any:
        """求**原始值**而非真假。信号的 context 快照要的是 ``ema(close,20)`` 的数字，
        走 ``evaluate`` 会被 truthy 压成布尔。"""
        try:
            return _eval(self.tree.body, ctx, self.registry)
        except ExprError:
            raise
        except Exception as e:
            raise ExprError(f"表达式求值失败: {self.source} -> {type(e).__name__}: {e}") from e

    def is_satisfied(self, ctx: EvalContext) -> bool:
        """成立与否。**None 视为不成立** —— 预热期宁可不报，也不能误报。"""
        return self.evaluate(ctx) is True


def compile_expr(source: str, registry: dict[str, FuncSpec] | None = None) -> CompiledExpr:
    """编译并做白名单校验。语法错、非法节点、未知函数/变量名都在这一步暴露。"""
    funcs = REGISTRY if registry is None else registry
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"表达式语法错误: {source} -> {e}") from e

    used: set[str] = set()
    referenced: set[Timeframe] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(f"表达式中不允许的语法 {type(node).__name__}: {source}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, _ALLOWED_CONSTANTS):
            raise ExprError(f"不允许的常量类型 {type(node.value).__name__}: {source}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExprError(f"只能调用已注册的函数名: {source}")
            if node.func.id not in funcs:
                known = ", ".join(sorted(funcs))
                raise ExprError(f"未注册的函数 {node.func.id}: {source}；可用: {known}")
            # `f(**d)` 会得到 arg=None 的 keyword。求值器把它静默丢掉，
            # 于是参数凭空消失而不报错 —— 在编译期直接拒绝
            if any(k.arg is None for k in node.keywords):
                raise ExprError(f"不允许 ** 展开传参: {source}")
            # **参数个数/名字必须编译期核对**：原先写错要等上线后第一根 bar 才抛
            # TypeError，而 config/rules 是 fail-fast 加载的，坏规则本不该进得了生产
            spec = funcs[node.func.id]
            problem = spec.check_call(len(node.args), [k.arg for k in node.keywords if k.arg])
            if problem:
                raise ExprError(
                    f"{node.func.id}() 参数不对: {source} -> {problem}；"
                    f"用法: {spec.doc}"
                )
            if node.func.id == "at":
                referenced.add(_at_timeframe(node, source))
            used.add(node.func.id)

    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in called and node.id not in FIELDS:
            raise ExprError(
                f"未知变量 {node.id}: {source}；可用字段: {', '.join(FIELDS)}"
            )
    return CompiledExpr(source=source, tree=tree, functions=frozenset(used),
                        registry=funcs, timeframes=frozenset(referenced))


def _at_timeframe(node: ast.Call, source: str) -> Timeframe:
    """``at()`` 的第一个参数必须是**字面量周期**。

    不接受变量或表达式：周期要在编译期就能确定，否则调用方无从知道
    "这条规则还需要派生哪些周期" —— 而少派生一个周期的后果是该级别恒为空序列、
    指标恒为 None、条件恒"不成立"，一条信号都不报且毫无提示。
    """
    if not node.args or not isinstance(node.args[0], ast.Constant):
        raise ExprError(f"at() 的第一个参数必须是字面量周期字符串（如 '1h'）: {source}")
    raw = node.args[0].value
    try:
        tf = Timeframe(str(raw))
    except ValueError:
        allowed = ", ".join(t.value for t in Timeframe)
        raise ExprError(f"at() 的周期 {raw!r} 无效: {source}；可用: {allowed}") from None
    return tf


# ---------------------------------------------------------------- 求值


def _eval(node: ast.AST, ctx: EvalContext, funcs: dict[str, FuncSpec]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return ctx.series(node.id)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):  # 编译期已挡住，这里是兜底
            raise ExprError("只能调用已注册的函数名")
        spec = funcs[node.func.id]
        if spec.special and node.func.id == "at":
            # 第二个参数必须在**切换后的上下文**里求值，所以不能走下面的通用路径
            return _eval(node.args[1], ctx.at(_at_timeframe(node, "at(...)")), funcs)
        args = [_eval(a, ctx, funcs) for a in node.args]
        kwargs = {k.arg: _eval(k.value, ctx, funcs) for k in node.keywords if k.arg}
        return spec.fn(ctx, *args, **kwargs)
    if isinstance(node, ast.UnaryOp):
        return _unary(node, ctx, funcs)
    if isinstance(node, ast.BinOp):
        return _binop(node, ctx, funcs)
    if isinstance(node, ast.Compare):
        return _compare(node, ctx, funcs)
    if isinstance(node, ast.BoolOp):
        return _boolop(node, ctx, funcs)
    raise ExprError(f"求值器不认识的节点 {type(node).__name__}")


def _unary(node: ast.UnaryOp, ctx: EvalContext, funcs: dict[str, FuncSpec]) -> Any:
    v = _eval(node.operand, ctx, funcs)
    if isinstance(node.op, ast.Not):
        t = truthy(v)
        return None if t is None else not t
    s = scalar(v)
    if s is None:
        return None
    return -float(s) if isinstance(node.op, ast.USub) else float(s)


def _binop(node: ast.BinOp, ctx: EvalContext, funcs: dict[str, FuncSpec]) -> float | None:
    left = scalar(_eval(node.left, ctx, funcs))
    right = scalar(_eval(node.right, ctx, funcs))
    if left is None or right is None:
        return None  # 预热期向上传播
    if isinstance(left, str) or isinstance(right, str):
        raise ExprError(f"字符串不能参与算术运算: {ast.dump(node)}")
    a, b = float(left), float(right)
    if isinstance(node.op, ast.Add):
        return a + b
    if isinstance(node.op, ast.Sub):
        return a - b
    if isinstance(node.op, ast.Mult):
        return a * b
    # 除零返回 None（"未知"）而不是抛错：一条规则的算术退化不该掀翻整个引擎
    if b == 0.0:
        return None
    return a / b if isinstance(node.op, ast.Div) else a % b


_COMPARERS: dict[type[ast.cmpop], Any] = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


def _compare(node: ast.Compare, ctx: EvalContext, funcs: dict[str, FuncSpec]) -> bool | None:
    left = scalar(_eval(node.left, ctx, funcs))
    result: bool | None = True
    for op, comparator in zip(node.ops, node.comparators, strict=True):
        right = scalar(_eval(comparator, ctx, funcs))
        if left is None or right is None:
            return None
        if isinstance(left, str) != isinstance(right, str):
            raise ExprError("字符串只能与字符串比较")
        if not _COMPARERS[type(op)](left, right):
            result = False
            break
        left = right  # 链式比较 a < b < c
    return result


def _boolop(node: ast.BoolOp, ctx: EvalContext, funcs: dict[str, FuncSpec]) -> bool | None:
    """短路求值 + 三值逻辑：and 遇 False 直接假，or 遇 True 直接真；否则 None 具有传染性。"""
    saw_unknown = False
    for value in node.values:
        t = truthy(_eval(value, ctx, funcs))
        if t is None:
            saw_unknown = True
            continue
        if isinstance(node.op, ast.And) and not t:
            return False
        if isinstance(node.op, ast.Or) and t:
            return True
    if saw_unknown:
        return None
    return isinstance(node.op, ast.And)


__all__ = ["CompiledExpr", "ExprError", "compile_expr"]
