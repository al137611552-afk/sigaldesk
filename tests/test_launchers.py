"""Windows 双击启动脚本的静态检查。

**这类文件只有在 Windows 上双击那一刻才知道坏没坏**，而开发机是 Linux，
跑不了 cmd.exe。所以能静态查的都查掉 —— 下面每一条都对应一种"双击后窗口一闪而过"。
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BATS = ["启动面板.bat", "更新程序.bat"]


@pytest.mark.parametrize("name", BATS)
def test_uses_crlf_and_has_no_bom(name: str) -> None:
    """**CRLF 是硬要求**：LF 换行的 .bat 在 cmd 里 goto/标签会失效。

    同时不能有 UTF-8 BOM —— BOM 字节会跑到 `@echo off` 前面，
    cmd 会把它当成一条无法识别的命令回显出来。
    """
    raw = (ROOT / name).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{name} 带了 BOM"
    assert raw.startswith(b"@echo off"), f"{name} 第一行不是 @echo off"
    assert b"\r\n" in raw, f"{name} 不是 CRLF 换行"
    assert raw.count(b"\n") == raw.count(b"\r\n"), f"{name} 混了 LF 和 CRLF"


@pytest.mark.parametrize("name", BATS)
def test_declares_utf8_codepage_before_printing_chinese(name: str) -> None:
    """中文输出前必须 `chcp 65001`，否则窗口里是一堆乱码。"""
    text = (ROOT / name).read_text(encoding="utf-8")
    head = text[: text.index("cd /d")]
    assert "chcp 65001" in head, f"{name} 没在开头切 UTF-8 代码页"


@pytest.mark.parametrize("name", BATS)
def test_runs_from_its_own_directory(name: str) -> None:
    """双击时的工作目录是桌面/资源管理器当前目录，不是脚本所在目录。
    不 `cd /d "%~dp0"` 就会找不到 .venv 和 scripts。"""
    text = (ROOT / name).read_text(encoding="utf-8")
    assert 'cd /d "%~dp0"' in text, f"{name} 没切到脚本自身目录"


@pytest.mark.parametrize("name", BATS)
def test_every_exit_path_pauses(name: str) -> None:
    """**每条退出路径都要 pause**。少一个，用户看到的就是"窗口一闪就没了"，
    连错误信息都读不到 —— 这是 .bat 最常见的坑。"""
    lines = [ln.strip() for ln in (ROOT / name).read_text(encoding="utf-8").splitlines()]
    for i, ln in enumerate(lines):
        if ln.startswith("exit /b"):
            before = [x for x in lines[max(0, i - 4):i] if x]
            assert any(x == "pause" for x in before), f"{name} 第 {i+1} 行的退出前没有 pause"


def test_update_checks_the_panel_is_stopped() -> None:
    """更新时若盯盘还在跑，等于一边写盘一边换代码。必须先拦住。"""
    text = (ROOT / "更新程序.bat").read_text(encoding="utf-8")
    assert "LISTENING" in text, "没有检查端口占用"
    assert ".git" in text, "没有检查这是不是 git 仓库（ZIP 解压来的没法 pull）"


def test_update_reads_file_size_not_file_content() -> None:
    """取文件大小要用 `for %%i in (文件)`，**不能用 `for /f`** —— 那是逐行读内容，
    拿到的 %%~zi 是空的，于是"有本地改动"永远检测不出来。"""
    text = (ROOT / "更新程序.bat").read_text(encoding="utf-8")
    for ln in text.splitlines():
        if "%%~zi" in ln:
            assert "for /f" not in ln, "用了 for /f，取到的不是文件大小"
            assert "for %%i in (" in ln


def test_launcher_waits_before_opening_the_browser() -> None:
    """服务端起来要几秒。立刻开浏览器只会看到"无法连接"，用户会以为没启动成功。"""
    text = (ROOT / "启动面板.bat").read_text(encoding="utf-8")
    assert "Start-Sleep" in text and "Start-Process" in text
