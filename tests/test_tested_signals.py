# -*- coding: utf-8 -*-
"""看多理由的历史检验标注（src/tested_signals.py）：例句取自 2026-10-14 日报。"""

from types import SimpleNamespace

from src.tested_signals import annotate, annotate_tested_signals, tested_notes


def test_known_invalid_reasons_are_annotated() -> None:
    assert tested_notes("均线多头排列（MA5>MA10>MA20）") == ["均线多头在 10 日上没有优势"]
    assert tested_notes("RSI超卖存在技术反弹要求") == ["跌得多的股票之后不反弹"]
    assert tested_notes("缩量回调洗盘特征明显，高管曾有增持动作") == [
        "缩量回踩的缩量条件历史上方向相反", "股东 / 高管增持之后不跑赢",
    ]
    assert tested_notes("近5日主力资金整体呈现净流入状态") == ["主力净流入之后 10 日反而多数跑输"]
    assert tested_notes("中报归母净利润同比大增68.17%") == ["已公布的业绩在公告窗口内就被消化，之后没有漂移"]


def test_valuation_and_neutral_facts_are_not_annotated() -> None:
    assert tested_notes("经营现金流稳健，估值处于合理区间") == []
    assert tested_notes("公司发布关于回购公司股份进展的公告") == []
    assert annotate("估值合理且中报业绩增速稳健") == "估值合理且中报业绩增速稳健"


def test_annotation_is_idempotent() -> None:
    once = annotate("RSI超卖技术反弹需求")
    assert once.endswith("〔历史检验：跌得多的股票之后不反弹〕")
    assert annotate(once) == once


def test_annotate_dashboard_fields_only_model_checks_with_tick() -> None:
    result = SimpleNamespace(dashboard={
        "signal_attribution": {"strongest_bullish_signal": "均线多头排列", "strongest_bearish_signal": "缩量"},
        "intelligence": {"positive_catalysts": ["利好1：近5日主力资金净流入", "利好2：回购进展"]},
        "battle_plan": {"action_checklist": [
            "✅ 【程序】高位过热（乖离过大/放量急拉）：未触发",
            "✅ 【模型】量价关系健康，缩量回调表明抛压较轻",
            "⚠️ 【模型】缩量回调但量能不足",
        ]},
    })
    changed = annotate_tested_signals(result, "zh")

    d = result.dashboard
    assert set(changed) == {"signal_attribution.strongest_bullish_signal", "intelligence.positive_catalysts",
                            "battle_plan.action_checklist"}
    assert "〔历史检验" in d["signal_attribution"]["strongest_bullish_signal"]
    assert d["signal_attribution"]["strongest_bearish_signal"] == "缩量"          # 看空理由不标
    assert "〔历史检验" in d["intelligence"]["positive_catalysts"][0]
    assert d["intelligence"]["positive_catalysts"][1] == "利好2：回购进展"
    checks = d["battle_plan"]["action_checklist"]
    assert "〔历史检验" not in checks[0]                                         # 【程序】项不标
    assert "〔历史检验" in checks[1]
    assert "〔历史检验" not in checks[2]                                         # ⚠️ 项已在提示风险


def test_non_chinese_reports_are_left_alone() -> None:
    result = SimpleNamespace(dashboard={"signal_attribution": {"strongest_bullish_signal": "均线多头排列"}})
    assert annotate_tested_signals(result, "en") == []
