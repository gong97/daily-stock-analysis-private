# -*- coding: utf-8 -*-
"""Tests for structure-aware decision stability calibration."""

from types import SimpleNamespace

import pytest

from src.analyzer import AnalysisResult, stabilize_decision_with_structure


def _result(
    *,
    decision_type: str,
    operation_advice: str,
    score: int,
    current_price: float,
    change_pct: float = 0.0,
) -> AnalysisResult:
    return AnalysisResult(
        code="002812",
        name="恩捷股份",
        sentiment_score=score,
        trend_prediction="看多" if decision_type == "buy" else "看空",
        operation_advice=operation_advice,
        decision_type=decision_type,
        report_language="zh",
        current_price=current_price,
        change_pct=change_pct,
        dashboard={
            "core_conclusion": {"one_sentence": "原始结论"},
            "data_perspective": {
                "price_position": {
                    "current_price": current_price,
                    "support_level": 30.0,
                    "resistance_level": 34.0,
                }
            },
        },
    )


def _fund_flow(main: float, five_day: float = 0.0, ten_day: float = 0.0) -> dict:
    return {
        "capital_flow": {
            "status": "ok",
            "data": {
                "stock_flow": {
                    "main_net_inflow": main,
                    "inflow_5d": five_day,
                    "inflow_10d": ten_day,
                }
            },
        }
    }


def _unsupported_fund_flow() -> dict:
    return {"capital_flow": {"status": "not_supported", "data": {}}}


_FLOW_CONTEXTS = {
    "inflow": _fund_flow(main=5_000_000, five_day=8_000_000, ten_day=9_000_000),
    "outflow": _fund_flow(main=-5_000_000, five_day=-8_000_000, ten_day=-9_000_000),
    "unsupported": _unsupported_fund_flow(),
    "absent": None,
}


@pytest.mark.parametrize(
    "decision, advice, price, change_pct, expected_decision, expected_advice",
    [
        ("buy", "买入", 33.4, 0.0, "hold", "震荡观望"),     # 接近压力
        ("buy", "买入", 32.0, 0.0, "hold", "震荡观望"),     # 支撑与压力之间
        ("buy", "买入", 35.0, 0.0, "buy", "买入"),          # 有效突破，保留买入
        ("sell", "卖出", 30.4, -2.1, "hold", "洗盘观察"),   # 贴近支撑
        ("sell", "卖出", 29.0, -3.0, "sell", "卖出"),       # 跌破支撑，保留卖出
    ],
)
def test_capital_flow_no_longer_changes_the_decision(
    decision, advice, price, change_pct, expected_decision, expected_advice
) -> None:
    """2026-10-04 起资金流不参与稳定性规则：流入、流出、取不到，结论完全一样，只看价格结构。

    出处：Macd-Qlib-Analyzer scripts/capital_flow_eval.py——主力净流入最强的股票之后 10 个交易日
    反而多数跑输，扣除前期涨跌后没有信息。以前「资金流缺失就降级买入」「流入改写卖出」都已去掉。
    """
    outcomes = {}
    for name, context in _FLOW_CONTEXTS.items():
        result = _result(
            decision_type=decision, operation_advice=advice, score=60, current_price=price, change_pct=change_pct
        )
        stabilize_decision_with_structure(
            result, SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]), context
        )
        outcomes[name] = (result.decision_type, result.operation_advice)
        assert "capital_flow_bias" not in result.dashboard.get("decision_stability", {})

    assert set(outcomes.values()) == {(expected_decision, expected_advice)}, outcomes


def test_downgrades_buy_near_resistance() -> None:
    result = _result(
        decision_type="buy",
        operation_advice="买入",
        score=65,
        current_price=33.4,
    )

    stabilize_decision_with_structure(
        result,
        SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]),
        _fund_flow(main=-1_000_000, five_day=-2_000_000),
    )

    assert result.decision_type == "hold"
    assert result.sentiment_score <= 59
    assert result.operation_advice == "震荡观望"
    assert result.dashboard["decision_stability"]["applied"] is True
    assert "不宜仅因短线反弹追买" in result.risk_warning
    assert result.dashboard["core_conclusion"]["signal_type"] == "🟡持有观望"


def test_downgrades_buy_mid_range() -> None:
    result = _result(
        decision_type="buy",
        operation_advice="买入",
        score=66,
        current_price=32.0,
    )

    stabilize_decision_with_structure(
        result,
        SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]),
        _fund_flow(main=0, five_day=0, ten_day=0),
    )

    assert result.decision_type == "hold"
    assert result.sentiment_score <= 59
    assert result.operation_advice == "震荡观望"
    assert "价格处于支撑与压力之间" in result.risk_warning


