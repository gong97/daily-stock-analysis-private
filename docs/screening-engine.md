# 选股引擎

DSA 将选股能力作为主项目的一部分维护。实现参考 [AlphaSift](https://github.com/ZhuLinsen/alphasift) 提交 [`9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf`](https://github.com/ZhuLinsen/alphasift/commit/9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf)，并按 Apache License 2.0 修改和分发。衍生文件保留来源头，许可证位于 `src/services/screening/LICENSE`，第三方声明见根目录 `THIRD_PARTY_NOTICES.md`。

## 代码边界

- `src/services/screening/`：快照、日 K、策略加载、过滤、评分、风险、LLM 重排与热点实现。
- `src/services/screening/strategies/`：随 DSA 版本发布的策略 YAML。
- `src/services/screening/pipeline.py`：筛选流程的直接入口。
- `src/services/screening_service.py`：DSA 业务编排，直接调用 pipeline，负责配置、数据源上下文、响应归一化、缓存与错误映射。
- `src/storage.py`：使用 DSA 现有 SQLAlchemy/SQLite 基础设施持久化已完成的选股运行，不另建文件数据库。
- `api/v1/endpoints/screening.py`：`/api/v1/screening` API。
- `src/services/screening_watchlist.py`：全市场扫描观察名单的分层、合并、淘汰与报告渲染逻辑。
- `scripts/market_scan.py` 与 `.github/workflows/10-market-scan.yml`：定时全市场扫描的编排入口与调度。
- `apps/dsa-web/src/api/screening.ts` 与 `StockScreeningPage.tsx`：Web 调用与展示。

服务层静态调用 `screening.pipeline`、`screening.strategy` 和 `screening.hotspot`。核心逻辑不通过模块名探测、动态适配器或多套路由分发，因此代码结构、错误边界和打包收集目标均由主项目直接定义。

## 配置

默认关闭：

```dotenv
SCREENING_ENABLED=false
```

Web“基础设置”页展示“选股”开关。开启后左侧显示“选股”入口并允许执行策略、热点和选股任务；关闭后入口隐藏，选股 API 继续拒绝业务请求。

常用可选项：

```dotenv
SCREENING_DATA_DIR=data/screening
SCREENING_SNAPSHOT_CACHE_TTL_SEC=300
SCREENING_SOURCE_CALL_TIMEOUT_SEC=
SCREENING_HOTSPOT_CALL_TIMEOUT_SEC=8
SCREENING_HOTSPOT_SEARCH_TIMEOUT_SEC=12
SCREENING_SNAPSHOT_CALL_TIMEOUT_SEC=60
SCREENING_DAILY_CALL_TIMEOUT_SEC=20
SCREENING_EASTMONEY_MIN_INTERVAL_SEC=1.0
SCREENING_EASTMONEY_JITTER_SEC=0.3
```

路径、缓存、超时和限流项只影响选股链路。`SCREENING_SNAPSHOT_CACHE_TTL_SEC` 默认 300 秒，设为 `0` 可关闭新鲜快照复用。`SCREENING_HOTSPOT_CALL_TIMEOUT_SEC` 的正数值是默认热点 provider 单次板块、成分股或直接详情 fallback 调用的总预算，后续 fallback 和并行成分股源共用同一截止时间，并把剩余时间传入可终止的 AkShare 子进程与 HTTP socket；设为 `0/off/disabled` 只关闭这层总预算，各真实数据源仍保留自身硬超时。`SCREENING_HOTSPOT_SEARCH_TIMEOUT_SEC` 是用户主动新闻搜索的端到端截止时间，缓存 owner 等待、重新竞争和 provider 子进程共享同一个绝对 deadline；`0/off/disabled` 会回退到安全默认值 12 秒，而不是无限等待。完整示例以 `.env.example` 为准。

## API 契约

| 路径 | 方法 | 行为 |
| --- | --- | --- |
| `/api/v1/screening/status` | GET | 返回开关、引擎状态、契约版本、参考项目和数据源健康信息 |
| `/api/v1/screening/strategies` | GET | 返回选股策略 |
| `/api/v1/screening/hotspots` | GET | 读取缓存或显式刷新热点题材 |
| `/api/v1/screening/hotspots/{topic}` | GET | 返回题材路线、成分股与核心股；`include_search=true` 时按需搜索近期消息 |
| `/api/v1/screening/screen` | POST | 同步执行选股；可传匿名 `variant_seed` 在每次运行中生成有界的近分候选组合 |
| `/api/v1/screening/screen/tasks` | POST | 提交后台选股任务；请求字段与同步接口一致 |
| `/api/v1/screening/screen/tasks/{task_id}` | GET | 查询任务进度、错误或最终结果 |
| `/api/v1/screening/history` | GET | 按策略、市场查询最近完成的选股运行摘要 |
| `/api/v1/screening/history/{run_id}` | GET | 读取一条持久化的完整选股结果 |
| `/api/v1/screening/source-history` | GET | 汇总历史运行中的快照源命中、错误和降级次数 |

后台任务使用 `report_type=screening_screen`，Web 会保存活动任务 ID，并在页面恢复时继续轮询。任务状态会分别提示全市场快照、候选上下文、LLM 重排、最终评分和新闻事件增强等阶段；完成后的结果同时写入 DSA 数据库，因此服务重启后仍可按 `run_id` 查询。

## 核心流程

```text
策略加载
  -> 全市场快照与字段标准化
  -> 硬过滤
  -> 因子评分与风险调整
  -> 候选上下文补充
  -> LLM 重排（可降级）
  -> 风险/组合约束与近分候选轮换
  -> Top 候选 DSA 行情/基本面/新闻增强
  -> API 归一化响应与 DSA 数据库持久化
  -> 用户按需进入 DSA 单股深度分析
```

- 全市场快照在短 TTL 内优先复用最近成功结果；新缓存会记录完整且有序的数据源优先级，只在当前优先级与写入时一致时复用，因此同一来源链中的后备源结果可以加速后续请求，修改来源配置后则会重新读取实时数据。缓存过期后按配置优先级逐源尝试，单一数据源失败后继续降级，并记录 source health 与 last-good 缓存。当前 Sina、Efinance、AkShare/东财和 Tushare 快照接口均不提供增量游标或变更序列，因此 TTL 内可以零请求复用，TTL 到期后仍需重新读取全表；本地比较前后差异不能减少上游传输量，不作为“增量拉取”宣传。
- 有 `TUSHARE_TOKEN` 时默认优先 Tushare，否则默认从 Sina 开始；显式 `SNAPSHOT_SOURCE_PRIORITY` 始终优先。
- 日 K 优先通过请求级 fetcher 复用 DSA 历史行情链路，无结果时再走筛选引擎的数据源降级；该桥接不会替换进程级函数，因此重叠选股请求之间不会共享 wrapper 或阻塞彼此。
- LLM 重排前只补充有限候选上下文，最终候选再补行情、基本面、新闻和摘要，控制请求量。
- 默认本地 `scorecard` 会覆盖完整短名单，保证所有可能进入近分轮换的候选使用同一最终评分口径；多个后置分析器串联时，每一步完成后都会按最新分数重排，因此后续 `dsa` 与 `external_http` 的 `POST_ANALYSIS_MAX_PICKS` 上限作用于当前真实前列。远程状态按实际提交候选记录，外部响应中的超限代码不会改写未提交候选；启用远程分析时轮换只会在已完成相同分析的候选之间发生。
- 模型、渠道、base URL、额外 headers、fallback、timeout 和 token 上限在单次调用范围内注入，不改写用户配置；主模型即使 HTTP 调用成功，但返回空内容、非 JSON 或覆盖率不足，也会继续尝试已配置的备用模型。最终 JSON 必须在 `content` 块或 `output` 块中；`reasoning_content`（链式思考）被视为内部辅助，不作为最终结果。
- 热点榜单刷新与选股长流程可并行执行；列表默认不批量预取详情，用户选中具体题材时才加载该题材详情；显式刷新后若继续保留当前题材，Web 会同步绕过详情缓存重拉该题材，保证榜单与详情来自同次刷新。
- 热点成分股并行获取东方财富与同花顺数据，并按固定数据源优先级合并，避免响应先后改变重复股票的字段：正数 `SCREENING_HOTSPOT_CALL_TIMEOUT_SEC` 作为默认 provider 整次调用的共享预算，板块列表、成分股以及引擎失败后的直接详情 fallback 都进入该预算；AkShare/东方财富的可终止子进程与同花顺 HTTP connect/read timeout 每一步只使用剩余时间，fallback 不会重新获得完整预算。直接详情 fallback 不再额外启动无法由该预算强制回收的实时行情预取，行情字段以已受控的成分股源结果为准。超时子进程会 terminate/kill 并回收；进程级并发槽限制活跃任务数量，方法返回前会等待已接纳的 worker 结束，不遗留后台线程。关闭整次调用预算时，各单源仍保留默认硬超时；单源失败不会阻止另一源和本地核心股回退。
- “搜索最新消息”复用 DSA 原生搜索服务的 provider 优先级、SearXNG 公共实例能力、结果缓存与请求合并，同一缓存键只有 owner 可以启动供应商链；owner 未产出可缓存结果时，等待者重新竞争且未获得所有权的请求继续等待，但缓存等待、抢占和 provider 执行共用请求的绝对 deadline，不会在排队后重新获得完整超时。搜索只补充有链接的事件/催化，不从网页内容推断板块成分股；增强记录分别追加到展示路线和原始时间线，不覆盖已有 `timeline`。搜索由用户主动触发，摘要在本地确定性压缩，不调用 LLM；响应以 `available`、`no_results`、`unavailable` 区分有结果、有效空结果和超时/容量/供应商失败，Web 不会再把运行失败提示成“没有近期消息”。搜索增强仅存在于本次响应，不写入或续期共享热点详情缓存，默认详情请求不会看到搜索状态或增加搜索等待。供应商子进程在启动、执行或清理任一阶段失败时都会释放进程级容量。
- 热点实时请求失败时优先使用 last-good cache；无缓存时返回稳定空态与明确错误。

## 结果轮换

Web 会在浏览器本地生成一个不含用户信息的匿名种子，并随同步或后台选股请求传入 `variant_seed`。如果 Web Storage 不可读写，则在当前页面会话的模块内存中复用同一个临时种子，保证同步和后台任务入口一致。服务端将匿名种子与本次运行 ID、市场和策略共同作为扰动输入：不同浏览器以及同一浏览器的不同运行，都可能在质量接近的候选中看到不同股票。

扰动不是随机改分，也不会绕过策略：硬过滤、风险否决、因子/LLM 得分、最终评分和组合集中度惩罚全部先执行。默认本地评分覆盖完整短名单；启用有数量上限的远程后置分析时，只有完成相同分析的候选才可参与轮换。分析器产出的候选顺序是轮换输入的权威顺序，并列分不会再按股票代码重新排序；原 Top-N 的前半部分和明显高于截止分的候选始终受保护，只有后半部分名额可从不低于原截止分 1.5 分的近分池中抽取，入选候选继续保持该输入相对顺序。种子不写入选股结果或运行历史。未传 `variant_seed` 或将轮换比例设为 0 时返回严格输入 Top-N，保持脚本与旧客户端兼容。

## 缓存与持久化

| 数据 | 位置 | 有效期/行为 |
| --- | --- | --- |
| 全市场快照 | `data/screening/snapshot.last_good.json` | 默认 5 分钟内直接复用且不标记 fallback；过期后请求实时源，实时源全部失败时仍可按最大陈旧时间约束回退并标记 stale/fallback |
| 个股日 K | `data/screening/daily_history/` | 按代码、来源和回看窗口分键，默认 TTL 24 小时；实时源全部失败时可使用过期缓存并标记 stale |
| 行业/概念映射 | `data/screening/industry_provider_cache/` | 默认 TTL 24 小时，并保存板块热度历史用于趋势计算 |
| 热点列表与历史 | `data/screening/hotspots.json`、`hotspot.history.jsonl` | 显式刷新写入；实时失败时回退最近可用快照 |
| 热点详情 | `data/screening/hotspot_details/` | 默认 TTL 30 分钟；只缓存结构化基础详情，显式消息搜索不写入或续期该缓存；实时失败时可回退过期详情并返回陈旧时长 |
| DSA 实时行情 | `DataFetcherManager` 的行情缓存 | 默认 TTL 10 分钟，沿用 `REALTIME_CACHE_TTL` |
| DSA 基本面/资金流 | `DataFetcherManager` 的基本面缓存 | 默认 TTL 120 秒，沿用 `FUNDAMENTAL_CACHE_TTL_SECONDS` |
| DSA 新闻/公告事件 | `SearchService` 内存缓存 | 成功结果默认 TTL 10 分钟；同题材并发请求在父进程合并，实际供应商链在限流、可终止的子进程中执行；服务重启后重新查询 |
| 完整选股结果 | DSA 数据库 `screening_runs` 表 | 完成后按 `run_id` 幂等写入；数据库写入失败不阻断选股主流程 |

候选上下文模块也支持 24 小时文件缓存，但 DSA 集成默认关闭其独立新闻/公告抓取，改用 DSA 自己的资讯、基本面和实时行情链路，避免同一候选重复请求两套数据源。

## 行业中性化

`_rank_score()` 默认做**全市场单一分位排名**，没有任何分组。后果是分位反映的是
「这只票所在行业贵不贵」，而不是「这只票在同类里贵不贵」：

| 行业 | PE 中位 | value 因子均值（全市场口径） |
| --- | --- | --- |
| 银行 | 6.1 | **89.1** |
| 非银行金融 | 15.5 | 72.5 |
| 电子元件 | 69.4 | 31.3 |
| 半导体 | 88.0 | 28.1 |

39 只银行的 value 均值 89.1，改成行业内排名后是 51.0——那 38 分**全部来自「它是银行」**。
个股层面更明显：招商银行全市场口径 87.5，但在银行内部只有 25.7（银行里最贵的一档），
全市场口径完全看不出这个差别。

策略 YAML 的 `scoring_profile` 可以按需开启：

```yaml
scoring_profile:
  industry_neutral: true
  industry_neutral_min_size: 10
```

- **只影响 `value` / `liquidity` / `size` 三个分位类因子**——它们是仅有的三个走
  `_rank_score()` 的因子；`momentum` / `stability` / `activity` 是绝对公式，不受影响。
- **行业内有效样本少于 `industry_neutral_min_size`（默认 10）时回退全市场口径**。
  实测 102 个行业里 29 个不足 10 只、最小的只有 1 只，3 只成员的分位只能取
  33/67/100，没有区分度。样本数按**非空值**计——20 只的行业里只有 2 只有 PE，同样是噪声。
- 行业为空的条目一律走全市场口径。

**默认关闭，按策略启用**：

| 策略 | 口径 | 理由 |
| --- | --- | --- |
| `quality_value`、`momentum_quality`、`balanced_alpha` | 行业中性 | 目标是「选好公司」，行业差属于噪声 |
| `dual_low` 等其余 8 个 | 全市场 | `dual_low` 要的就是全市场最便宜的资产，中性化会改变其语义 |

实测对比（同一份快照，各自硬筛后 Top 8）：

```
quality_value（行业中性）  → 银行 1 只，分布在港口航运/建筑施工/输变电设备/专用设备/非银行金融
dual_low（全市场口径）     → 银行 6 只
```

## 两类策略的边界

DSA 中存在两类用途不同的策略文件：

| 位置 | 解决的问题 | 加载方 | 执行阶段 |
| --- | --- | --- | --- |
| `src/services/screening/strategies/*.yaml` | 从全市场筛出哪些候选 | `src/services/screening/strategy.py` | 快照过滤、因子评分、风险和排序 |
| `strategies/*.yaml` | 对单只股票如何分析和形成结论 | `src/agent/skills/base.py` | DSA Agent/报告分析 |

即使 `shrink_pullback`、`volume_breakout` 同名，两者也使用不同目录、Schema 和 loader，不会相互覆盖。筛选策略可通过 `analysis_skills` 声明下一阶段建议使用的分析 skill；Web 的“进一步深度分析”会显式携带这些 skill。未声明映射的筛选策略继续使用用户当前选择或默认分析策略，不做含义不可靠的强行映射。

## 全市场扫描与观察名单

`pipeline.screen()` 本身就是一次全市场扫描（拉全市场快照 → L1 硬筛 → Top-N 补日线 →
因子评分 → L2 排序 → L3 后置分析）。定时把它跑成一份可持续维护的观察名单，由三个部分组成：

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| 名单逻辑 | `src/services/screening_watchlist.py` | 策略分层、名单合并/淘汰与报告渲染；纯逻辑，不触网 |
| 编排入口 | `scripts/market_scan.py` | 按分层批量调用 `pipeline.screen()`，落盘产物、可选落库与推送 |
| 调度 | `.github/workflows/10-market-scan.yml` | daily / weekly 两条 cron，产物提交回仓库 |

`scripts/market_scan.py` 不是第二套选股 CLI：它不重复实现筛选、评分或结果存储，只做批量编排，
筛选仍走 `pipeline.screen()`，运行历史仍写既有的 `screening_runs` 表。

### 按 holding_period 分层调频

扫描频率取自策略 YAML 的 `style.holding_period`，不在代码里硬编码策略名：

| holding_period | 频率 | 内置策略 |
| --- | --- | --- |
| `short_term` | daily（每交易日 17:30 北京时间） | `capital_heat`、`oversold_reversal`、`volume_breakout`、`theme_momentum` |
| `swing` | weekly（每周五 18:30 北京时间） | `momentum_quality`、`low_volatility_quality`、`shrink_pullback` |
| `watchlist` | weekly | `balanced_alpha`、`quality_value`、`dual_low`、`blue_chip_income` |

未识别的 `holding_period` 一律按 weekly 处理，避免新策略被默默拉成每日高频。
`WATCHLIST_CADENCE_MAP=short_term:daily,swing:weekly,watchlist:weekly` 可覆盖该映射，
只接受 `daily` / `weekly`，非法项会被跳过并保留默认值。

分层的成本依据来自两类策略的实际差异：`low_volatility_quality`、`shrink_pullback`、
`volume_breakout` 的硬过滤需要日 K 特征，会逐只拉取最多 `DAILY_ENRICH_MAX_CANDIDATES` 条历史，
是整个工作流最主要的耗时来源；其余策略只消费一次全市场快照。同一进程内跑完一组策略时，
快照（`SCREENING_SNAPSHOT_CACHE_TTL_SEC`）和日 K 缓存（`SCREENING_DAILY_HISTORY_CACHE_TTL_HOURS`）
都会被复用，因此把一组策略放进一个 job 比拆成多个 job 便宜得多。

每次运行的各策略耗时会追加到 `data/watchlist/timing.json`（保留最近 20 次），
这是后续调频的实测依据：某个策略持续逼近 job 超时，就把它降到 weekly，
或调小 `DAILY_ENRICH_MAX_CANDIDATES`。

### 名单产物

全部写在 `WATCHLIST_DIR`（默认 `data/watchlist/`，已在 `.gitignore` 中对 `/data/*` 开例外）：

| 文件 | 内容 |
| --- | --- |
| `current.json` | 名单真源：每只票的入选策略与分数、首次/最近入选日、命中次数、行业、风险标记 |
| `current.csv` | 同一份名单的扁平表，便于直接查看 |
| `STOCK_LIST.txt` | 逗号分隔代码，便于手工取用（需 `--write-stock-list`）；日报流程**不**消费它 |
| `timing.json` | 最近 20 次运行的各策略耗时 |
| `history/<日期>-<频率>.json` | 当次各策略的原始候选与分数，用于回溯"当时为什么选它" |
| `latest_report.md` | 最近一次的 Markdown 报告（新进 / 移出 / 当前名单 / 策略耗时） |
| `pinned.txt` | 手工固定的代码（每行一个，`#` 为注释），永不淘汰且排在名单最前 |
| `cache/industry/` | akshare 行业板块映射缓存，随仓库版本化 |

名单维护规则：`hit_count` 计的是**被选中的扫描日数**——同一次扫描被多个策略同时
选中只算一次，**同一天重复运行也只算一次**。它不是工作流的运行次数：`hit_count` 会通过
名单排序的命中加分（每多一次 +2 分，上限 +10）影响行业配额和容量裁剪，
如果按运行次数累计，手动重跑几轮就能把一只票顶进名单——那是运行次数在选股。
`last_seen` 同理只前进不后退，用更早的日期补跑一轮不会把时间基准拉回去（否则 TTL 会凭空延长）。
超过 `WATCHLIST_TTL_DAYS`（默认 30 天）
没有再被任何策略选中即移出；同一行业最多保留 `WATCHLIST_MAX_PER_INDUSTRY` 只（默认 2）；
名单规模上限 `WATCHLIST_MAX_SIZE`（默认 60），
按「最近得分 + 命中加分 − 陈旧扣分」排序裁剪。`pinned.txt` 中的代码不占名额也不会被裁掉，
因此自动扫描不会冲掉手工长期跟踪的票。

### 与日报的关系：两条独立流水线

全市场扫描和日报是**互不干扰的两条线**，不共享股票列表：

| | 输入 | 产出 |
| --- | --- | --- |
| `10-market-scan.yml` | 全市场快照 | 观察名单 + **独立的观察名单报告邮件** |
| `00-daily-analysis.yml` | `STOCK_LIST`（= 持仓股） | 日报邮件 |

观察名单**不会**写进 `STOCK_LIST`。这是刻意的：`STOCK_LIST` 是持仓股列表，
`src/core/tiered_analysis.py` 的分层逻辑正建立在这个前提上——「该加仓 / 该减仓」
两侧都有实际行动价值，因此不需要再和持仓求交集。一旦把扫描候选并进去，
「该减仓」一侧对未持仓的票就失去了意义，深度复盘名额也会被无效候选占掉。

扫描结果通过 `WATCHLIST_NOTIFY`（工作流里默认 `true`）走 `route_type="report"`
推送，落在 `NOTIFICATION_REPORT_CHANNELS` 配置的渠道上。名单本身以
`data/watchlist/` 提交回仓库，供历史回溯。

### 进攻侧策略 `theme_momentum`

策略库整体偏防守——10 个原生策略里 6 个把当日涨幅上限卡在 +5% 以内，`value` + `stability`
合计权重普遍过半，因此 weekly 名单会系统性地被低估值大盘股占满（首次实跑 19 只里 9 只银行）。
`theme_momentum` 是显式的进攻侧补充，DSA 原生、非 AlphaSift 衍生。

它和另外两个进攻策略的分工：

| 策略 | 主导因子 | 是否需日 K | 覆盖范围 |
| --- | --- | --- | --- |
| `capital_heat` | activity 0.28 | 否 | 全市场 |
| `volume_breakout` | 日 K 形态 | **是** | 硬筛后 Top-N 子集 |
| `theme_momentum` | theme_heat 0.27 | 否 | **全市场** |

两个设计要点：

- **`market_cap_max: 800 亿`** —— 这是纯配置下唯一能把大盘蓝筹挡在门外的手段，
  也是整个策略库里第一处使用该字段。没有它，低 PE / 低波动 / 大成交额的银行会继续在因子层占优。
- **不配任何 `pe_ttm_*` / `pb_*`，也不给 `value` 权重** —— 硬过滤会连同 NaN 一起淘汰
  （`series.notna()`），配了 `pe_ttm_min` 就等于把所有亏损成长股直接剔除；而
  `_compute_value_score` 对 PE≤0 给 `na_score=25`，给 `value` 任何权重都会系统性压制成长股。
  估值风险改由 `risk_profile` 温和处理（`invalid_pe_points` 3.0 → 0.5，`high_pb` 8 → 20）。

进攻需要**五层同时调**，否则设定会被下游原样扣回去：硬筛的 `change_pct_max`、
`momentum_chase_start_pct` / `_penalty_slope`、`stability` 权重、风险层的 `chase_change_pct`、
以及 L3 scorecard 的 `hot_money_penalty` / `volume_spike_penalty`。该策略五层都做了对应放宽。

"拒绝退潮接盘"体现在 `theme_heat_cooling_penalty_slope`（1.4）高于
`theme_heat_trend_slope`（1.2），惩罚上限（18）也高于加分上限（14）。

该策略依赖 `INDUSTRY_PROVIDER` 提供板块数据，`data_requirements` 会自动标记
`industry_context`；没有行业数据时 `theme_heat` 与 `topic_alignment` 退化成常数，
它会失去区分度。它也是第一个使用 `topic_alignment` 因子的策略。

### 名单分桶：防守 / 均衡 / 进攻

名单按策略 YAML 的 `style.risk_profile` 分成三个桶，**TTL、行业配额和容量都按桶独立结算**：

| bucket | 策略 |
| --- | --- |
| `defensive` | blue_chip_income、dual_low、low_volatility_quality、quality_value |
| `balanced` | balanced_alpha、momentum_quality、oversold_reversal、shrink_pullback |
| `aggressive` | capital_heat、theme_momentum、volume_breakout |

一只票可以**同时属于多个桶**。分桶依据存在逐策略的背书记录里
（`strategies: {策略名: {score, last_seen, bucket}}`），`bucket` / `buckets` 都是从它派生的：

| 字段 | 含义 |
| --- | --- |
| `buckets` | 当前仍然有效的全部分桶，按优先级排序 |
| `bucket` | 主桶，用于 TTL、行业配额和容量结算 |

**主桶优先级 `aggressive > balanced > defensive`**，取的不是"哪个更重要"，而是
**让衰减最快的桶治理时效**：进攻属性是当下正在发生的事，14 天不再被确认就该出局；
防守属性是长期底色，掉出名单也随时能被 `dual_low` 选回来。反过来配会让已经退潮的
进攻票靠"它也便宜"赖在名单里 45 天。

**逐策略背书还解决了两个问题**：

1. **分桶不再跨运行漂移**。此前 `bucket` 是存储字段，周一 daily 跑成 `aggressive`、
   周五 weekly 跑成 `defensive`，连带 TTL（14↔45 天）和行业配额（4↔2）一起翻转。
   现在同一份数据永远得出同一个分桶。
2. **TTL 按每条背书自己的桶结算**。进攻策略的背书 14 天失效、防守策略的 45 天；
   条目只要还剩任一条有效背书就留在名单里，全部失效才算 `ttl` 出局。
   此前 `strategies` 只存分数且从不清理，三个月前选过一次的策略会永远挂在那只票上。

同时符合多个桶的票**只在主桶列一行、只占一个名额**，在报告里标注「兼 防守」。
容量上限管的是"你要盯多少只票"，一只票不会因为同时符合两套逻辑就变成两只。

**为什么必须分桶**：`latest_score` 来自 `_rank_score(..., pct=True)`，是**该策略硬筛存活池内的
分位排名**。`dual_low` 池子里 239 只票的 84 分，和 `theme_momentum` 池子里几十只票的 84 分
不是一回事。全局排序会让两把不同的尺子争同一批名额——而防守策略在数量上占优
（11 个策略里 4 个 defensive、4 个 balanced），进攻票会被系统性挤掉。

分桶不等于拆成两份名单：一只票同时被 `dual_low` 和 `theme_momentum` 选中（既便宜又有资金关注）
是最值得看的信号，只有在一份数据里才看得见。`bucket` 与 `cadence` 也是两根不同的轴——
`oversold_reversal` 是 `balanced` 但跑 daily，不能拿 cadence 代替。

各桶默认限额：

| | TTL | 容量 | 同行业上限 |
| --- | --- | --- | --- |
| `defensive` | 45 天 | 25 | 2 |
| `balanced` | 30 天 | 20 | 2 |
| `aggressive` | **14 天** | 15 | **4** |

进攻桶 TTL 更短（short_term 信号两周后基本失效）、行业配额更松（热点扩散天然是同板块多只，
卡 2 只会砍掉信号本身）。三桶容量合计 60，与拆桶前的全局上限一致。

`WATCHLIST_TTL_DAYS` / `WATCHLIST_MAX_SIZE` / `WATCHLIST_MAX_PER_INDUSTRY` 三项都支持
留空（用默认）、`"30"`（三桶统一）、`"30,aggressive:10"`（默认加覆盖）、
`"defensive:45,balanced:30,aggressive:14"`（逐桶指定）四种写法，逐桶配置总是覆盖统一默认值，
与书写顺序无关。未知 `risk_profile` 落到 `balanced`。

报告和 `current.csv` 都按桶分段输出，并明确标注分数不可跨桶比较。

### 行业数据的两级缓存

akshare 板块数据的两半变化速度差一个数量级，而**贵的那半恰好是慢的**：

| 来源 | 请求数 | 产出 | 变化速度 | TTL |
| --- | --- | --- | --- | --- |
| `list_func()` 板块列表 | **2** | `*_heat_score`、`*_rank`、`*_change_pct`、`board_heat_summary` | 每天 | `SCREENING_INDUSTRY_PROVIDER_CACHE_TTL_HOURS`（默认 24，工作流设 20） |
| `cons_func()` 板块成分 | **最多 2×MAX_BOARDS** | `industry`、`concepts` | 月度 | `SCREENING_INDUSTRY_CONSTITUENTS_CACHE_TTL_HOURS`（默认 720） |

因此 `fetch_akshare_board_map()` 把两者分开缓存：成分命中缓存时，一次日度热度刷新只发
**2 个请求**，而不是约 162 个。

**过期判定用缓存 JSON 里的 `created_at`，不用文件 mtime。** 这在 CI 上是决定性的：
`actions/checkout` 会把每个文件的 mtime 重置成检出时刻，随仓库版本化的缓存若按 mtime 判断
将永远"新鲜"，TTL 完全失效。`created_at` 缺失时才退回 mtime。

**热度历史**：每次热度刷新会向 `akshare_board_heat_*.json.history.jsonl` 追加一行/板块
（同一天只记一次），由 `load_board_heat_trends()` 按 5 次观测的滚动窗口算出
`board_heat_trend_score`、`board_heat_persistence_score`、`board_heat_cooling_score`、
`board_heat_observations`。**没有这份历史，这四个字段根本不存在**，任何基于它们调参的策略
（如 `theme_momentum` 的退潮惩罚）都是在对空气打分。历史文件随仓库版本化，
因此需要连续若干个交易日的运行才能积累出有效窗口。

部分板块拉取失败时，本轮仍返回可用映射，但**不写成分缓存**——否则残缺映射会被当成新鲜结果
固化 720 小时。

### 跨策略行业配额

配额分两层，因为桶内配额管不住跨桶叠加：

| 层 | 配置 | 结算范围 | 默认 |
| --- | --- | --- | --- |
| 桶内 | `WATCHLIST_MAX_PER_INDUSTRY` | 每个桶内部 | defensive 2 / balanced 2 / aggressive 4 |
| 全局 | `WATCHLIST_MAX_PER_INDUSTRY_TOTAL` | 整份名单跨桶 | 3 |

只有桶内配额时，9 只银行分散在均衡桶和防守桶、每桶各留 2 只，整份名单仍有 4 只银行。
全局上限作为最后一道兜底，在桶内配额之后执行。

全局上限淘汰谁，不能按分数全局排序——那正是分桶要避免的事（分数跨桶不可比）。
改成**按桶优先级轮流取**：每一轮从每个桶里取该行业排名最高的一只，取满为止。
这样每个含该行业的桶都能先保住自己最好的那只，全程不做跨桶分数比较，
结果与字典序和运行顺序无关。

实测（把真实行业回填到 19 条名单）：

| 全局上限 | 名单规模 | 银行 |
| --- | --- | --- |
| 关闭 | 19 → 13 | 9 → 4 |
| **3（默认）** | 19 → 12 | 9 → 3 |
| 2 | 19 → 10 | 9 → 2 |


选股引擎的 `apply_portfolio_overlay` 只在**单个策略内部**生效：即使 `balanced_alpha` 的
`portfolio_profile` 限制了同一 bucket 最多 1 只，7 个策略各选 1 只银行，名单里仍然会有 7 只银行。
首次实跑就出现了这个结果——19 只候选里有 9 只银行。

`WATCHLIST_MAX_PER_INDUSTRY` 补上这一层，并在**每个桶内**独立结算。淘汰顺序是 TTL → 行业配额 → 容量上限：
行业配额腾出的名额会让给其他行业的候选，而不是浪费掉。`pinned` 条目豁免且不占名额；
行业为空的条目不参与配额（无法分组，强行淘汰会误伤）。

**行业数据来自快照本身**：`em_datacenter` 的 `sty` 参数请求了 `INDUSTRY` 与 `CONCEPT`，
零额外请求、不改变返回行数，实测 5147 行**行业零缺失**、103 个分类。
此前只能靠 `INDUSTRY_PROVIDER=akshare` 补，但它走 `push2.eastmoney.com`，
在 GitHub Actions 上稳定 502 / RemoteDisconnected（连续两轮实测失败），
导致行业列全空、配额静默放行。`data.eastmoney.com`（快照用的那个 host）则一直是通的。

akshare provider 现在只剩板块热度趋势一个用途（供 `theme_momentum`），
它是 fail-open 的，失败只在 degradation 留记录。

**这一层是静默失效的重灾区**：`industry` 为空时条目一律放行（无法分组，强行淘汰会误伤），
于是"配置在、逻辑在、数据不在"时，报告看起来完全正常，只是名单里挤满同一个行业。
首次实跑就踩了这个坑——19 条候选行业全空，9 只银行原样进入名单，而 `max_per_industry` 早已配好。

因此每份报告都会在概览里输出一行诊断，`meta` 里也记 `industry_quota_effective` /
`industry_missing_count`，配额因缺数据而空转时脚本还会打 warning：

```
⚠️ 行业配额未生效：19 条候选全部缺少行业数据，配额无从分组（检查 INDUSTRY_PROVIDER 与板块缓存）
行业配额：已生效，本次裁掉 7 条；另有 2 条缺少行业数据未参与分组
```

这层依赖行业数据，因此工作流把 `INDUSTRY_PROVIDER` 默认设为 `akshare`：**默认快照源
`em_datacenter` 的 `sty` 参数不请求任何行业字段**，不补行业数据的话，不仅这层配额无从分组，
策略内的 `portfolio_profile` 也整个空转、`theme_heat` 因子会退化成对所有候选相同的常数。

代价是首次最多 162 次 akshare 请求（2 个板块列表 + 各 80 个板块成分，逐板块 fail-open）。
缓存目录指向已版本化的 `data/watchlist/cache/industry`，所以这个成本不是每轮都付。
注意缓存 TTL 判定用的是文件 mtime，而 `actions/checkout` 每次都会把 mtime 重置，
因此提交进仓库的缓存在 runner 上不会自动过期——需要刷新时用 workflow_dispatch 的
`refresh_industry_map=true`（建议每月一次）。

### 运行方式

```bash
# 每周组（swing + watchlist）
python scripts/market_scan.py --cadence weekly --save-db

# 每交易日组（short_term），并推送报告
python scripts/market_scan.py --cadence daily --notify

# 指定策略试跑，不写任何产物
python scripts/market_scan.py --strategies dual_low,quality_value --dry-run --force-run
```

扫描默认关闭 L2 LLM 重排（`--use-llm` 开启），因此不消耗 LLM 额度，
对同一份快照结果是确定性的；候选质量的二次判断交给后续的单股深度分析。
脚本受 `SCREENING_ENABLED` 控制，未开启时直接拒绝执行（退出码 2）。
非交易日默认跳过（退出码 0），`--force-run` 可强制运行。
单个策略失败不会中断整轮扫描，只在报告中标记；只有全部策略失败才返回退出码 1 并保持名单不变。

落库沿用 `screening_runs` 表（`--save-db`）。注意 GitHub Actions runner 上的 SQLite 是一次性的，
只有把 `DATABASE_URL` 指向外部数据库时这份运行历史才有持久价值；
名单本身的持久化真源是提交回仓库的 `current.json`。

## DSA 原生能力复用

- 行情：日 K 优先调用 DSA `DataFetcherManager`，无结果才进入筛选模块自己的多源 fallback；最终候选继续补 DSA 实时行情。
- 基本面与资讯：最终候选复用 DSA 基本面上下文和 `SearchService`；资本流向来自 DSA 基本面上下文，重要公告/业绩/减持事件调用 DSA `search_stock_events`，热点消息搜索沿用其数据源优先级、时效过滤、缓存和同请求合并，仅将真实供应商调用隔离到可终止子进程，不重复维护独立资讯入口。
- 模型：沿用 DSA LiteLLM 模型、渠道、fallback、base URL、额外 headers、超时和 token 配置。
- 任务与页面：复用 DSA 后台任务队列、Web 轮询和桌面端同源 Web 资源。
- 存储与后续分析：运行结果写入 DSA 数据库；候选可进入 DSA 原生单股分析并携带策略 skill。

对照固定参考提交，快照、日 K、美股、行业/概念、热点、候选新闻/公告/资金流、字段标准化、过滤、评分、风险、排序和数据源熔断等原始数据与选股能力均已纳入；其中公告/事件和资金流在 DSA 编排层分别接入原生事件搜索与基本面上下文。参考项目另外提供独立 CLI/server、JSON 文件 store、报告渲染、doctor、运行/数据源历史和 T+N 评估：本实现只吸收 DSA 确实缺少的运行历史与数据源历史，并接到 DSA 数据库；CLI/server 不重复建设，T+N 评估与表现统计继续复用 DSA 已有 BacktestService，避免形成第二套回测真源。实时 source health 已在 `/status` 返回，历史稳定性由 `/source-history` 补齐。

## 收益

1. 选股服务、策略、API、Web 和打包脚本在同一版本中演进，避免契约漂移。
2. 服务层只有一套原生调用路径，状态探针和业务请求反映相同实现。
3. Docker 与桌面产物直接收集同一份模块和策略资源，部署结果更一致。
4. 数据源降级、评分和策略变化可以在主仓库完成端到端审查与回归。
5. 来源 commit、许可证和逐文件归因明确，便于后续选择性同步上游修复。

## 风险与控制

| 风险 | 影响 | 控制措施 |
| --- | --- | --- |
| 主仓库维护面扩大 | 数据源或策略问题由 DSA 直接承担 | 模块边界、契约测试和 CI 打包探针共同约束 |
| 与参考项目逐渐分叉 | 上游修复不能直接覆盖 | 固定参考 revision，逐模块比较并选择性移植 |
| 数据源限流或字段变化 | 快照、热点或日 K 降级 | timeout、retry、source health 与 last-good cache |
| LLM 超时或格式异常 | 重排不可用或解释字段缺失 | 非结构化响应继续尝试备用模型；全部失败时保留因子排序，并返回尝试模型与失败原因 |
| 结果轮换扩大候选差异 | 临界候选可能因浏览器不同而变化 | 仅轮换近分尾部位，保持硬过滤、风险否决、分值和头部候选不变；无种子时关闭 |
| 缓存目录变化 | 升级后旧缓存不会自动复用 | 新目录独立为 `data/screening`；升级前按需备份 |
| 运行历史增长 | 完整候选结果会增加数据库体积 | 历史接口默认只读摘要，运维可按现有数据库备份/保留策略管理 |
| 配置与 API 更名 | 旧自动化需同步调整 | 在发布说明明确 `SCREENING_ENABLED` 与 `/api/v1/screening` |
| 许可证归因遗漏 | 发布合规风险 | 保留 LICENSE、THIRD_PARTY_NOTICES 和衍生文件头 |

选股结果仅用于研究和辅助判断，不构成投资建议，也不保证收益或数据完整性。

## 更新参考实现

AlphaSift 是参考来源，不是自动同步源。更新时应：

1. 记录目标 commit 和许可证变化；
2. 比较 `src/services/screening/` 的 DSA 特有修改，按模块选择性移植；
3. 更新衍生文件头、`REFERENCE_REVISION` 和 `THIRD_PARTY_NOTICES.md`；
4. 检查 pipeline、API/Web 字段、数据源降级、策略资源与冻结打包；
5. 更新本文档和 `docs/CHANGELOG.md`，完成后端、Web、Docker/桌面验证。

## 回滚

- 业务回滚：设置 `SCREENING_ENABLED=false` 并重启；普通个股分析、报告、通知和问股不受影响。
- 代码回滚：revert 引入选股引擎的提交并重建后端、Docker 与桌面产物。
- 数据回滚：如需保留选股缓存和运行历史，先备份 `data/screening/` 与 DSA 数据库；代码回滚不会主动删除 `screening_runs` 用户数据。
