# 观察名单（全市场扫描产物）

本目录由 `scripts/market_scan.py` 维护，调度见 `.github/workflows/10-market-scan.yml`，
契约说明见 `docs/screening-engine.md` 的「全市场扫描与观察名单」。

`.gitignore` 对 `/data/*` 开了例外只保留本目录，因此这里的文件会被提交回仓库。

| 文件 | 谁写 | 内容 |
| --- | --- | --- |
| `current.json` | 脚本 | 名单真源：**逐策略背书**（分数/最近入选日/所属桶）、首次入选日、命中次数、行业、风险标记；`bucket` / `buckets` 由背书派生 |
| `current.csv` | 脚本 | 同一份名单的扁平表 |
| `STOCK_LIST.txt` | 脚本 | 逗号分隔代码，便于手工取用（需 `--write-stock-list`）；日报流程不消费它 |
| `timing.json` | 脚本 | 最近 20 次运行的各策略耗时，用于按实测调频 |
| `history/` | 脚本 | 每次运行的原始候选与分数 |
| `latest_report.md` | 脚本 | 最近一次的 Markdown 报告 |
| `industry_map.csv` | `scripts/refresh_industry_map.py` | code → 行业/概念 的静态映射表，建议每月刷新；只含成分归属，不含热度字段 |
| `pinned.txt` | 人工 | 手工固定的代码，永不淘汰且排在名单最前 |
| `cache/industry/` | 脚本 | akshare 行业/概念板块映射缓存，随仓库版本化以避免每轮重打约 162 次请求 |

除 `pinned.txt` 外的文件都会被下一次扫描覆盖，不要手工编辑。

观察名单是独立于日报的一条流水线：扫描结果通过独立的报告邮件推送，
**不会**并入日报的 `STOCK_LIST`（那是持仓股列表）。原因见
`docs/screening-engine.md` 的「与日报的关系：两条独立流水线」。

`pinned.txt` 里刚加、还没被任何策略扫中的代码会以空壳条目进入名单
（无名称/行业/价格，分数为 0），下次被扫中时才补齐这些字段。

行业映射缓存的 TTL 判定用文件 mtime，而 `actions/checkout` 每次都会把 mtime 重置成"刚刚"，因此提交进仓库的缓存在 runner 上不会自动过期。需要刷新时用 workflow_dispatch 的`refresh_industry_map=true`（建议每月一次）。
