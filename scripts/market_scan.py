#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全市场扫描并维护观察名单。

按策略 YAML 的 ``style.holding_period`` 把内置选股策略分成不同扫描频率
（``short_term`` → daily，``swing`` / ``watchlist`` → weekly），逐个跑
``src.services.screening.pipeline.screen()`` 的全市场扫描，把候选合并进
``data/watchlist/current.json``，落库并推送独立的观察名单报告。

观察名单是独立于日报的一条流水线：它**不会**写进日报的 ``STOCK_LIST``。
``STOCK_LIST`` 是持仓股列表（见 ``src/core/tiered_analysis.py`` 的前提），
把扫描候选并进去会让「该减仓」一侧对未持仓的票失去意义。

使用方法::

    python scripts/market_scan.py --cadence weekly
    python scripts/market_scan.py --cadence daily --notify
    python scripts/market_scan.py --strategies dual_low,quality_value --dry-run

默认关闭 LLM 重排（``--use-llm`` 开启），因此单次扫描不消耗 LLM 额度，
结果对同一份快照是确定性的。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.services.screening_watchlist import (  # noqa: E402
    DEFAULT_MAX_PER_INDUSTRY,
    DEFAULT_MAX_PER_INDUSTRY_BY_BUCKET,
    DEFAULT_MAX_SIZE,
    DEFAULT_MAX_SIZE_BY_BUCKET,
    DEFAULT_TTL_DAYS,
    DEFAULT_TTL_DAYS_BY_BUCKET,
    RunSummary,
    apply_pinned,
    expire_entries,
    format_date,
    load_pinned_codes,
    load_watchlist,
    merge_run,
    parse_bucket_limits,
    parse_cadence_map,
    render_report,
    resolve_cadence,
    save_watchlist,
    save_watchlist_csv,
    select_strategies,
    to_stock_list,
    update_timing_log,
)

logger = logging.getLogger("market_scan")

DEFAULT_WATCHLIST_DIR = "data/watchlist"
# 中国内地市场时区，用于判定"今天"是哪个交易日。
_CN_TZ = timezone(timedelta(hours=8))


