# ADR-0004 存储：Parquet+DuckDB 存行情，SQLite 存运行态

- 日期: 2026-08-28
- 状态: Accepted

## 背景
开发机 2 核 4G、磁盘仅剩 7.6G；目标规模 ~100 标的 × 4 周期 × 20 规则，分钟级。

## 决策
- 历史 K 线：Parquet，`market/symbol/timeframe/date` 分区，用 DuckDB 做分析查询。
- 运行态（规则、状态机、信号、订单、账户）：SQLite（WAL 模式）。
- Tick 不落盘。

## 权衡
- 否决 ClickHouse/TimescaleDB：单人分钟级场景，运维与内存成本远超收益。
- 否决 Kafka/Redis Streams：单进程 asyncio 事件总线足够，引入消息中间件纯属负担。
- Parquet 列存 + zstd，分钟线一个品种一年约数 MB 级，磁盘余量可支撑数十品种多年数据。

## 已知限制
- SQLite 单写入者：引擎与 Web 同进程共享连接池，若将来拆进程需改为 PG 或加写入代理。
- 全市场长周期回测在本开发机跑不动，须搬 VPS 或 Windows 机（见 dev-machine 约束）。
