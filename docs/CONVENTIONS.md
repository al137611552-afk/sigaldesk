# CONVENTIONS — Signal Desk

## 代码
- Python 3.12，全量类型标注；`ruff` + `mypy`。
- 纯逻辑（indicators / patterns / rules）**不得** import 网络、磁盘、时间模块的具体实现；
  当前时间一律由外部传入（`as_of`），便于测试与回放。
- 所有对外 IO 集中在 `feed/` 与 `sinks/`。
- 命名：内部 symbol 形如 `CN.SHFE.rb2610` / `CN.SHFE.rb.MAIN` / `CRYPTO.BINANCE.BTCUSDT.PERP`。

## 时间
- 内部一律秒级 UTC epoch；Bar 必带 `open_ts` 与 `close_ts`，禁止只存一个时间戳。
- 期货 Bar 另带 `trading_day`（交易日，与自然日解耦）。
- 展示层才做时区转换。

## 测试
- 每个指标/原语/插件配单测；形态类用固定 CSV/Parquet 夹具，结果快照比对。
- 改动后跑全量回归，全绿才算完成；失败如实贴输出。

## 版本与提交
- Keep a Changelog + SemVer + Conventional Commits。
- 关键决策写 ADR（`docs/adr/ADR-NNNN-*.md`）。
- 只在明确要求时提交/推送；不在默认分支直接提交。

## 安全
- 凭据只进 `.env`（chmod 600，已 gitignore）；不进代码、日志、文档、提交。
- Quote API 用 TLS 指纹固定，不用全局 `verify=False`。
