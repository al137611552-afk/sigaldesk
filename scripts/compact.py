#!/usr/bin/env python3
"""把碎掉的 Parquet 分区并成大文件（compaction）。

**为什么需要它。** 分区键原来一律是"交易日"。对 1m（一天 1440 根）合适，
对日/周/月线就退化成**一行一个文件**：实测 998 根日线摊在 975 个文件里，
读一次 789ms、占 3005 KB；并成一个文件后 3ms、59 KB —— **快 278 倍、小 51 倍**。
多出来的全是每个文件那份 schema + footer。这是 Parquet 生态里最经典的
small files problem，Delta Lake 的 OPTIMIZE、Iceberg 的 rewrite_data_files
做的都是同一件事。

现在日历周期（1d/1w/1mon）按**年**分区，本脚本把存量数据迁过去。

**安全性**（这脚本会删文件，所以每一条都不能省）：
  - 先写新分区、**校验逐根一致**、再删旧文件。校验不过就整个标的跳过，不删任何东西。
  - 幂等：已经是新布局的直接跳过，重复跑不会出错。
  - 中断安全：跑一半挂了也只是新旧并存，`read_range` 会按 close_ts 去重兜底，
    再跑一次即可收敛。
  - **跑之前请先停掉 watch.py** —— 一边写一边搬会丢刚落的数据。

用法：
    .venv/bin/python scripts/compact.py --dry-run     # 只看要动什么
    .venv/bin/python scripts/compact.py
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sigdesk.core.models import Timeframe  # noqa: E402
from sigdesk.store.parquet_io import (  # noqa: E402
    partition_key,
    partition_path,
    partition_unit,
    read_bars,
    write_bars,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def plan(data_root: pathlib.Path) -> list[tuple[str, Timeframe, list[pathlib.Path]]]:
    """列出所有"该并而未并"的 (标的, 周期, 待并文件)。"""
    out = []
    for market in sorted(p for p in data_root.iterdir() if p.is_dir()):
        for sym_dir in sorted(p for p in market.iterdir() if p.is_dir()):
            for tf_dir in sorted(p for p in sym_dir.iterdir() if p.is_dir()):
                try:
                    tf = Timeframe(tf_dir.name)
                except ValueError:
                    continue                      # _continuous 之类的辅助目录
                if partition_unit(tf) != "year":
                    continue                      # 分钟级维持按日，见模块 docstring
                files = sorted(f for f in tf_dir.glob("*.parquet"))
                # 已经是年度分区的（文件名是 4 位年份）不用动
                stale = [f for f in files if not (len(f.stem) == 4 and f.stem.isdigit())]
                if stale:
                    out.append((sym_dir.name, tf, files))
    return out


def compact_one(
    data_root: pathlib.Path, uid: str, tf: Timeframe, files: list[pathlib.Path], *, dry: bool
) -> tuple[int, int, str]:
    """并一个 (标的, 周期)。返回 (原文件数, 新文件数, 说明)。"""
    bars = []
    for f in files:
        bars.extend(read_bars(f, uid, tf))
    if not bars:
        return (len(files), len(files), "空目录，跳过")
    before = {b.close_ts: b for b in bars}
    targets = {partition_path(data_root, uid, tf, partition_key(b)) for b in bars}
    if dry:
        return (len(files), len(targets), f"{len(before)} 根")

    write_bars(data_root, list(before.values()))

    # **校验再删**：把新分区读回来，逐根比对。不一致就一个都不删。
    after = {}
    for f in sorted(targets):
        for b in read_bars(f, uid, tf):
            after[b.close_ts] = b
    if after != before:
        return (len(files), len(targets),
                f"⚠️ 校验不一致（旧 {len(before)} 根 / 新 {len(after)} 根），未删除任何文件")

    removed = 0
    for f in files:
        if f not in targets:
            f.unlink()
            removed += 1
    return (len(files), len(targets), f"{len(before)} 根，删了 {removed} 个旧文件")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default=str(ROOT / "data" / "bars"))
    ap.add_argument("--dry-run", action="store_true", help="只报告，不改动任何文件")
    args = ap.parse_args()

    data_root = pathlib.Path(args.data_root)
    if not data_root.exists():
        print(f"没有这个目录：{data_root}")
        return 1

    jobs = plan(data_root)
    if not jobs:
        print("没有需要合并的分区 —— 已经是新布局了。")
        return 0

    note = "（--dry-run，不会改动）" if args.dry_run else ""
    print(f"待合并：{len(jobs)} 个 (标的, 周期){note}")
    tot_before = tot_after = 0
    bad = 0
    for uid, tf, files in jobs:
        b, a, note = compact_one(data_root, uid, tf, files, dry=args.dry_run)
        tot_before += b
        tot_after += a
        if "⚠️" in note:
            bad += 1
        print(f"  {uid:<30} {tf.value:<5} {b:>5} -> {a:<4} 个文件   {note}")

    print(f"\n合计 {tot_before} -> {tot_after} 个文件")
    if bad:
        print(f"⚠️ {bad} 个校验没过，那些的旧文件一个都没删 —— 请先查明原因。")
        return 1
    if args.dry_run:
        print("这是 --dry-run。去掉它再跑一次才会真正合并。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
