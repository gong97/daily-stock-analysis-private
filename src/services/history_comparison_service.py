# -*- coding: utf-8 -*-
"""
===================================
Report Engine - History Comparison Service
===================================

Fetches recent analysis signal changes per stock for report rendering.
Excludes current record via exclude_query_id.
"""

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from src.storage import DatabaseManager
from src.report_language import normalize_report_language
from src.schemas.decision_action import display_action_fields
from src.schemas.decision_scale import extract_decision_guardrail_reason
from src.utils.data_processing import parse_json_field
from src.utils.sniper_points import find_sniper_points, parse_sniper_value

logger = logging.getLogger(__name__)

# 「今日重大变化」查最近几天的历史，用来在跨节假日/停牌时仍能找到上一个
# 交易日的记录；90 天是 get_signal_changes 已经在用的窗口，这里沿用同一个
# 值而不是另起一个数字。
_PREVIOUS_DECISION_LOOKBACK_DAYS = 90
_PREVIOUS_DECISION_QUERY_LIMIT = 20


def _record_to_signal(
    record: Any,
    *,
    report_language: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Convert AnalysisHistory record to signal dict. Skip on parse error."""
    raw_result = parse_json_field(getattr(record, "raw_result", None))
    if not isinstance(raw_result, dict):
        raw_result = {}

    operation_advice = raw_result.get("operation_advice") or getattr(record, "operation_advice", None)
    explicit_action = raw_result.get("action")
    action_label = raw_result.get("action_label")
    resolved_report_language = normalize_report_language(
        report_language
        or raw_result.get("report_language")
        or getattr(record, "report_language", None)
    )
    action_fields = display_action_fields(
        operation_advice=operation_advice,
        explicit_action=explicit_action,
        action_label=action_label,
        report_type=getattr(record, "report_type", None),
        report_language=resolved_report_language,
        sentiment_score=getattr(record, "sentiment_score", None),
        guardrail_reason=extract_decision_guardrail_reason(raw_result),
    )

    try:
        return {
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "query_id": record.query_id,
            "sentiment_score": record.sentiment_score,
            "operation_advice": record.operation_advice,
            "action": action_fields["action"],
            "action_label": action_fields["action_label"],
            "trend_prediction": record.trend_prediction,
        }
    except Exception as e:
        logger.debug("Skip record for history comparison: %s", e)
        return None


def get_signal_changes(
    code: str,
    limit: int = 5,
    exclude_query_id: Optional[str] = None,
    *,
    report_language: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Get recent signal changes for a single stock.

    Args:
        code: Stock code
        limit: Max records to return
        exclude_query_id: Exclude record with this query_id (e.g. current run)

    Returns:
        List of signal dicts (created_at, sentiment_score, operation_advice, trend_prediction)
    """
    db = DatabaseManager.get_instance()
    records = db.get_analysis_history(
        code=code,
        days=90,
        limit=limit,
        exclude_query_id=exclude_query_id,
    )
    out = []
    for r in records:
        sig = _record_to_signal(r, report_language=report_language)
        if sig:
            out.append(sig)
    return out


def get_signal_changes_batch(
    codes: List[str],
    limit: int = 5,
    exclude_query_ids: Optional[Dict[str, str]] = None,
    *,
    report_language: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Get recent signal changes for multiple stocks.

    Args:
        codes: Stock codes
        limit: Max records per stock
        exclude_query_ids: Map code -> query_id to exclude per stock

    Returns:
        Dict mapping code -> list of signal dicts
    """
    exclude_query_ids = exclude_query_ids or {}
    db = DatabaseManager.get_instance()
    result: Dict[str, List[Dict[str, Any]]] = {c: [] for c in codes}
    for code in codes:
        exclude = exclude_query_ids.get(code)
        records = db.get_analysis_history(
            code=code,
            days=90,
            limit=limit,
            exclude_query_id=exclude,
        )
        for r in records:
            sig = _record_to_signal(r, report_language=report_language)
            if sig:
                result[code].append(sig)
    return result


def _record_sniper_points(record: Any) -> Dict[str, Optional[float]]:
    """优先用表上的独立 Float 列；缺失时回退解析 raw_result JSON。"""
    points: Dict[str, Optional[float]] = {}
    for key in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit"):
        points[key] = getattr(record, key, None)

    if all(v is None for v in points.values()):
        raw_result = parse_json_field(getattr(record, "raw_result", None))
        if isinstance(raw_result, dict):
            found = find_sniper_points(raw_result)
            if found:
                for key in points:
                    points[key] = parse_sniper_value(found.get(key))

    return points


def _record_to_previous_decision(
    record: Any,
    *,
    report_language: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """跟 _record_to_signal 同样的 action 归一化口径，另加止损/目标价。"""
    raw_result = parse_json_field(getattr(record, "raw_result", None))
    if not isinstance(raw_result, dict):
        raw_result = {}

    operation_advice = raw_result.get("operation_advice") or getattr(record, "operation_advice", None)
    explicit_action = raw_result.get("action")
    action_label = raw_result.get("action_label")
    resolved_report_language = normalize_report_language(
        report_language
        or raw_result.get("report_language")
        or getattr(record, "report_language", None)
    )
    action_fields = display_action_fields(
        operation_advice=operation_advice,
        explicit_action=explicit_action,
        action_label=action_label,
        report_type=getattr(record, "report_type", None),
        report_language=resolved_report_language,
        sentiment_score=getattr(record, "sentiment_score", None),
        guardrail_reason=extract_decision_guardrail_reason(raw_result),
    )

    try:
        sniper = _record_sniper_points(record)
        return {
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "sentiment_score": record.sentiment_score,
            "action": action_fields["action"],
            "action_label": action_fields["action_label"],
            "stop_loss": sniper.get("stop_loss"),
            "take_profit": sniper.get("take_profit"),
            "ideal_buy": sniper.get("ideal_buy"),
            "secondary_buy": sniper.get("secondary_buy"),
        }
    except Exception as e:
        logger.debug("Skip record for previous-decision comparison: %s", e)
        return None


def get_previous_decisions_batch(
    codes: List[str],
    *,
    report_type: str,
    before_date: Optional[date] = None,
    report_language: Optional[str] = None,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """取每只股票「上一个交易日、同一 report_type」的那一行分析结果，
    作为「今日重大变化」对比的基线。

    同一只股票同一天可能已经写了两行（Tier 1 BRIEF + Tier 2 FULL，各自
    独立的 query_id），所以不能靠 exclude_query_id 排除今天的记录——必须
    先按日期把 before_date（默认今天）当天及之后的行全部丢弃，再在剩下的
    行里挑 report_type 匹配的最新一行。跨 report_type 对比（比如拿 Tier 1
    的基线去比 Tier 2 的结果）会把模型档位差异误判成信号变化。

    Args:
        codes: 股票代码列表
        report_type: 要匹配的 report_type（如 "full"、"brief"），必须与
            当前结果的 report_type 一致
        before_date: 排除这一天及之后的记录，默认今天
        report_language: 传给 action 归一化的语言

    Returns:
        Dict[code, 基线 dict 或 None]。取不到基线（首次运行、库查询失败、
        90 天内没有同 report_type 的历史）时该股票的值是 None，调用方
        应当把它当作「无基线，跳过该股票的变化判定」处理，不抛异常。
    """
    cutoff_date = before_date or date.today()
    result: Dict[str, Optional[Dict[str, Any]]] = {c: None for c in codes}

    try:
        db = DatabaseManager.get_instance()
    except Exception as e:
        logger.warning("[previous_decisions] 无法获取数据库实例，跳过历史对比: %s", e)
        return result

    for code in codes:
        try:
            records = db.get_analysis_history(
                code=code,
                days=_PREVIOUS_DECISION_LOOKBACK_DAYS,
                limit=_PREVIOUS_DECISION_QUERY_LIMIT,
            )
        except Exception as e:
            logger.warning("[previous_decisions] 查询 %s 历史失败: %s", code, e)
            continue

        for record in records:
            created_at = getattr(record, "created_at", None)
            if not isinstance(created_at, datetime):
                continue
            if created_at.date() >= cutoff_date:
                continue
            if getattr(record, "report_type", None) != report_type:
                continue
            decision = _record_to_previous_decision(record, report_language=report_language)
            if decision:
                result[code] = decision
            break

    return result
