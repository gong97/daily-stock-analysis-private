# -*- coding: utf-8 -*-
"""追涨型买点过滤（src/chase_guard.py）：例句取自 2026-10-14 日报的真实输出。"""

from types import SimpleNamespace

from src.chase_guard import current_price_of, is_chase_buy_point, scrub_chase_text, strip_chase_buy_points
from src.utils.sniper_points import extract_sniper_points


def test_drops_breakout_clause_keeps_pullback_clause() -> None:
    text = "分批建仓策略：若股价回踩29.30元附近且无恐慌盘释放，可建仓1-2成；若右侧放量突破30.50元再补仓1-2成。"
    assert scrub_chase_text(text) == "分批建仓策略：若股价回踩29.30元附近且无恐慌盘释放，可建仓1-2成。"


def test_trims_or_breakout_tail() -> None:
    text = "空仓者建议：暂不急于入场，等待缩量回踩强支撑位（如10.50元附近）确认或右侧放量突破后再行考虑。"
    assert scrub_chase_text(text) == "空仓者建议：暂不急于入场，等待缩量回踩强支撑位（如10.50元附近）确认。"


def test_whole_text_is_chase_falls_back_to_no_chase_note() -> None:
    assert scrub_chase_text("若放量突破30元，则果断跟进。") == "不追涨：只在回踩支撑确认时考虑。"


def test_text_without_breakout_is_untouched() -> None:
    assert scrub_chase_text("于MA5或MA10支撑位逢低吸纳，严格止损。") is None
    # 「突破」出现在风控语境（跌破/破位）以外但没有买入动作时不删
    assert scrub_chase_text("若跌破支撑则减仓；突破失败需警惕。") is None


def test_english_breakout_tail() -> None:
    assert scrub_chase_text("Wait for support confirmation or a breakout before acting.", "en") == (
        "Wait for support confirmation."
    )


def test_buy_point_above_price_or_breakout_is_chase() -> None:
    assert is_chase_buy_point("次优入场位：30.50元（放量突破近期压力位并获得均线确认时）", 29.45)
    assert is_chase_buy_point("理想入场位：30.50元", 29.45)          # 高于现价
    assert not is_chase_buy_point("理想入场位：42.27元（回踩 MA5 支撑且缩量企稳时）", 42.67)
    assert not is_chase_buy_point("理想入场位：70.60元（贴近MA5）", 70.55)  # 0.2% 容差内
    assert not is_chase_buy_point(None, 10.0)


def _result(**dashboard):
    return SimpleNamespace(dashboard=dashboard, market_snapshot={"price": 29.45}, current_price=None)


def test_strip_rewrites_dashboard_in_place() -> None:
    result = _result(
        battle_plan={
            "sniper_points": {
                "ideal_buy": "理想入场位：29.30元（回踩支撑）",
                "secondary_buy": "次优入场位：30.50元（放量突破近期压力位并获得均线确认时）",
                "stop_loss": "止损位：28.00元",
            },
            "position_strategy": {"entry_plan": "回踩29.30元建仓1-2成；若右侧放量突破30.50元再补仓1-2成。"},
        },
        core_conclusion={"position_advice": {"no_position": "等待支撑确认或放量突破后再行动。",
                                             "has_position": "以29元为风控线。"}},
    )
    removed = strip_chase_buy_points(result, current_price_of(result), "zh")

    assert set(removed) == {"sniper_points.secondary_buy", "position_strategy.entry_plan",
                            "position_advice.no_position"}
    points = extract_sniper_points(result)
    assert points["ideal_buy"] == 29.30
    assert points["secondary_buy"] is None          # 说明文字里没有数字，不会被当成价位
    assert points["stop_loss"] == 28.00
    assert result.dashboard["battle_plan"]["position_strategy"]["entry_plan"] == "回踩29.30元建仓1-2成。"
    assert result.dashboard["core_conclusion"]["position_advice"]["no_position"] == "等待支撑确认。"
    assert result.dashboard["core_conclusion"]["position_advice"]["has_position"] == "以29元为风控线。"
    assert result.dashboard["chase_guard"]["removed"] == removed


def test_strip_is_noop_without_dashboard() -> None:
    assert strip_chase_buy_points(SimpleNamespace(dashboard=None), 10.0) == []
