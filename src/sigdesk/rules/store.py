"""规则文件的增删改查（PRD FR-5.3）。

`config/rules/` 下的文件**会被真加载且 fail-fast** —— 写坏一个就是下次启动失败。
所以这里的铁律是：

1. **只写校验通过的**。保存前先 `load_rule` 编译一遍（表达式走白名单 AST，
   未知函数名/变量名在编译期就会报错），不通过就根本不落盘。
2. **原子写**。临时文件 + `os.replace`，避免写到一半进程挂掉留下半个 YAML。
3. **删除不真删**，移进 `_trash/`。规则是人花时间调出来的，误删不可逆比留垃圾糟得多。
   `load_rules` 用的是 `glob("*.yaml")` 不是 `rglob`，所以子目录不会被加载。
4. **id 与文件名绑定**（`<id>.yaml`）。加载器本身允许文件名与 id 无关，
   但让人从面板改 id 却不改文件名，很快就会出现"改完存了两份"。
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import re
import tempfile
from typing import Any

import yaml

from .loader import RuleError, load_rule
from .model import Rule

TRASH_DIR = "_trash"
# 文件名安全：id 直接拼进路径，不限制就是路径穿越
VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RuleStoreError(ValueError):
    pass


def _check_id(rule_id: str) -> str:
    if not VALID_ID.match(rule_id):
        raise RuleStoreError(
            f"规则 id {rule_id!r} 不合法：只允许字母数字与 . _ -，长度 1~64，且不能以符号开头"
            f"（id 会直接作为文件名）"
        )
    return rule_id


def parse_source(source: str, registry: Any = None) -> tuple[Rule, dict[str, object]]:
    """解析并**编译**一段规则 YAML。语法校验就是这一步，与真正加载走同一条路。

    ``registry`` 用于展开 universe 里的通配符（``CN.*`` = 国内期货全品种）。
    不传的话带通配符的规则会被判为非法 —— 面板保存/校验时必须传，
    否则你在面板里写 `CN.*` 会被拒，而它在盘上是合法的。
    """
    try:
        raw = yaml.safe_load(source)
    except yaml.YAMLError as e:
        raise RuleStoreError(f"YAML 解析失败: {e}") from e
    if not isinstance(raw, dict):
        raise RuleStoreError("规则必须是一个 YAML 对象（顶层是 id/universe/conditions 这些键）")
    try:
        rule = load_rule(raw, registry)
    except RuleError as e:
        raise RuleStoreError(str(e)) from e
    _check_id(rule.id)
    return rule, raw


class RuleStore:
    """规则目录的读写。构造不做 IO，方法各自负责。"""

    def __init__(self, directory: pathlib.Path, registry: Any = None) -> None:
        self.directory = directory
        # 展开 universe 里的通配符要用它（`CN.*` = 国内期货全品种）。
        # 面板的规则读写必须传，否则写着 `CN.*` 的规则在面板里存不进去。
        self.registry = registry

    def path_of(self, rule_id: str) -> pathlib.Path:
        return self.directory / f"{_check_id(rule_id)}.yaml"

    def ids(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(p.stem for p in self.directory.glob("*.yaml"))

    def read_source(self, rule_id: str) -> str:
        path = self.path_of(rule_id)
        if not path.exists():
            raise RuleStoreError(f"规则 {rule_id} 不存在")
        return path.read_text(encoding="utf-8")

    def save(self, source: str, *, expect_id: str | None = None, create: bool = False) -> Rule:
        """校验 -> 原子写。`create=True` 时目标已存在即报错，避免误覆盖别人的规则。"""
        rule, _ = parse_source(source, self.registry)
        if expect_id is not None and rule.id != expect_id:
            raise RuleStoreError(
                f"内容里的 id 是 {rule.id!r}，与要保存的 {expect_id!r} 不一致。"
                f"改 id 请当作「新建 + 删除旧的」，否则会留下两份"
            )
        path = self.path_of(rule.id)
        if create and path.exists():
            raise RuleStoreError(f"规则 {rule.id} 已存在。要覆盖请用更新（PUT），不要用新建")

        # 与目录里其它文件的 id 冲突（有人手工建了个文件名不等于 id 的规则）
        for other in self.directory.glob("*.yaml"):
            if other == path:
                continue
            try:
                raw = yaml.safe_load(other.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                continue
            if isinstance(raw, dict) and str(raw.get("id") or "") == rule.id:
                raise RuleStoreError(f"id {rule.id} 与 {other.name} 里的规则重复")

        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{rule.id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(source if source.endswith("\n") else source + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise
        return rule

    def delete(self, rule_id: str) -> pathlib.Path:
        """移进 `_trash/`，不真删。返回归档后的路径。"""
        path = self.path_of(rule_id)
        if not path.exists():
            raise RuleStoreError(f"规则 {rule_id} 不存在")
        trash = self.directory / TRASH_DIR
        trash.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        target = trash / f"{rule_id}.{stamp}.yaml"
        os.replace(path, target)
        return target

    def validate_all(self) -> None:
        """把整个目录编译一遍。保存后调它，确保下次启动一定起得来。"""
        seen: dict[str, str] = {}
        for path in sorted(self.directory.glob("*.yaml")):
            rule, _ = parse_source(path.read_text(encoding="utf-8"), self.registry)
            if rule.id in seen:
                raise RuleStoreError(f"规则 id 重复: {rule.id}（{seen[rule.id]} 与 {path.name}）")
            seen[rule.id] = path.name


__all__ = ["TRASH_DIR", "RuleStore", "RuleStoreError", "parse_source"]
