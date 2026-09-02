#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 history/ 重算观察名单的 hit_count。

`hit_count` 计的是**被选中的扫描日数**，通过 `rank_score()` 的命中加分
（每多一次 +2，上限 +10）参与行业配额和容量裁剪。

2026-08-30 之前 `merge_run()` 每调用一次就 +1，`seen_this_run` 只在单次调用内
去重，因此同一天手动重跑几轮 weekly 会把计数灌到 6/7——那是运行次数在选股，
不是策略。该缺陷已在 46a0470 修复（同一天重复运行不再重复计数），但**已经写进
current.json 的历史数据不会自愈**：`merge_run` 只做增量，从不回头重算。

本脚本按 `data/watchlist/history/<日期>-<频率>.json` 重放，统计每只票真正出现过
的**不同扫描日**数量，覆盖 current.json 里的 `hit_count`。history 是每轮运行的
原始候选留档，是这个数字唯一可考证的来源。

用法::

    python scripts/repair_watchlist_hit_count.py --dry-run   # 只报告差异
    python scripts/repair_watchlist_hit_count.py             # 写回

只跑一次即可；修复后的正常运行会自己维持正确计数。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.services.screening_watchlist import (  # noqa: E402
    load_watchlist,
    save_watchlist,
)


def count_hits_from_history(history_dir: Path) -> dict[str, int]:
    """统计每只票在 history 里出现过的不同扫描日数。

    同一天的 daily 与 weekly 是两个文件但只算一天——`hit_count` 的语义是
    「被选中的扫描日数」，同一天多次命中不重复计数。
    """
    days_by_code: dict[str, set[str]] = defaultdict(set)
    for path in sorted(history_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"跳过无法解析的历史文件 {path.name}: {exc}", file=sys.stderr)
            continue
        run_date = str(payload.get("run_date") or "").strip()
        if not run_date:
            print(f"跳过缺少 run_date 的历史文件 {path.name}", file=sys.stderr)
            continue
        for picks in (payload.get("picks") or {}).values():
            for pick in picks or []:
                code = str(pick.get("code") or "").strip()
                if code:
                    days_by_code[code].add(run_date)
    return {code: len(days) for code, days in days_by_code.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 history/ 重算观察名单的 hit_count")
    parser.add_argument("--watchlist-dir", default="data/watchlist", help="观察名单目录")
    parser.add_argument("--dry-run", action="store_true", help="只报告差异，不写回")
    args = parser.parse_args(argv)

    out_dir = Path(args.watchlist_dir)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    current_path = out_dir / "current.json"
    history_dir = out_dir / "history"

    if not current_path.exists():
        print(f"找不到名单文件: {current_path}", file=sys.stderr)
        return 1
    if not history_dir.exists():
        print(f"找不到历史目录: {history_dir}", file=sys.stderr)
        return 1

    entries, meta = load_watchlist(current_path)
    truth = count_hits_from_history(history_dir)
    if not truth:
        print("history/ 里没有可用的候选记录，放弃重算（避免把计数全部清零）", file=sys.stderr)
        return 1

    changes: list[tuple[str, int, int]] = []
    for code, entry in entries.items():
        # history 里查不到的条目（如 pinned 占位、或早于留档的条目）保持原值：
        # 缺记录不等于没命中过，清零会误伤。
        actual = truth.get(code)
        if actual is None or actual == entry.hit_count:
            continue
        changes.append((code, entry.hit_count, actual))

    if not changes:
        print("hit_count 与 history 一致，无需修复")
        return 0

    changes.sort(key=lambda item: item[1] - item[2], reverse=True)
    print(f"{'代码':<10}{'当前':>6}{'实际':>6}{'差值':>6}")
    for code, before, after in changes:
        print(f"{code:<10}{before:>6}{after:>6}{before - after:>+6}")
    print(f"\n共 {len(changes)} 条")

    if args.dry_run:
        print("--dry-run：未写回")
        return 0

    for code, _, after in changes:
        entries[code].hit_count = after
    meta["hit_count_repaired_from_history"] = True
    save_watchlist(current_path, entries, meta)
    print(f"已写回 {current_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
