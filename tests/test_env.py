""".env 加载。**凭据相关的代码，错了就是泄露或白跑**，所以每条都钉住。"""

from __future__ import annotations

import os
import pathlib

import pytest

from sigdesk.core.env import EnvLoad, candidate_paths, load_env, parse_env


def test_parse_handles_comments_export_and_quotes() -> None:
    text = """
# 注释
QUOTE_API_BASE=https://host:8680
export QUOTE_API_KEY="abc=123"
BARK_URL='https://day.app/x'
EMPTY=
NOT_A_PAIR
    SPACED  =  value
"""
    got = parse_env(text)
    assert got["QUOTE_API_BASE"] == "https://host:8680"
    assert got["QUOTE_API_KEY"] == "abc=123", "值里可以有等号，只按第一个 = 切"
    assert got["BARK_URL"] == "https://day.app/x"
    assert got["EMPTY"] == ""
    assert got["SPACED"] == "value"
    assert "NOT_A_PAIR" not in got


def test_existing_environment_wins(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 export / CI 注入的值优先级最高 —— 否则"我明明设了却不生效"极难查。"""
    (tmp_path / ".env").write_text("K1=from_file\nK2=from_file\n", encoding="utf-8")
    monkeypatch.delenv("SIGDESK_ENV", raising=False)
    monkeypatch.setenv("K1", "from_shell")
    monkeypatch.delenv("K2", raising=False)
    load_env(tmp_path)
    assert os.environ["K1"] == "from_shell"
    assert os.environ["K2"] == "from_file"


def test_lookup_order_is_explicit_then_project_then_user(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户级放最后，但它是**换新包也不丢**的那一层 ——
    分发包里不含 .env，项目内那份每换一个包就没了。"""
    monkeypatch.setenv("SIGDESK_ENV", str(tmp_path / "explicit.env"))
    paths = candidate_paths(tmp_path)
    assert paths[0].name == "explicit.env"
    assert paths[1] == tmp_path / ".env"
    assert paths[2].parts[-2:] == (".signal-desk", ".env")


def test_missing_files_are_not_an_error(tmp_path: pathlib.Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 .env 时脚本仍要能起来（凭据可以从环境变量来）。"""
    monkeypatch.setenv("SIGDESK_ENV", str(tmp_path / "nope.env"))
    got = load_env(tmp_path / "also-nope")
    assert isinstance(got, EnvLoad)
    assert "未找到" in got.describe()


def test_describe_never_leaks_values(tmp_path: pathlib.Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """**诊断信息只能有键名，不能有值** —— 值进了日志就等于泄露。"""
    secret = "super-secret-key-value"
    (tmp_path / ".env").write_text(f"QUOTE_API_KEY={secret}\n", encoding="utf-8")
    monkeypatch.delenv("SIGDESK_ENV", raising=False)
    monkeypatch.delenv("QUOTE_API_KEY", raising=False)
    got = load_env(tmp_path)
    assert secret not in got.describe()
    assert secret not in repr(got)
    assert "QUOTE_API_KEY" in got.keys


def test_no_env_file_ships_in_the_repo() -> None:
    """凭据只进 .env，而 .env 不进版本库、不进分发包（CLAUDE.md 的既定约定）。"""
    assert pathlib.Path(".env.example").is_file(), "示例文件要在"
    example = pathlib.Path(".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            _, _, value = line.partition("=")
            assert not value.strip() or value.strip().startswith("https://your"), (
                f".env.example 里不该有真值: {line}")


def test_watch_degrades_per_market_instead_of_failing_whole() -> None:
    """**一个市场挂了不该拖垮另一个。** 用户那台机器 DNS 污染导致 OKX 连不上，
    结果连能用的期货也盯不了 —— 盯盘系统不该这样。

    这里钉住源码结构（真实降级行为已用 hosts 污染 + 抽掉凭据两种方式实跑验证过，
    见 DEVLOG 2026-09-01）。
    """
    src = pathlib.Path("scripts/watch.py").read_text(encoding="utf-8")
    body = src[src.index("取加密历史"):]
    body = body[: body.index("missed = apply_history")]
    assert body.count("except Exception") >= 2, "两个市场各自要能单独失败"
    assert "本次不盯加密" in body and "本次不盯期货" in body
    assert "QUOTE_API_BASE" in body and "setup_env.py" in body, (
        "缺凭据是最常见的启动失败，要说清楚缺哪个、怎么配")
    assert "没有任何行情源可用" in src, "两边都挂了才算真的起不来，且要说明原因"


def test_pin_tls_reads_the_env_file_itself() -> None:
    """`pin_tls.py` 必须自己 `load_env()`。

    原来它只看 `os.environ`，于是用 `setup_env.py` 把凭据写进用户级 `.env` 之后
    再跑它，仍然会报「请先 `set -a; . ./.env; set +a`」—— 那是一句 **bash 语法**，
    Windows 上照着做也没用，用户会卡在这一步（RUN-WINDOWS.md 里正是这一步）。
    """
    src = pathlib.Path("scripts/pin_tls.py").read_text(encoding="utf-8")
    assert "load_env(ROOT)" in src, "没有读 .env，配好了也会说没配"
    # 只看**打给用户的**那几行；解释这个坑的注释里出现 "set -a" 是应该的
    printed = [ln for ln in src.splitlines()
               if "print(" in ln and not ln.strip().startswith("#")]
    assert not any("set -a" in ln for ln in printed), "别再给 bash-only 的提示"
    assert "setup_env.py" in src, "没配时要指向正确的下一步"


def test_pin_tls_can_write_back() -> None:
    """指纹能自己写回，不用手抄一串 64 位十六进制。

    手抄是最容易抄错、也最容易被跳过的一步 —— 跳过的后果是连不上
    （不是不安全：指纹缺失/不符时客户端会拒绝连接）。
    """
    src = pathlib.Path("scripts/pin_tls.py").read_text(encoding="utf-8")
    assert '"--write"' in src
    assert "from setup_env import write_env" in src, "写文件的口径要与 setup_env 共用一处"
    assert "指纹**变了**" in src, "指纹变化可能是证书轮换，也可能是中间人，必须提醒"


def test_setup_env_offers_to_pin_the_fingerprint() -> None:
    """配完地址就顺手把指纹抓了 —— 少一步手工，就少一个被跳过的环节。

    抓不到也**不能让整个配置流程失败**：网络不通是常事，凭据本身是有效的。
    """
    src = pathlib.Path("scripts/setup_env.py").read_text(encoding="utf-8")
    assert "def try_pin(" in src
    assert "return \"\"" in src, "抓取失败要返回空串继续，不是抛异常"
    assert "pin_tls.py --write" in src, "失败时要告诉用户之后怎么补"