def test_skips_downgrade_when_only_generic_risk_warning_and_sell_near_support() -> None:
    result = _result(
        decision_type="sell",
        operation_advice="卖出",
        score=30,
        current_price=30.4,
        change_pct=1.0,
    )
    result.risk_warning = "注意常见回撤风险，建议关注仓位。"

    stabilize_decision_with_structure(
        result,
        SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]),
        _fund_flow(main=500_000, five_day=300_000),
    )

    assert result.decision_type == "hold"
    assert result.operation_advice == "洗盘观察"
    assert "价格贴近支撑且未跌破" in result.risk_warning


def test_stability_can_infer_decision_from_natural_chinese_phrases_in_analyzer_path() -> None:
    result = _result(
        decision_type="建议卖出",
        operation_advice="建议卖出",
        score=30,
        current_price=30.4,
        change_pct=1.0,
    )

    stabilize_decision_with_structure(
        result,
        SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]),
        _fund_flow(main=500_000, five_day=300_000),
    )

    assert result.decision_type == "hold"
    assert result.operation_advice == "洗盘观察"
    assert result.dashboard["decision_stability"]["applied"] is True


def test_downgrades_sell_near_support_without_sustained_outflow() -> None:
    result = _result(
        decision_type="sell",
        operation_advice="卖出",
        score=30,
        current_price=30.4,
        change_pct=-2.1,
    )

    stabilize_decision_with_structure(
        result,
        SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]),
        _fund_flow(main=800_000, five_day=1_200_000),
    )

    assert result.decision_type == "hold"
    assert result.sentiment_score >= 45
    assert result.operation_advice == "洗盘观察"
    assert "不宜仅因单日下跌直接卖出" in result.risk_warning


def test_insider_selling_alone_does_not_count_as_significant_risk() -> None:
    """减持只作提示、不参与决策（2026-09-30 检验：股东减持无效，高管减持只作弱参考）。"""
    result = _result(
        decision_type="sell",
        operation_advice="卖出",
        score=30,
        current_price=30.4,
        change_pct=-2.1,
    )
    result.dashboard["intelligence"] = {"risk_alerts": ["2026-09-25 弱参考：近期高管减持（董事减持2.00万）",
                                                        "2026-09-20 股东高位减持预告"]}

    stabilize_decision_with_structure(
        result,
        SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]),
        _fund_flow(main=800_000, five_day=1_200_000),
    )

    assert result.decision_type == "hold"
    assert result.operation_advice == "洗盘观察"


def test_preserves_sell_signal_when_significant_risk_exists_near_support() -> None:
    result = _result(
        decision_type="sell",
        operation_advice="卖出",
        score=30,
        current_price=30.4,
        change_pct=-2.1,
    )
    result.risk_warning = "重大利空消息：公司发布重大减持计划"
    result.dashboard["intelligence"] = {"risk_alerts": ["股东高位减持预告"]}

    stabilize_decision_with_structure(
        result,
        SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]),
        _fund_flow(main=800_000, five_day=1_200_000),
    )

    assert result.decision_type == "sell"
    assert result.operation_advice == "卖出"


def test_refines_hold_pullback_near_support_as_shakeout_watch() -> None:
    result = _result(
        decision_type="hold",
        operation_advice="持有",
        score=52,
        current_price=30.5,
        change_pct=-1.6,
    )

    stabilize_decision_with_structure(
        result,
        SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]),
        _fund_flow(main=0, five_day=500_000),
    )

    assert result.decision_type == "hold"
    assert result.operation_advice == "洗盘观察"
    assert "更适合按洗盘观察处理" in result.risk_warning


def test_structural_rewrite_is_labelled_in_position_advice() -> None:
    """程序改写的仓位建议带来源标注；模型原填的「建议仓位」不改写，只注明结论已改为观望。"""
    result = _result(decision_type="sell", operation_advice="卖出", score=30, current_price=30.4, change_pct=-2.1)
    result.dashboard["battle_plan"] = {"position_strategy": {"suggested_position": "建议仓位：2成"}}

    stabilize_decision_with_structure(
        result,
        SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]),
        _fund_flow(main=800_000, five_day=1_200_000),
    )

    advice = result.dashboard["core_conclusion"]["position_advice"]
    assert advice["no_position"].endswith("（程序改写：结构稳定规则）")
    assert advice["has_position"].endswith("（程序改写：结构稳定规则）")
    assert result.dashboard["battle_plan"]["position_strategy"]["suggested_position"] == (
        "建议仓位：2成（模型原建议；结论已被结构稳定规则改为观望）"
    )
