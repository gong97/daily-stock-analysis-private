# -*- coding: utf-8 -*-
"""市场结构上下文的提示词：没有市场层面的题材证据时整段不写；缺失清单与 *_partial 标签不交给模型。

2026-09-30 报告里的「成分股排行数据缺失」就是模型照抄了这一段的缺失证据清单
（东财板块/概念接口在 CI 不可达，排行全空，个股位置只能是 edge/unknown）。
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.market_structure_prompt import format_market_structure_prompt_section  # noqa: E402
from src.schemas.market_structure import MARKET_STRUCTURE_SCHEMA_VERSION  # noqa: E402


def _context(**theme):
    return {
        "schema_version": MARKET_STRUCTURE_SCHEMA_VERSION,
        "status": "partial",
        "market_theme_context": {
            "active_themes": [], "leading_concepts": [], "leading_industries": [],
            "data_quality": {"missing_fields": ["concept_rankings", "industry_rankings"]},
            **theme,
        },
        "stock_market_position": {
            "primary_theme": {"name": "水力发电"},
            "theme_phase": "unknown",
            "stock_role": "edge",
            "risk_tags": [{"code": "theme_data_partial"}, {"code": "stock_theme_evidence_partial"},
                          {"code": "theme_overheated"}],
            "missing_fields": ["hotspot_constituents", "leader_stocks"],
        },
    }


def test_section_skipped_without_market_evidence():
    assert format_market_structure_prompt_section(_context()) == ""
    assert format_market_structure_prompt_section(_context(), report_language="en") == ""


def test_section_kept_but_without_diagnostics_when_rankings_exist():
    text = format_market_structure_prompt_section(
        _context(leading_industries=[{"name": "电力", "change_pct": 1.2}]))
    assert "## 市场结构上下文" in text and "领涨行业：电力(+1.20%)" in text
    assert "缺失证据" not in text and "partial" not in text.replace("- 状态：partial", "")
    assert "theme_overheated" in text  # 非数据状态的风险标签照旧
    assert "不要断言个股是题材龙头" in text
    en = format_market_structure_prompt_section(
        _context(leading_concepts=[{"name": "AI"}]), report_language="en")
    assert "Missing evidence" not in en and "Guardrail" in en
