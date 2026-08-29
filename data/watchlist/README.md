# 观察名单（全市场扫描产物）

本目录由 `scripts/market_scan.py` 维护，调度见 `.github/workflows/10-market-scan.yml`，
契约说明见 `docs/screening-engine.md` 的「全市场扫描与观察名单」。

`.gitignore` 对 `/data/*` 开了例外只保留本目录，因此这里的文件会被提交回仓库。

| 文件 | 谁写 | 内容 |
| --- | --- | --- |
| `current.json` | 脚本 | 名单真源：入选策略与分数、首次/最近入选日、命中次数、行业、风险标记 |
| `current.csv` | 脚本 | 同一份名单的扁平表 |
| `STOCK_LIST.txt` | 脚本 | 逗号分隔代码，供日报流程当作 `STOCK_LIST`（需 `--write-stock-list`） |
| `timing.json` | 脚本 | 最近 20 次运行的各策略耗时，用于按实测调频 |
| `history/` | 脚本 | 每次运行的原始候选与分数 |
| `latest_report.md` | 脚本 | 最近一次的 Markdown 报告 |
| `pinned.txt` | 人工 | 手工固定的代码，永不淘汰且排在名单最前 |

除 `pinned.txt` 外的文件都会被下一次扫描覆盖，不要手工编辑。
