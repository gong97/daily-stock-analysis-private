# 全市场扫描观察名单（daily）

- 扫描日期：2026-08-31
- 名单规模：33（防守 5｜均衡 18｜进攻 10）
- 本次新进：0｜本次移出：0
- 策略数：4｜总耗时：341.8s
- 行业配额：已生效，本次裁掉 0 条

## 失败策略

- `capital_heat`：RuntimeError: All snapshot sources failed: sina: missing required columns volume_ratio; efinance: Expecting value: line 1 column 1 (char 0); akshare_em: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')); em_datacenter: returned empty data
- `theme_momentum`：RuntimeError: All snapshot sources failed: sina: missing required columns volume_ratio; efinance: Expecting value: line 1 column 1 (char 0); akshare_em: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')); em_datacenter: returned empty data; last_good_cache: missing required columns volume_ratio
- `volume_breakout`：RuntimeError: All snapshot sources failed: sina: missing required columns volume_ratio; efinance: Expecting value: line 1 column 1 (char 0); akshare_em: ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')); em_datacenter: returned empty data; last_good_cache: missing required columns volume_ratio

## 当前名单 · 防守（5）

| # | 代码 | 名称 | 行业 | 最近分 | 命中日数 | 首次入选 | 最近入选 | 策略 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 600015 | 华夏银行 | 银行 | 84.01 | 6 | 2026-08-30 | 2026-08-31 | dual_low |
| 2 | 601601 | 中国太保 | 非银行金融 | 80.79 | 6 | 2026-08-30 | 2026-08-31 | blue_chip_income, quality_value |
| 3 | 601877 | 正泰电器 | 输变电设备 | 82.79 | 1 | 2026-08-31 | 2026-08-31 | quality_value |
| 4 | 601390 | 中国中铁 | 基础建设 | 81.71 | 1 | 2026-08-31 | 2026-08-31 | dual_low, quality_value |
| 5 | 600028 | 中国石化 | 石油天然气 | 70.20 | 1 | 2026-08-31 | 2026-08-31 | low_volatility_quality |

## 当前名单 · 均衡（18）

| # | 代码 | 名称 | 行业 | 最近分 | 命中日数 | 首次入选 | 最近入选 | 策略 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 601166（兼 防守） | 兴业银行 | 银行 | 92.59 | 6 | 2026-08-30 | 2026-08-31 | balanced_alpha, momentum_quality, oversold_reversal, quality_value |
| 2 | 601919（兼 防守） | 中远海控 | 港口航运 | 84.64 | 6 | 2026-08-30 | 2026-08-31 | balanced_alpha, blue_chip_income, low_volatility_quality, momentum_quality, quality_value, shrink_pullback |
| 3 | 601668（兼 防守） | 中国建筑 | 建筑施工 | 83.88 | 6 | 2026-08-30 | 2026-08-31 | balanced_alpha, blue_chip_income, dual_low, quality_value |
| 4 | 601229（兼 防守） | 上海银行 | 银行 | 81.47 | 6 | 2026-08-30 | 2026-08-31 | balanced_alpha, blue_chip_income, dual_low, quality_value |
| 5 | 601318（兼 防守） | 中国平安 | 非银行金融 | 80.85 | 6 | 2026-08-30 | 2026-08-31 | balanced_alpha, low_volatility_quality, momentum_quality, shrink_pullback |
| 6 | 000157 | 中联重科 | 专用设备 | 87.99 | 1 | 2026-08-31 | 2026-08-31 | oversold_reversal |
| 7 | 600875 | 东方电气 | 电源设备 | 86.96 | 1 | 2026-08-31 | 2026-08-31 | oversold_reversal |
| 8 | 002432 | 九安医疗 | 医疗器械 | 86.45 | 1 | 2026-08-31 | 2026-08-31 | balanced_alpha, momentum_quality, oversold_reversal |
| 9 | 000783 | 长江证券 | 非银行金融 | 76.16 | 6 | 2026-08-30 | 2026-08-31 | shrink_pullback |
| 10 | 600309 | 万华化学 | 化学原料 | 75.62 | 6 | 2026-08-30 | 2026-08-31 | shrink_pullback |
| 11 | 000951 | 中国重汽 | 汽车 | 85.50 | 1 | 2026-08-31 | 2026-08-31 | oversold_reversal |
| 12 | 000807 | 云铝股份 | 基本金属 | 75.00 | 6 | 2026-08-30 | 2026-08-31 | shrink_pullback |
| 13 | 600887 | 伊利股份 | 食品 | 73.12 | 6 | 2026-08-30 | 2026-08-31 | shrink_pullback |
| 14 | 601600 | 中国铝业 | 基本金属 | 71.72 | 5 | 2026-08-30 | 2026-08-31 | shrink_pullback |
| 15 | 000425 | 徐工机械 | 专用设备 | 78.51 | 1 | 2026-08-31 | 2026-08-31 | balanced_alpha, momentum_quality |
| 16 | 002241 | 歌尔股份 | 电子设备制造 | 78.05 | 1 | 2026-08-31 | 2026-08-31 | balanced_alpha, shrink_pullback |
| 17 | 000651（兼 防守） | 格力电器 | 白色家电 | 76.63 | 1 | 2026-08-31 | 2026-08-31 | balanced_alpha, blue_chip_income, low_volatility_quality, momentum_quality, shrink_pullback |
| 18 | 000100 | TCL科技 | 电子元件 | 73.55 | 1 | 2026-08-31 | 2026-08-31 | momentum_quality |

