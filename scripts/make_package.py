#!/usr/bin/env python
"""打 Windows 测试包。

    python scripts/make_package.py            # 出到 dist/

**装什么由 `git ls-files` 决定** —— 那正是"仓库里有什么"，于是 `.gitignore` 挡掉的
东西（`.env`、`data/`、各种缓存、设计画布的播种产物）天然不会进包，不需要再维护
一份平行的排除清单（维护两份迟早对不上，而对不上的后果是把凭据打进包里）。

样本数据是**显式加进来**的：验收要用它离线跑，但它在 .gitignore 里。
只带 `CN.SHFE.rb.CONT`（主连，跨一次换月）+ 拼接元数据，够验收用，约 2.6 MB。

三条硬约束，出包前逐条断言：
1. **不含 `.env`**（凭据不进包；用户级 `~/.signal-desk/.env` 跟着机器走）
2. **不含任何 `.sqlite3`**（运行态是你的信号历史，不该分发）
3. **解压后只有一层目录** `signal-desk/`，不是 `signal-desk/signal-desk/`
"""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOP = "signal-desk"
# 验收离线跑要用的样本。别把整个 data/ 塞进来 —— 加密行情他自己连得上，
# 其余期货合约对验收没有增量价值，只是让包变大。
SAMPLES = [
    "data/bars/CN/CN.SHFE.rb.CONT",
    "data/bars/_continuous",
]
BANNED = (".env", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", "credentials")


def tracked() -> list[pathlib.Path]:
    out = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True,
    )
    return [ROOT / p for p in out.stdout.decode().split("\0") if p]


def samples() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for rel in SAMPLES:
        d = ROOT / rel
        if not d.exists():
            print(f"⚠️  样本缺失：{rel} —— 验收的「数据与日历」组会失败")
            continue
        files.extend(f for f in d.rglob("*") if f.is_file())
    return files


def main() -> int:
    files = tracked() + samples()
    bad = [f for f in files if any(str(f).endswith(b) or f.name == b for b in BANNED)]
    if bad:
        print("拒绝出包，命中禁止项：", *[f"  {f}" for f in bad], sep="\n")
        return 2

    stamp = dt.date.today().isoformat()
    out = ROOT / "dist" / f"signal-desk-{stamp}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            z.write(f, f"{TOP}/{f.relative_to(ROOT).as_posix()}")

    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    tops = {n.split("/", 1)[0] for n in names}
    assert tops == {TOP}, f"解压后不是单层目录: {tops}"
    assert not [n for n in names if n.endswith(".env")], "包里有 .env"
    assert not [n for n in names if ".sqlite3" in n], "包里有数据库"
    assert f"{TOP}/run_acceptance.bat" in names, "缺 run_acceptance.bat"

    mb = out.stat().st_size / 1024 / 1024
    print(f"{out}  {mb:.1f} MB  {len(names)} 个文件")
    print(f"  代码 {len(tracked())} 个 · 样本 {len(samples())} 个")
    print("  ✅ 无 .env · 无数据库 · 单层目录 · 含 run_acceptance.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
