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


_RUNUP_HIT = {"flag": True, "top": False, "excess": 0.12, "stock_ret": 0.15, "index_ret": 0.03, "cutoff": 0.08}
_RUNUP_MISS = {**_RUNUP_HIT, "flag": False, "excess": 0.01}


@pytest.mark.parametrize(
    "decision, advice, price, change_pct",
    [
        ("buy", "买入", 33.4, 0.0),      # 接近压力
        ("buy", "买入", 32.0, 0.0),      # 支撑与压力之间
        ("buy", "买入", 35.0, 0.0),      # 已突破压力
        ("sell", "卖出", 30.4, -2.1),    # 贴近支撑
        ("sell", "卖出", 29.0, -3.0),    # 跌破支撑
    ],
)
@pytest.mark.parametrize("runup", [None, _RUNUP_MISS])
def test_price_levels_no_longer_change_the_decision(decision, advice, price, change_pct, runup) -> None:
    """2026-10-16 起支撑 / 压力价位不再改判：八种算法都不比随机价位准（Macd-Qlib-Analyzer sr_levels_reaction）。
    资金流也不参与（capital_flow_eval）。"""
    for context in _FLOW_CONTEXTS.values():
        result = _result(decision_type=decision, operation_advice=advice, score=60, current_price=price,
                         change_pct=change_pct)
        stabilize_decision_with_structure(
            result, SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]), context, recent_runup=runup,
        )
        assert (result.decision_type, result.operation_advice) == (decision, advice)
        assert result.dashboard["core_conclusion"]["one_sentence"] == "原始结论"


def test_recent_runup_downgrades_buy_to_watch() -> None:
    """近 5 日涨幅居全市场前 10% 时「买入」改判观望：之后 10 日多数跑输（reversal_flow_robustness，t≈9）。"""
    result = _result(decision_type="buy", operation_advice="买入", score=70, current_price=32.0)
    result.dashboard["battle_plan"] = {"position_strategy": {"suggested_position": "建议仓位：3成"}}

    stabilize_decision_with_structure(
        result, SimpleNamespace(support_levels=[30.0], resistance_levels=[34.0]), None, recent_runup=_RUNUP_HIT,
    )

    assert result.decision_type == "hold"
    assert result.operation_advice == "观望"
    assert result.sentiment_score <= 59
    assert "追涨" in result.risk_warning
    assert result.dashboard["decision_stability"]["applied"] is True
    advice = result.dashboard["core_conclusion"]["position_advice"]
    assert advice["no_position"].endswith("（程序改写：结构稳定规则）")
    assert advice["has_position"].endswith("（程序改写：结构稳定规则）")
    assert "支撑" not in advice["no_position"] and "风控线" not in advice["has_position"]
    assert result.dashboard["battle_plan"]["position_strategy"]["suggested_position"] == (
        "建议仓位：3成（模型原建议；结论已被结构稳定规则改为观望）"
    )


def test_recent_runup_infers_buy_from_natural_phrase() -> None:
    result = _result(decision_type="建议买入", operation_advice="建议买入", score=66, current_price=32.0)
    stabilize_decision_with_structure(result, None, None, recent_runup=_RUNUP_HIT)
    assert result.decision_type == "hold"


def test_recent_runup_leaves_sell_and_hold_alone() -> None:
    sell = _result(decision_type="sell", operation_advice="卖出", score=30, current_price=30.4)
    stabilize_decision_with_structure(sell, None, None, recent_runup=_RUNUP_HIT)
    assert (sell.decision_type, sell.operation_advice) == ("sell", "卖出")

    hold = _result(decision_type="hold", operation_advice="持有", score=52, current_price=30.5)
    stabilize_decision_with_structure(hold, None, None, recent_runup=_RUNUP_HIT)
    assert (hold.decision_type, hold.operation_advice) == ("hold", "持有")
    assert hold.dashboard["core_conclusion"]["one_sentence"] == "原始结论"