## 当前名单 · 进攻（10）

| # | 代码 | 名称 | 行业 | 最近分 | 命中日数 | 首次入选 | 最近入选 | 策略 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 600316 | 洪都航空 | 航空航天装备 | 79.72 | 1 | 2026-08-31 | 2026-08-31 | volume_breakout |
| 2 | 603596 | 伯特利 | 汽车 | 77.40 | 1 | 2026-08-31 | 2026-08-31 | theme_momentum, volume_breakout |
| 3 | 002430 | 杭氧股份 | 环保 | 75.60 | 1 | 2026-08-31 | 2026-08-31 | capital_heat, theme_momentum, volume_breakout |
| 4 | 002466（兼 均衡） | 天齐锂业 | 稀有金属 | 73.88 | 1 | 2026-08-31 | 2026-08-31 | capital_heat, momentum_quality |
| 5 | 601233（兼 均衡） | 桐昆股份 | 合成纤维及树脂 | 72.68 | 1 | 2026-08-31 | 2026-08-31 | capital_heat, momentum_quality |
| 6 | 002042 | 华孚时尚 | 纺织 | 72.66 | 1 | 2026-08-31 | 2026-08-31 | volume_breakout |
| 7 | 002092 | 中泰化学 | 化学原料 | 72.64 | 1 | 2026-08-31 | 2026-08-31 | volume_breakout |
| 8 | 301297 | 富乐德 | 专业服务 | 72.26 | 1 | 2026-08-31 | 2026-08-31 | capital_heat, theme_momentum |
| 9 | 601360 | 三六零 | 计算机软件 | 71.67 | 1 | 2026-08-31 | 2026-08-31 | capital_heat, theme_momentum |
| 10 | 002129 | TCL中环 | 电源设备 | 69.72 | 1 | 2026-08-31 | 2026-08-31 | theme_momentum |

> 分数是各策略硬筛存活池内的分位排名，**不可跨桶比较**。标「兼 X」的票同时符合多个桶，只在主桶计一个名额。

## 策略耗时

| 策略 | 持有周期 | 耗时(s) | 快照 | 硬筛后 | 入选 | 日线补齐 |
| --- | --- | --- | --- | --- | --- | --- |
| capital_heat | short_term | 88.8 | 0 | 0 | 0 | 否 |
| oversold_reversal | short_term | 74.3 | 5546 | 367 | 5 | 否 |
| theme_momentum | short_term | 88.5 | 0 | 0 | 0 | 否 |
| volume_breakout | short_term | 90.3 | 0 | 0 | 0 | 否 |
