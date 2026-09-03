from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.history_comparison_service import (
    _record_to_signal,
    get_previous_decisions_batch,
)


def _record(**overrides):
    values = {
        "created_at": datetime(2026, 7, 11, 9, 0),
        "query_id": "q1",
        "sentiment_score": 72,
        "operation_advice": "Hold",
        "trend_prediction": "Bullish",
        "report_type": "stock",
        "report_language": "en",
        "raw_result": "{}",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _decision_record(**overrides):
    """跟 _record 类似，但补上 get_previous_decisions_batch 需要的狙击点位列。"""
    values = {
        "created_at": datetime(2026, 7, 11, 9, 0),
        "query_id": "q1",
        "sentiment_score": 55,
        "operation_advice": "Hold",
        "report_type": "full",
        "report_language": "zh",
        "raw_result": "{}",
        "ideal_buy": None,
        "secondary_buy": None,
        "stop_loss": None,
        "take_profit": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_history_signal_uses_score_aligned_display_action() -> None:
    signal = _record_to_signal(_record(), report_language="en")

    assert signal["action"] == "buy"
    assert signal["action_label"] == "Buy"


def test_history_signal_preserves_applied_guardrail() -> None:
    signal = _record_to_signal(
        _record(
            raw_result=(
                '{"action":"hold","dashboard":{"decision_stability":'
                '{"applied":true,"reason":"Wait for confirmation"}}}'
            )
        ),
        report_language="en",
    )

    assert signal["action"] == "hold"
    assert signal["action_label"] == "Hold"


def _mock_db_with_records(records_by_code):
    mock_db = MagicMock()
    mock_db.get_analysis_history.side_effect = (
        lambda code, **kwargs: records_by_code.get(code, [])
    )
    return mock_db


def test_previous_decisions_skips_todays_rows_of_either_report_type() -> None:
    """回归防线：同一只股票今天有 Tier1(brief) + Tier2(full) 两行，
    取 full 基线时不能被今天的任何一行污染——必须拿到昨天的 full 行。
    """
    today = date(2026, 8, 20)
    records = [
        # 今天两行，倒序排列（get_analysis_history 本身就是 created_at desc）
        _decision_record(
            created_at=datetime(2026, 8, 20, 15, 0),
            report_type="full",
            sentiment_score=90,
            stop_loss=999.0,
        ),
        _decision_record(
            created_at=datetime(2026, 8, 20, 9, 0),
            report_type="brief",
            sentiment_score=91,
            stop_loss=998.0,
        ),
        # 昨天的 full 行——这才是应该被选中的基线
        _decision_record(
            created_at=datetime(2026, 8, 19, 15, 0),
            report_type="full",
            sentiment_score=66,
            operation_advice="Hold",
            stop_loss=298.0,
            take_profit=322.0,
        ),
        _decision_record(
            created_at=datetime(2026, 8, 19, 9, 0),
            report_type="brief",
            sentiment_score=60,
            stop_loss=297.0,
        ),
    ]
    mock_db = _mock_db_with_records({"300750.SZ": records})

    with patch(
        "src.services.history_comparison_service.DatabaseManager.get_instance",
        return_value=mock_db,
    ):
        result = get_previous_decisions_batch(
            ["300750.SZ"], report_type="full", before_date=today
        )

    baseline = result["300750.SZ"]
    assert baseline is not None
    assert baseline["stop_loss"] == 298.0
    assert baseline["take_profit"] == 322.0
    assert baseline["sentiment_score"] == 66


def test_previous_decisions_returns_none_when_no_history() -> None:
    mock_db = _mock_db_with_records({"603986.SH": []})

    with patch(
        "src.services.history_comparison_service.DatabaseManager.get_instance",
        return_value=mock_db,
    ):
        result = get_previous_decisions_batch(["603986.SH"], report_type="full")

    assert result["603986.SH"] is None


def test_previous_decisions_returns_none_when_only_other_report_type_exists() -> None:
    """昨天只跑过 brief（比如那天没触发分层分析），取 full 基线应为 None，
    不能退回去用 brief 的行冒充。"""
    records = [
        _decision_record(
            created_at=datetime(2026, 8, 19, 9, 0),
            report_type="brief",
            stop_loss=297.0,
        ),
    ]
    mock_db = _mock_db_with_records({"300750.SZ": records})

    with patch(
        "src.services.history_comparison_service.DatabaseManager.get_instance",
        return_value=mock_db,
    ):
        result = get_previous_decisions_batch(
            ["300750.SZ"], report_type="full", before_date=date(2026, 8, 20)
        )

    assert result["300750.SZ"] is None


def test_previous_decisions_db_failure_does_not_raise() -> None:
    with patch(
        "src.services.history_comparison_service.DatabaseManager.get_instance",
        side_effect=RuntimeError("db unavailable"),
    ):
        result = get_previous_decisions_batch(["300750.SZ"], report_type="full")

    assert result["300750.SZ"] is None


def test_previous_decisions_falls_back_to_raw_result_sniper_points() -> None:
    """独立 Float 列都是 None 时，回退解析 raw_result JSON 里的止损/目标价。"""
    records = [
        _decision_record(
            created_at=datetime(2026, 8, 19, 9, 0),
            report_type="full",
            raw_result=(
                '{"dashboard":{"battle_plan":{"sniper_points":'
                '{"stop_loss":"止损位：298元","take_profit":"目标位：322元"}}}}'
            ),
        ),
    ]
    mock_db = _mock_db_with_records({"300750.SZ": records})

    with patch(
        "src.services.history_comparison_service.DatabaseManager.get_instance",
        return_value=mock_db,
    ):
        result = get_previous_decisions_batch(
            ["300750.SZ"], report_type="full", before_date=date(2026, 8, 20)
        )

    baseline = result["300750.SZ"]
    assert baseline["stop_loss"] == 298.0
    assert baseline["take_profit"] == 322.0