def _env_text(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _env_int(name: str, default: int) -> int:
    raw = _env_text(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数，回退默认值 %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_text(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="全市场扫描并维护观察名单",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--cadence",
        default="weekly",
        choices=["daily", "weekly", "all"],
        help="按 holding_period 分层选择要跑的策略；--strategies 会覆盖该选择",
    )
    parser.add_argument(
        "--strategies",
        default="",
        help="显式指定策略名（逗号分隔），忽略 --cadence 分层",
    )
    parser.add_argument("--market", default="cn", choices=["cn", "us"], help="扫描市场")
    parser.add_argument(
        "--max-output",
        type=int,
        default=None,
        help="覆盖每个策略的入选条数；不传时用策略 YAML 里的 max_output",
    )
    parser.add_argument(
        "--out-dir",
        default=_env_text("WATCHLIST_DIR", DEFAULT_WATCHLIST_DIR),
        help="观察名单输出目录",
    )
    parser.add_argument(
        "--ttl-days",
        default=_env_text("WATCHLIST_TTL_DAYS"),
        help=(
            "超过该天数没有再被任何策略选中就移出名单；0 表示不淘汰。"
            "支持按桶配置，如 'defensive:45,balanced:30,aggressive:14'；"
            "留空使用各桶默认值"
        ),
    )
    parser.add_argument(
        "--max-size",
        default=_env_text("WATCHLIST_MAX_SIZE"),
        help=(
            "每个桶的容量上限；0 表示不限制。pinned 条目不占用也不会被裁掉。"
            "支持按桶配置，如 'defensive:25,balanced:20,aggressive:15'"
        ),
    )
    parser.add_argument(
        "--max-per-industry",
        default=_env_text("WATCHLIST_MAX_PER_INDUSTRY"),
        help=(
            "同一行业在单个桶内最多保留几只；0 表示不限制。"
            "这是跨策略的集中度控制，选股引擎的组合分散只在单个策略内生效。"
            "支持按桶配置，如 'defensive:2,aggressive:4'"
        ),
    )
    parser.add_argument("--use-llm", action="store_true", help="开启 L2 LLM 重排（默认关闭）")
    parser.add_argument(
        "--force-run",
        action="store_true",
        help="跳过交易日检查",
    )
    parser.add_argument("--notify", action="store_true", help="通过 DSA 通知渠道推送报告")
    parser.add_argument("--save-db", action="store_true", help="把每个策略的运行写入 screening_runs 表")
    parser.add_argument(
        "--write-stock-list",
        action="store_true",
        help="额外导出一份逗号分隔的 STOCK_LIST.txt，便于手工取用；日报流程不消费它",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="照常执行扫描，但不写任何文件、不落库、不推送",
    )
    parser.add_argument("--debug", action="store_true", help="打开 DEBUG 日志")
    return parser


def _resolve_run_date(market: str) -> date:
    """扫描日期按市场所在时区取，避免 UTC runner 把收盘后的运行算到前一天。"""
    if market == "cn":
        return datetime.now(_CN_TZ).date()
    return datetime.now().date()


def _is_trading_day(market: str, run_date: date) -> bool:
    """交易日判定；日历不可用时 fail-open 返回 True。"""
    try:
        from src.core.trading_calendar import is_market_open
    except Exception as exc:  # pragma: no cover - 依赖缺失时不阻断扫描
        logger.warning("交易日历不可用，按交易日处理: %s", exc)
        return True
    return bool(is_market_open(market, run_date))


def _run_one_strategy(
    *,
    strategy: str,
    holding_period: str,
    cadence: str,
    market: str,
    max_output: Optional[int],
    use_llm: bool,
    config: Any,
) -> tuple[RunSummary, List[Dict[str, Any]], Optional[Any]]:
    """跑一个策略，返回 (摘要, 候选 dict 列表, 原始 ScreenResult)。

    单个策略失败不会中断整轮扫描：异常写进 RunSummary.error，由调用方决定退出码。
    """
    from src.services.screening.pipeline import screen

    summary = RunSummary(strategy=strategy, cadence=cadence, holding_period=holding_period)
    started = time.monotonic()
    try:
        result = screen(
            strategy,
            market=market,
            max_output=max_output,
            use_llm=use_llm,
            config=config,
        )
    except Exception as exc:
        summary.elapsed_sec = time.monotonic() - started
        summary.error = f"{type(exc).__name__}: {exc}"
        logger.error("策略 %s 扫描失败（继续跑其余策略）: %s", strategy, summary.error)
        return summary, [], None

    summary.elapsed_sec = time.monotonic() - started
    summary.snapshot_count = int(getattr(result, "snapshot_count", 0) or 0)
    summary.after_filter_count = int(getattr(result, "after_filter_count", 0) or 0)
    summary.daily_enriched = bool(getattr(result, "daily_enriched", False))
    summary.snapshot_source = str(getattr(result, "snapshot_source", "") or "")
    summary.run_id = str(getattr(result, "run_id", "") or "")
    summary.degradation = [str(item) for item in (getattr(result, "degradation", None) or [])]

    picks = [asdict(pick) for pick in (getattr(result, "picks", None) or [])]
    summary.pick_count = len(picks)
    logger.info(
        "策略 %s 完成：快照 %s → 硬筛 %s → 入选 %s，耗时 %.1fs",
        strategy,
        summary.snapshot_count,
        summary.after_filter_count,
        summary.pick_count,
        summary.elapsed_sec,
    )
    return summary, picks, result


def _save_run_to_db(result: Any, picks: Sequence[Dict[str, Any]]) -> None:
    """写入 screening_runs 表。DB 不可用时只 warning，不影响扫描产物。"""
    try:
        from src.storage import DatabaseManager
    except Exception as exc:
        logger.warning("数据库模块不可用，跳过选股运行落库: %s", exc)
        return
    payload = {
        "run_id": str(getattr(result, "run_id", "") or ""),
        "strategy": str(getattr(result, "strategy", "") or ""),
        "market": str(getattr(result, "market", "") or ""),
        "snapshot_source": str(getattr(result, "snapshot_source", "") or ""),
        "snapshot_count": getattr(result, "snapshot_count", None),
        "after_filter_count": getattr(result, "after_filter_count", None),
        "candidate_count": len(picks),
        "candidates": list(picks),
        "llm_ranked": bool(getattr(result, "llm_ranked", False)),
        "daily_enriched": bool(getattr(result, "daily_enriched", False)),
        "source_errors": [str(item) for item in (getattr(result, "source_errors", None) or [])],
        "degradation": [str(item) for item in (getattr(result, "degradation", None) or [])],
    }
    try:
        DatabaseManager().save_screening_run(payload)
    except Exception as exc:
        logger.warning("选股运行落库失败（fail-open）: %s", exc)


def _notify(report: str, *, run_date: date, cadence: str) -> None:
    """推送报告。通知不可用时只 warning，不影响扫描产物。"""
    try:
        from src.notification import get_notification_service
    except Exception as exc:
        logger.warning("通知模块不可用，跳过推送: %s", exc)
        return
    try:
        service = get_notification_service()
        sent = service.send(
            report,
            # 观察名单属于报告类通知，走 NOTIFICATION_REPORT_CHANNELS 的渠道过滤。
            route_type="report",
            dedup_key=f"market_scan:{cadence}:{format_date(run_date)}",
            # 显式指定主题，否则邮件渠道会套用日报的「股票智能分析报告」。
            email_subject=f"📈 全市场扫描报告 - {format_date(run_date)}",
        )
        logger.info("观察名单报告推送%s", "成功" if sent else "失败（无可用渠道）")
    except Exception as exc:
        logger.warning("观察名单报告推送失败（fail-open）: %s", exc)


def _write_github_summary(report: str) -> None:
    """把报告写进 GitHub Actions 的 Job Summary（本地运行时是 no-op）。"""
    summary_path = _env_text("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(report)
            handle.write("\n")
    except OSError as exc:
        logger.warning("写入 GITHUB_STEP_SUMMARY 失败: %s", exc)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not _env_bool("SCREENING_ENABLED", False):
        logger.error(
            "SCREENING_ENABLED 未开启，拒绝执行全市场扫描。"
            "请在 .env 或 CI 变量中设置 SCREENING_ENABLED=true。"
        )
        return 2

    run_date = _resolve_run_date(args.market)
    if not args.force_run and not _is_trading_day(args.market, run_date):
        logger.info("%s 不是 %s 市场的交易日，跳过本次扫描（--force-run 可强制执行）",
                    format_date(run_date), args.market)
        return 0

    from src.services.screening.config import Config
    from src.services.screening.strategy import list_strategies

    config = Config.from_env()
    cadence_map = parse_cadence_map(_env_text("WATCHLIST_CADENCE_MAP"))

    explicit = [item.strip() for item in str(args.strategies or "").split(",") if item.strip()]
    infos = list_strategies(config.strategies_dir)
    if explicit:
        by_name = {str(getattr(info, "name", "")): info for info in infos}
        unknown = [name for name in explicit if name not in by_name]
        if unknown:
            logger.error("未知策略：%s（可用：%s）", ", ".join(unknown), ", ".join(sorted(by_name)))
            return 2
        planned = []
        for name in explicit:
            style = getattr(by_name[name], "style", None) or {}
            holding_period = str(style.get("holding_period", "") if isinstance(style, dict) else "")
            planned.append((name, holding_period))
    else:
        planned = select_strategies(
            infos,
            cadence=args.cadence,
            cadence_map=cadence_map,
            market=args.market,
        )

    if not planned:
        logger.warning("没有匹配 cadence=%s market=%s 的策略，本次不执行", args.cadence, args.market)
        return 0

    # 名单分桶取自策略 YAML 的 style.risk_profile，与 cadence 是两根不同的轴：
    # oversold_reversal 是 balanced 但跑 daily，所以不能拿 cadence 代替。
    risk_profiles: Dict[str, str] = {}
    for info in infos:
        style = getattr(info, "style", None) or {}
        risk_profiles[str(getattr(info, "name", ""))] = str(
            style.get("risk_profile", "") if isinstance(style, dict) else ""
        )

    logger.info(
        "本次扫描 cadence=%s market=%s 策略=%s",
        args.cadence,
        args.market,
        ", ".join(name for name, _ in planned),
    )

    summaries: List[RunSummary] = []
    picks_by_strategy: Dict[str, List[Dict[str, Any]]] = {}
    holding_periods: Dict[str, str] = {}
    for name, holding_period in planned:
        cadence = resolve_cadence(holding_period, cadence_map)
        summary, picks, result = _run_one_strategy(
            strategy=name,
            holding_period=holding_period,
            cadence=cadence,
            market=args.market,
            max_output=args.max_output,
            use_llm=args.use_llm,
            config=config,
        )
        summaries.append(summary)
        holding_periods[name] = holding_period
        if picks:
            picks_by_strategy[name] = picks
        if args.save_db and result is not None and not args.dry_run:
            _save_run_to_db(result, picks)

    succeeded = [item for item in summaries if not item.error]
    if not succeeded:
        logger.error("全部 %s 个策略均执行失败，观察名单保持不变", len(summaries))
        return 1

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    current_path = out_dir / "current.json"

    entries, meta = load_watchlist(current_path)
    before_codes = set(entries)
    entries, added = merge_run(
        entries,
        picks_by_strategy,
        run_date=run_date,
        holding_periods=holding_periods,
        risk_profiles=risk_profiles,
        cadence_map=cadence_map,
    )
    entries = apply_pinned(entries, load_pinned_codes(out_dir / "pinned.txt"))
    ttl_limits = parse_bucket_limits(
        args.ttl_days, defaults=DEFAULT_TTL_DAYS_BY_BUCKET, scalar_default=DEFAULT_TTL_DAYS
    )
    size_limits = parse_bucket_limits(
        args.max_size, defaults=DEFAULT_MAX_SIZE_BY_BUCKET, scalar_default=DEFAULT_MAX_SIZE
    )
    industry_limits = parse_bucket_limits(
        args.max_per_industry,
        defaults=DEFAULT_MAX_PER_INDUSTRY_BY_BUCKET,
        scalar_default=DEFAULT_MAX_PER_INDUSTRY,
    )
    entries, removed = expire_entries(
        entries,
        run_date=run_date,
        ttl_days=ttl_limits,
        max_size=size_limits,
        max_per_industry=industry_limits,
    )
    # 本次刚进来又立刻被容量裁掉的，不算"新进"。
    added = [code for code in added if code in entries]

    report = render_report(
        run_date=run_date,
        cadence=args.cadence,
        entries=entries,
        added=added,
        removed=removed,
        summaries=summaries,
    )

    logger.info(
        "名单 %s → %s（新进 %s，移出 %s）",
        len(before_codes),
        len(entries),
        len(added),
        len(removed),
    )

    if args.dry_run:
        logger.info("--dry-run：跳过写文件、落库和推送")
        print(report)
        return 0

    meta.update({
        "last_run_date": format_date(run_date),
        "last_run_cadence": args.cadence,
        "last_run_market": args.market,
        "last_run_strategies": [name for name, _ in planned],
        "last_run_failed_strategies": [item.strategy for item in summaries if item.error],
        "llm_ranked": bool(args.use_llm),
        "ttl_days": ttl_limits,
        "max_size": size_limits,
        "max_per_industry": industry_limits,
        "updated_at": datetime.now(_CN_TZ).isoformat(timespec="seconds"),
    })
    save_watchlist(current_path, entries, meta)
    save_watchlist_csv(out_dir / "current.csv", entries)
    update_timing_log(out_dir / "timing.json", summaries, run_date=run_date, cadence=args.cadence)

    history_dir = out_dir / "history"
    history_path = history_dir / f"{format_date(run_date)}-{args.cadence}.json"
    _write_history(history_path, run_date=run_date, cadence=args.cadence,
                   summaries=summaries, picks_by_strategy=picks_by_strategy)

    if args.write_stock_list:
        codes = to_stock_list(entries, run_date=run_date)
        stock_list_path = out_dir / "STOCK_LIST.txt"
        stock_list_path.parent.mkdir(parents=True, exist_ok=True)
        stock_list_path.write_text(",".join(codes) + "\n", encoding="utf-8")
        logger.info("已写出 %s（%s 只）", stock_list_path, len(codes))

    report_path = out_dir / "latest_report.md"
    report_path.write_text(report, encoding="utf-8")
    _write_github_summary(report)

    if args.notify or _env_bool("WATCHLIST_NOTIFY", False):
        _notify(report, run_date=run_date, cadence=args.cadence)

    print(report)
    return 0


def _write_history(
    path: Path,
    *,
    run_date: date,
    cadence: str,
    summaries: Sequence[RunSummary],
    picks_by_strategy: Dict[str, List[Dict[str, Any]]],
) -> None:
    """留一份本次运行的原始候选，用于回溯"当时为什么选它"。"""
    payload = {
        "run_date": format_date(run_date),
        "cadence": cadence,
        "summaries": [item.to_dict() for item in summaries],
        "picks": {
            strategy: [
                {
                    "rank": pick.get("rank"),
                    "code": pick.get("code"),
                    "name": pick.get("name"),
                    "final_score": pick.get("final_score"),
                    "screen_score": pick.get("screen_score"),
                    "industry": pick.get("industry"),
                    "price": pick.get("price"),
                    "change_pct": pick.get("change_pct"),
                    "risk_level": pick.get("risk_level"),
                    "risk_flags": pick.get("risk_flags") or [],
                    "factor_scores": pick.get("factor_scores") or {},
                }
                for pick in picks
            ]
            for strategy, picks in picks_by_strategy.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
