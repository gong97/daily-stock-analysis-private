#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成/刷新 code → 行业/概念 的静态映射表。

为什么要静态表而不是每次现拉：

- **行业归属变化很慢**（月度量级），没必要每轮扫描都重新获取。
- **快照源之间不一致**：sina 完全没有行业列，em_datacenter 有。哪个源胜出取决于
  当轮各源的可用性，因此同一轮里不同策略可能拿到不同口径的数据——实测就出现过
  `balanced_alpha` 走 sina（5 条候选行业全空）而其余 6 个策略走 em_datacenter 的情况。
- **akshare 板块接口在 CI 上不可用**：它走 push2.eastmoney.com，在 GitHub Actions 上
  稳定 502 / RemoteDisconnected。

映射表通过 ``INDUSTRY_MAP_FILES`` 在 `enrich_industry_concepts()` 里生效，而该函数在
**硬筛和评分之前**执行，因此行业配额、策略内的 portfolio_profile、theme_heat 与
topic_alignment 都能拿到数据。它只填补空值（`_apply_industry_column` 的
``current.eq("") & incoming.ne("")``），永远不会覆盖快照自带的更新鲜的行业。

使用方法::

    python scripts/refresh_industry_map.py                    # 写默认路径
    python scripts/refresh_industry_map.py --out data/x.csv   # 指定输出
    python scripts/refresh_industry_map.py --source sina      # 指定快照源（需带行业列）

建议每月刷新一次并提交。表里**不包含**任何热度字段——那些每天都变，
由 provider 或快照在运行时提供。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("refresh_industry_map")

DEFAULT_OUT = "data/watchlist/industry_map.csv"
# em_datacenter 的 sty 里带 INDUSTRY/CONCEPT，且它用的 data.eastmoney.com
# 是 CI 上唯一稳定可达的东财 host。
DEFAULT_SOURCE = "em_datacenter"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成 code → 行业/概念 的静态映射表",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出路径（.csv 或 .json）")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="快照源，必须带行业列")
    parser.add_argument(
        "--min-rows",
        type=int,
        default=3000,
        help="行业非空的行数低于该值时判定为抓取异常，拒绝覆盖已有映射表",
    )
    parser.add_argument("--debug", action="store_true", help="打开 DEBUG 日志")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from src.services.screening.industry import save_industry_map
    from src.services.screening.snapshot import fetch_cn_snapshot

    logger.info("正在拉取快照：source=%s", args.source)
    df = fetch_cn_snapshot(args.source)

    if "industry" not in df.columns:
        logger.error(
            "快照源 %s 不返回行业列，无法生成映射表（sina 就是这种情况）", args.source
        )
        return 2

    mapping: dict[str, dict[str, object]] = {}
    for _, row in df.iterrows():
        code = str(row.get("code") or "").strip()
        industry = str(row.get("industry") or "").strip()
        if not code or not industry:
            continue
        # 只保留成分归属，不保留任何热度字段：热度每天都变，静态表存了就是错的。
        mapping[code] = {
            "industry": industry,
            "concepts": str(row.get("concepts") or "").strip(),
        }

    if len(mapping) < args.min_rows:
        logger.error(
            "只解析出 %s 条有效行业映射，低于 --min-rows=%s，判定为抓取异常，"
            "不覆盖已有映射表",
            len(mapping),
            args.min_rows,
        )
        return 1

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path
    saved = save_industry_map(mapping, out_path)

    industries = {item["industry"] for item in mapping.values()}
    logger.info(
        "已写出 %s：%s 只股票、%s 个行业（快照 %s 行）",
        saved,
        len(mapping),
        len(industries),
        len(df),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
