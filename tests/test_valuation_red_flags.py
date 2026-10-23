# -*- coding: utf-8 -*-
"""估值报告质量红旗（src/valuation_red_flags.py）。"""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from src import valuation_red_flags as vrf

PAYLOAD = {
    "stocks": {
        "000858.SZ": {
            "date": "2026-10-15", "confidence": "中低** —— 核心原因：数据冲突", "report": "2026/10/000858.SZ_20261015.md",
            "flags": ["🚩 2025年收入/利润大幅追溯下修", "🚩 收入与经营现金流背离"],
            "company_risks": ["会计差错更正尚无监管结论"],
        },
        "600900.SH": {"date": "2026-09-11", "confidence": "中", "flags": [],
                      "company_risks": ["来水不确定"]},
        "600000.SH": {"date": "2025-01-01", "confidence": "中", "flags": ["很旧的红旗"], "company_risks": []},
    }
}


@pytest.fixture(autouse=True)
def _local_file(tmp_path, monkeypatch):
    path = tmp_path / "red_flags.json"
    path.write_text(json.dumps(PAYLOAD, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv(vrf.ENV_FILE, str(path))
    monkeypatch.delenv(vrf.ENV_TOKEN, raising=False)
    vrf.reset_cache()
    yield
    vrf.reset_cache()


def test_lookup_accepts_bare_or_suffixed_codes_and_drops_stale_reports():
    today = date(2026, 10, 16)
    assert vrf.red_flags_for("000858", today)["flags"][0].startswith("🚩")
    assert vrf.red_flags_for("sz000858", today)["confidence"] == "中低"
    assert vrf.red_flags_for("600000.SH", today) is None        # 超过 MAX_AGE_DAYS
    assert vrf.red_flags_for("601318", today) is None           # 没有报告


def test_no_token_and_no_file_means_empty(monkeypatch):
    monkeypatch.delenv(vrf.ENV_FILE)
    vrf.reset_cache()
    assert vrf.load_red_flags() == {}
    assert vrf.red_flags_for("000858") is None


def test_prompt_section_lists_flags_and_risks():
    text = vrf.render_red_flags_prompt(vrf.red_flags_for("000858", date(2026, 10, 16)))
    assert "估值报告质量红旗（Macd-Qlib-Analyzer 估值报告，2026-10-15）" in text
    assert "- 红旗：🚩 收入与经营现金流背离" in text
    assert "- 公司层面风险：会计差错更正尚无监管结论" in text
    assert vrf.render_red_flags_prompt(None) == ""


def test_apply_flips_model_no_risk_tick_only_when_flags_exist():
    checklist = [
        "✅ 【程序】近期高管减持：近期无",
        "✅ 【模型】无重大利空，近期仅有回购进展公告",
        "✅ 【模型】估值与业绩预期匹配",
    ]
    result = SimpleNamespace(dashboard={"battle_plan": {"action_checklist": list(checklist)}})
    vrf.apply_red_flags(result, vrf.red_flags_for("000858", date(2026, 10, 16)))
    items = result.dashboard["battle_plan"]["action_checklist"]
    assert items[0] == checklist[0]
    assert items[1].startswith("⚠️ 【模型】无重大利空") and "列有 2 条重大红旗" in items[1]
    assert items[2] == checklist[2]
    assert result.dashboard["valuation_red_flags"]["date"] == "2026-10-15"

    # 只有一般性的公司层面风险时不改模型的勾
    result2 = SimpleNamespace(dashboard={"battle_plan": {"action_checklist": list(checklist)}})
    vrf.apply_red_flags(result2, vrf.red_flags_for("600900", date(2026, 10, 16)))
    assert result2.dashboard["battle_plan"]["action_checklist"] == checklist
    assert result2.dashboard["valuation_red_flags"]["company_risks"] == ["来水不确定"]


def test_prompt_tells_model_not_to_copy_report_risks_into_alerts():
    text = vrf.render_red_flags_prompt(vrf.red_flags_for("000858", date(2026, 10, 16)))
    assert "不要把它们写进 risk_alerts" in text and "不得给这些旧条目标上近期日期" in text


def test_alerts_copied_from_report_are_dropped_and_new_events_kept():
    """2026-10-20 实例：长江电力的风险警报抄了估值报告的来水风险，还标成「【2026-10-19】」。"""
    entry = {"date": "2026-09-11", "confidence": "中", "flags": [],
             "company_risks": ["来水波动风险：2026年前三季度六座电站总发电量同比-4.28%，乌东德偏枯9.29%；"
                               "来水是公司收入的核心波动变量，且不受公司控制。"]}
    alerts = [
        "【2026-10-19】来水波动风险：2026年前三季度六座电站总发电量同比-4.28%，乌东德偏枯9.29%，来水影响主营收入。",
        "【2026-10-18】公司公告控股股东拟减持不超过1%股份。",
    ]
    result = SimpleNamespace(dashboard={"intelligence": {"risk_alerts": list(alerts)}})
    changed = vrf.apply_red_flags(result, entry)
    assert result.dashboard["intelligence"]["risk_alerts"] == [alerts[1]]
    assert "intelligence.risk_alerts" in changed


def test_short_generic_overlap_is_not_treated_as_copy():
    entry = {"flags": ["🚩 存货高企"], "company_risks": []}
    alerts = ["【2026-10-19】行业库存高企，渠道去库存压力加大。", "非字符串条目"]
    assert vrf.drop_copied_alerts(alerts + [{"x": 1}], entry) == alerts + [{"x": 1}]


KINDED = {
    "kind_notes": {"tested": "检验说明文字"},
    "stocks": {
        "300750.SZ": {"date": "2026-10-21", "confidence": "中",
                      "flags": ["预计负债/销售返利：+23% YoY", "收入增速 vs 现金流增速背离"],
                      "flag_kinds": ["other", "tested"], "company_risks": []},
        "000858.SZ": {"date": "2026-10-15", "confidence": "中",
                      "flags": ["🚩 收入与经营现金流背离", "🚩 重大红旗：追溯下修"],
                      "flag_kinds": ["tested", "major"], "company_risks": []},
    },
}
CHECKLIST = ["✅ 【程序】近期高管减持：近期无", "✅ 【模型】无重大利空，近期仅有回购进展公告"]


@pytest.fixture
def kinded(tmp_path, monkeypatch):
    path = tmp_path / "kinded.json"
    path.write_text(json.dumps(KINDED, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv(vrf.ENV_FILE, str(path))
    vrf.reset_cache()


def test_finance_notes_keep_model_tick_but_add_warning_line(kinded):
    entry = vrf.red_flags_for("300750", date(2026, 10, 23))
    assert entry["flag_kinds"] == ["tested", "other"]  # 重大的排前面，其次已检验
    assert entry["kind_notes"] == {"tested": "检验说明文字"}
    model_warning = "⚠️ 【估值报告】模型自己写的"
    result = SimpleNamespace(dashboard={"battle_plan": {"action_checklist": CHECKLIST + [model_warning]}})
    vrf.apply_red_flags(result, entry)
    items = result.dashboard["battle_plan"]["action_checklist"]
    assert items[:2] == CHECKLIST
    assert items[2] == "⚠️ 【估值报告】财务提示 2 条（估值报告 2026-10-21，非重大利空，见「估值报告红旗」）"
    assert len(items) == 3
    vrf.apply_red_flags(result, entry)  # 重复调用不重复加
    assert result.dashboard["battle_plan"]["action_checklist"] == items


def test_major_flag_still_flips_and_counts_only_major(kinded):
    entry = vrf.red_flags_for("000858", date(2026, 10, 23))
    assert entry["flags"][0] == "🚩 重大红旗：追溯下修"
    result = SimpleNamespace(dashboard={"battle_plan": {"action_checklist": list(CHECKLIST)}})
    vrf.apply_red_flags(result, entry)
    items = result.dashboard["battle_plan"]["action_checklist"]
    assert items[1].startswith("⚠️ 【模型】无重大利空") and "列有 1 条重大红旗" in items[1]
    assert len(items) == 2


def test_prompt_labels_kinds_and_carries_tested_note(kinded):
    text = vrf.render_red_flags_prompt(vrf.red_flags_for("000858", date(2026, 10, 23)))
    assert "- 红旗：🚩 重大红旗：追溯下修" in text
    assert "- 财务提示：🚩 收入与经营现金流背离" in text
    assert "「财务提示」单独不构成重大利空" in text and "检验说明文字" in text
    plain = vrf.render_red_flags_prompt(vrf.red_flags_for("300750", date(2026, 10, 23)))
    assert "- 红旗：" not in plain
