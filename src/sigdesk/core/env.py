"""`.env` 加载。纯逻辑 + 一点点文件读取，无网络。

**为什么要有这个**：脚本原先直接读 `os.environ`，文档教的是
`set -a; . ./.env; set +a` —— 那是 **bash 专有写法**，Windows 上没有对应物，
于是每开一个终端都要手动 `set` 一遍。让脚本自己读，两个平台才一样。

查找顺序（先找到的先用，**已经在环境里的永远不被覆盖**）：

1. ``SIGDESK_ENV`` 指定的文件（显式最优先）
2. 项目目录下的 ``.env``（跟着这份代码走）
3. ``~/.signal-desk/.env``（**跟着这台机器走**）

第 3 条是关键：分发包里**不含 `.env`**（凭据不进包），所以每换一个新包、
解压到新目录，项目内的 `.env` 就没了。放用户级目录就只配一次。

**永远不打印值。** 只报告读了哪个文件、有哪些键 —— 值一旦进日志就等于泄露。
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

USER_ENV = pathlib.Path.home() / ".signal-desk" / ".env"


@dataclass(frozen=True, slots=True)
class EnvLoad:
    """加载结果。**只有键名，没有值** —— 便于打印诊断而不泄露凭据。"""

    files: list[pathlib.Path] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if not self.files:
            return "未找到 .env（凭据需从环境变量传入）"
        where = "、".join(str(f) for f in self.files)
        return f"已从 {where} 读入 {len(self.keys)} 个配置项"


def parse_env(text: str) -> dict[str, str]:
    """解析 `.env` 文本。支持 `#` 注释、`export ` 前缀、值两侧的成对引号。"""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def candidate_paths(project_dir: pathlib.Path | None = None) -> list[pathlib.Path]:
    """按优先级从高到低列出候选文件（不判断是否存在）。"""
    paths: list[pathlib.Path] = []
    explicit = os.environ.get("SIGDESK_ENV")
    if explicit:
        paths.append(pathlib.Path(explicit))
    if project_dir is not None:
        paths.append(project_dir / ".env")
    paths.append(USER_ENV)
    return paths


def load_env(project_dir: pathlib.Path | None = None) -> EnvLoad:
    """把找到的 `.env` 读进 `os.environ`。

    **已经在环境里的键不会被覆盖** —— 显式 export 或 CI 注入的值优先级最高，
    否则调试时"我明明设了却不生效"会非常难查。
    """
    files: list[pathlib.Path] = []
    keys: set[str] = set()
    for path in candidate_paths(project_dir):
        try:
            if not path.is_file():
                continue
            data = parse_env(path.read_text(encoding="utf-8"))
        except OSError:
            continue  # 读不了就跳过，不该因此让脚本起不来
        applied = False
        for key, value in data.items():
            if key in os.environ:
                continue  # 环境里已有的优先
            os.environ[key] = value
            keys.add(key)
            applied = True
        if applied or data:
            files.append(path)
    return EnvLoad(files=files, keys=sorted(keys))


__all__ = ["USER_ENV", "EnvLoad", "candidate_paths", "load_env", "parse_env"]
