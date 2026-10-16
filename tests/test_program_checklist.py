# -*- coding: utf-8 -*-
"""检查清单的【程序】项：已检验信号由程序判定，插在【模型】项前面；只提示、不调分。"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.program_checklist import apply_program_checklist, build_program_checklist  # noqa: E402


def _fund(status="ok", recent=None):
    return {"holder_changes": {"status": status, "data": {"management_recent_selling": recent or []}}}


def test_overheat_and_turnover_marks():
    items = build_program_checklist({"flag_a": True, "flag_b": False, "flag_c": True}, _fund())
    assert items[0] == "⚠️ 【程序】高位过热（乖离过大/放量急拉）：触发（A）"
    assert items[1] == "⚠️ 【程序】高位高换手：触发"
    items = build_program_checklist({"flag_a": False, "flag_b": False, "flag_c": False}, _fund())
    assert items[0].startswith("✅") and items[1].startswith("✅")
    # B 算不出来（量能常态不足）且 A 未触发：不能说「未触发」
    items = build_program_checklist({"flag_a": False, "flag_b": None, "flag_c": None}, _fund())
    assert items[0].startswith("❓") and items[1].startswith("❓")
    assert all(i.startswith("❓") for i in build_program_checklist(None, None)[:2])


def test_insider_selling_marks():
    recent = [{"date": "2026-09-25", "who": "张三", "change": "减持2.00万"}, {"date": "2026-09-24"}]
    assert build_program_checklist(None, _fund(recent=recent))[2] == \
        "⚠️ 【程序】近期高管减持：2026-09-25 张三 减持2.00万 等 2 笔"
    assert build_program_checklist(None, _fund())[2] == "✅ 【程序】近期高管减持：近期无"
    assert build_program_checklist(None, _fund(status="failed"))[2].startswith("❓")


def test_apply_prepends_program_items_and_drops_model_copies():
    result = SimpleNamespace(dashboard={"battle_plan": {"action_checklist": [
        "✅ 【模型】无重大利空",
        "✅ 【程序】高位过热：未触发",  # 模型自己写的程序项，要被替换
        "⚠️ 【模型】入场位置与风险回报是否合理",
    ]}})
    apply_program_checklist(result, overheat_flags={"flag_a": False, "flag_b": True, "flag_c": False},
                            fundamental_context=_fund())
    checklist = result.dashboard["battle_plan"]["action_checklist"]
    assert [c.split(" ")[0] for c in checklist[:4]] == ["⚠️", "✅", "✅", "❓"]  # 第 4 项近 5 日涨幅：没传 → 数据不足
    assert checklist[4:] == ["✅ 【模型】无重大利空", "⚠️ 【模型】入场位置与风险回报是否合理"]
    assert sum("【程序】" in c for c in checklist) == 4


def test_apply_creates_battle_plan_and_english_labels():
    result = SimpleNamespace(dashboard={})
    items = apply_program_checklist(result, overheat_flags=None, fundamental_context=None, language="en")
    assert result.dashboard["battle_plan"]["action_checklist"] == items
    assert items[2] == "❓ [Rule]Recent executive selling: data unavailable"


def test_system_prompt_no_longer_asks_for_self_check():
    from unittest.mock import patch

    from src.analyzer import GeminiAnalyzer

    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        analyzer = GeminiAnalyzer()
    with patch.object(GeminiAnalyzer, "_get_skill_prompt_sections", return_value=("", "", False)):
        analyzer._config_override = SimpleNamespace(enable_chip_distribution=True)
        prompt = analyzer._get_analysis_system_prompt("zh", stock_code="600519")
    # 清单里不再有这一项（「强烈买入」评分标准里那句「仓位与止损计划明确」是另一回事，保留）
    assert "检查项5：仓位与止损计划明确" not in prompt and "【模型】仓位与止损" not in prompt
    assert prompt.count("【模型】") >= 5 and "标【程序】" in prompt


def test_program_flags_are_stored_structured_for_the_summary_table() -> None:
    """总览表读 dashboard["program_flags"]，不解析清单文字。"""
    from types import SimpleNamespace

    from src.program_checklist import apply_program_checklist, program_flag_states

    ctx = {"holder_changes": {"status": "ok", "data": {"management_recent_selling": [{"date": "2026-10-01"}]}}}
    assert program_flag_states({"flag_a": False, "flag_b": True, "flag_c": False}, ctx) == {
        "overheat": True, "turnover": False, "insider": True, "runup": None,
    }
    assert program_flag_states(None, None) == {"overheat": None, "turnover": None, "insider": None, "runup": None}

    result = SimpleNamespace(dashboard={})
    apply_program_checklist(result, overheat_flags={"flag_a": False, "flag_b": False, "flag_c": True},
                            fundamental_context={"holder_changes": {"status": "ok", "data": {}}})
    assert result.dashboard["program_flags"] == {"overheat": False, "turnover": True, "insider": False, "runup": None}



def test_recent_runup_is_the_fourth_program_item() -> None:
    from src.program_checklist import build_program_checklist

    hit = {"flag": True, "top": True, "excess": 0.14, "stock_ret": 0.16, "index_ret": 0.02}
    items = build_program_checklist(None, None, "zh", recent_runup=hit)
    assert items[3] == "⚠️ 【程序】近 5 日涨幅居前（不追高）：5 日 +16.0%，扣除中证 1000 后 +14.0%，全市场前 5%"
    miss = {**hit, "flag": False, "top": False}
    assert build_program_checklist(None, None, "zh", recent_runup=miss)[3].startswith("✅")
