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
    assert items[1].startswith("⚠️ 【模型】无重大利空") and "列有 2 条质量红旗" in items[1]
    assert items[2] == checklist[2]
    assert result.dashboard["valuation_red_flags"]["date"] == "2026-10-15"

    # 只有一般性的公司层面风险时不改模型的勾
    result2 = SimpleNamespace(dashboard={"battle_plan": {"action_checklist": list(checklist)}})
    vrf.apply_red_flags(result2, vrf.red_flags_for("600900", date(2026, 10, 16)))
    assert result2.dashboard["battle_plan"]["action_checklist"] == checklist
    assert result2.dashboard["valuation_red_flags"]["company_risks"] == ["来水不确定"]
