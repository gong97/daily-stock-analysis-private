# -*- coding: utf-8 -*-
"""分层分析的每日信号记录：把邮件里给出的判断原样落盘，供事后检验。

为什么要单独写一份
------------------
CI 每次都在全新的 runner 上跑，SQLite 库不缓存也不上传，所以自带的回测
（backtest_service）每次都是 processed=0——这个项目的判断从来没被检验过。
LLM 的判断又不能拿历史数据回放（模型知道后来发生了什么，当时的新闻也拼
不回来），只能从现在开始向前积累。

这份记录由 workflow 提交进版本库（data/signal_log/YYYY-MM.jsonl），一行一只
股票一次运行。只追加、不去重：同一天重跑会多出几行，留给检验脚本按
「运行后第一个交易日 + 代码」决定取哪一行——那是检验口径的一部分，应该
和判据一起预先登记，不在写入时替它做决定。

日期口径
--------
定时任务名义上是北京时间 23:00，实际常被 GitHub 推迟到次日凌晨 2~5 点
开跑，行情快照里的 date 因此可能是「次日」甚至周六。所以这里不自己推断
交易日，只记录 run_at_utc（真实运行时刻）和快照原样的日期；检验时以
「run_at 之后第一个开盘」为买入点，不会偷看未来。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.tiered_analysis import ADD_ACTIONS, CUT_ACTIONS, TieredAnalysisOutcome
from src.schemas.decision_action import display_action_fields_for_result
from src.utils.sniper_points import extract_sniper_points

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "data" / "signal_log"


def is_enabled() -> bool:
    """只在 CI 里打开：本地试跑的结果不应混进检验样本。"""
    return os.getenv("SIGNAL_LOG_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dashboard_of(result: Any) -> Dict[str, Any]:
    dashboard = getattr(result, "dashboard", None)
    return dashboard if isinstance(dashboard, dict) else {}


def _price_with_source(result: Any, dashboard: Dict[str, Any]) -> tuple:
    """现价及其来源。来源要记下来：LLM 回填的价格可能是编的，检验时要能剔掉。"""
    snapshot = getattr(result, "market_snapshot", None)
    if isinstance(snapshot, dict):
        for key, source in (("price", "realtime"), ("close", "close")):
            value = _float_or_none(snapshot.get(key))
            if value is not None:
                return value, source
    price_position = dashboard.get("data_perspective", {}).get("price_position", {})
    value = _float_or_none(price_position.get("current_price"))
    if value is not None:
        return value, "llm"
    return None, None


def _bucket(action: Optional[str]) -> str:
    if action in ADD_ACTIONS:
        return "add"
    if action in CUT_ACTIONS:
        return "cut"
    return "hold"


def _judgement(result: Any) -> Dict[str, Any]:
    """一次分析里与检验有关的全部字段；初筛和复核共用同一套。"""
    dashboard = _dashboard_of(result)
    action = display_action_fields_for_result(result)["action"]
    sniper = extract_sniper_points(result)
    price_position = dashboard.get("data_perspective", {}).get("price_position", {})
    return {
        "model": getattr(result, "model_used", None),
        "action": action,
        "raw_action": getattr(result, "action", None),
        "bucket": _bucket(action),
        "score": getattr(result, "sentiment_score", None),
        "time_sensitivity": dashboard.get("core_conclusion", {}).get("time_sensitivity"),
        "stop_loss": sniper.get("stop_loss"),
        "take_profit": sniper.get("take_profit"),
        "ideal_buy": sniper.get("ideal_buy"),
        "secondary_buy": sniper.get("secondary_buy"),
        "support": _float_or_none(price_position.get("support_level")),
        "resistance": _float_or_none(price_position.get("resistance_level")),
    }


def build_signal_records(
    outcome: TieredAnalysisOutcome,
    *,
    run_at: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """每只初筛成功的股票一行；进了深度复核的，复核结论放在 deep 里。"""
    run_at = (run_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidates = {c.code: c for c in outcome.candidates}

    records: List[Dict[str, Any]] = []
    for result in outcome.lite_results:
        if not getattr(result, "success", False):
            continue
        code = getattr(result, "code", "")
        dashboard = _dashboard_of(result)
        price, price_source = _price_with_source(result, dashboard)
        snapshot = getattr(result, "market_snapshot", None)

        candidate = candidates.get(code)
        deep = None
        if candidate is not None and candidate.deep_result is not None:
            deep = _judgement(candidate.deep_result)

        records.append(
            {
                "schema": SCHEMA_VERSION,
                "run_at_utc": run_at.isoformat(timespec="seconds"),
                "snapshot_date": snapshot.get("date") if isinstance(snapshot, dict) else None,
                "code": code,
                "name": getattr(result, "name", ""),
                "price": price,
                "price_source": price_source,
                "lite": _judgement(result),
                "deep_side": candidate.side if candidate is not None else None,
                "deep": deep,
            }
        )
    return records


def append_signal_log(
    outcome: TieredAnalysisOutcome,
    *,
    directory: Optional[Path] = None,
    run_at: Optional[datetime] = None,
) -> Optional[Path]:
    """追加到 <directory>/<运行月份 UTC>.jsonl，返回写入的文件；没有可写的行时返回 None。"""
    run_at = (run_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    records = build_signal_records(outcome, run_at=run_at)
    if not records:
        return None

    directory = Path(directory) if directory is not None else DEFAULT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_at:%Y-%m}.jsonl"
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    logger.info("[signal_log] 已追加 %d 条到 %s", len(records), path)
    return path
