"""规则文件 CRUD。config/rules 会被真加载且 fail-fast —— 写坏一个就是下次启动失败。"""

from __future__ import annotations

import pathlib

import pytest

from sigdesk.rules.loader import load_rules
from sigdesk.rules.store import RuleStore, RuleStoreError, parse_source

GOOD = """
id: demo
universe: [CRYPTO.OKX.BTCUSDT.PERP]
conditions:
  - on: 5m
    mode: state
    when: close > 0
emit:
  direction: long
"""


@pytest.fixture
def store(tmp_path: pathlib.Path) -> RuleStore:
    d = tmp_path / "rules"
    d.mkdir()
    return RuleStore(d)


def test_save_then_load_roundtrip(store: RuleStore) -> None:
    rule = store.save(GOOD, create=True)
    assert rule.id == "demo"
    assert store.path_of("demo").exists()
    assert [r.id for r in load_rules(store.directory)] == ["demo"]


def test_invalid_rule_never_touches_disk(store: RuleStore) -> None:
    """校验不过就根本不落盘 —— 落了盘就是下次启动失败。"""
    with pytest.raises(RuleStoreError):
        store.save(GOOD.replace("close > 0", "close > __import__('os')"), create=True)
    assert list(store.directory.glob("*.yaml")) == []
    assert list(store.directory.glob("*.tmp")) == []


def test_bad_yaml_is_reported_not_written(store: RuleStore) -> None:
    with pytest.raises(RuleStoreError, match="YAML 解析失败"):
        store.save("id: [unclosed\n", create=True)
    assert list(store.directory.glob("*")) == []


def test_non_object_source_is_rejected(store: RuleStore) -> None:
    with pytest.raises(RuleStoreError, match="必须是一个 YAML 对象"):
        store.save("- just\n- a list\n", create=True)


def test_create_refuses_to_overwrite(store: RuleStore) -> None:
    store.save(GOOD, create=True)
    with pytest.raises(RuleStoreError, match="已存在"):
        store.save(GOOD, create=True)


def test_update_requires_matching_id(store: RuleStore) -> None:
    """从面板改 id 却不改文件名，很快就会出现"改完存了两份"。"""
    store.save(GOOD, create=True)
    with pytest.raises(RuleStoreError, match="不一致"):
        store.save(GOOD.replace("id: demo", "id: renamed"), expect_id="demo")


def test_duplicate_id_in_another_file_is_caught(store: RuleStore) -> None:
    (store.directory / "handwritten.yaml").write_text(GOOD, encoding="utf-8")
    with pytest.raises(RuleStoreError, match="重复"):
        store.save(GOOD, create=True)


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".hidden", "", "x" * 65])
def test_path_traversal_and_junk_ids_are_refused(store: RuleStore, bad: str) -> None:
    """id 直接拼进路径。不限制就是路径穿越。"""
    with pytest.raises(RuleStoreError, match="不合法"):
        store.path_of(bad)


def test_delete_archives_instead_of_unlinking(store: RuleStore) -> None:
    """规则是人花时间调出来的，误删不可逆比留垃圾糟得多。"""
    store.save(GOOD, create=True)
    archived = store.delete("demo")
    assert not store.path_of("demo").exists()
    assert archived.exists() and archived.read_text(encoding="utf-8").strip() == GOOD.strip()
    # 回收站是子目录，load_rules 用的是 glob 不是 rglob，不会被加载
    assert load_rules(store.directory) == []


def test_delete_missing_is_an_error(store: RuleStore) -> None:
    with pytest.raises(RuleStoreError, match="不存在"):
        store.delete("nope")


def test_validate_all_catches_a_handwritten_duplicate(store: RuleStore) -> None:
    store.save(GOOD, create=True)
    (store.directory / "other.yaml").write_text(GOOD, encoding="utf-8")
    with pytest.raises(RuleStoreError, match="id 重复"):
        store.validate_all()


def test_parse_source_compiles_expressions() -> None:
    """校验走的是与真正加载完全同一条编译路径。白名单 AST 在这里就该报错。"""
    with pytest.raises(RuleStoreError):
        parse_source(GOOD.replace("close > 0", "close.__class__"))
    with pytest.raises(RuleStoreError):
        parse_source(GOOD.replace("close > 0", "no_such_fn(close)"))


def test_shipped_rules_dir_survives_validate_all() -> None:
    """真规则目录必须能过 validate_all —— 它就是"下次还起得来吗"的判据。"""
    RuleStore(pathlib.Path("config/rules")).validate_all()
