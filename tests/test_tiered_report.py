# -*- coding: utf-8 -*-
"""Tests for the Tier 2 candidate card rendering in src/core/tiered_report.py."""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.tiered_analysis import TieredCandidate
from src.core.tiered_report import render_candidate_block


def _make_deep_result(dashboard=None, **overrides):
    fields = dict(
        sentiment_score=66,
        action="hold",
        operation_advice=None,
        action_label=None,
        report_type=None,
        report_language="zh",
        analysis_summary="短期趋势转弱，等待确认。",
        buy_reason="均线走平，量能不足以支撑突破。",
        key_points="筹码集中度提升。",
        risk_warning="短线已有较大乖离，追高风险高。",
        market_snapshot=None,
        dashboard=dashboard if dashboard is not None else {},
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _full_dashboard():
    return {
        "core_conclusion": {
            "one_sentence": "短线乖离偏大，压力位上方缺量能确认。",
        },
        "data_perspective": {
            "price_position": {
                "current_price": 128.40,
                "ma20": 123.6,
                "support_level": 121.5,
                "resistance_level": 132.5,
            },
            "volume_analysis": {
                "volume_ratio": 1.1,
            },
        },
        "battle_plan": {
            "sniper_points": {
                "ideal_buy": "理想买入点：121元（在MA5附近）",
                "secondary_buy": "次优买入点：124元",
                "stop_loss": "止损位：117.80元（跌破MA20）",
                "take_profit": "目标位：141元（前高）",
            },
            "position_strategy": {
                "suggested_position": "5-8成",
            },
        },
        "phase_decision": {
            "action_window": "盘中跟踪",
            "next_check_time": "下一交易日收盘",
            "watch_conditions": [
                "收盘 > ¥132.5",
                "量比 > 1.3",
                "主力资金流转正",
            ],
        },
    }


class TestRenderCandidateBlockFullData(unittest.TestCase):
    """字段齐全时，卡片应包含执行计划、现价、距支撑/距压力百分比。"""

    def test_full_dashboard_renders_all_sections(self):
        candidate = TieredCandidate(
            code="603986.SH",
            name="兆易创新",
            side="add",
            lite_action="add",
            lite_score=78,
            deep_result=_make_deep_result(dashboard=_full_dashboard()),
        )

        block = render_candidate_block(candidate)

        self.assertIn("603986.SH", block)
        self.assertIn("¥128.40", block)
        self.assertIn("🎯 执行计划", block)
        self.assertIn("¥121.00", block)  # 理想买点解析自带前缀的字符串
        self.assertIn("¥117.80", block)  # 止损
        self.assertIn("📊 关键位置", block)
        self.assertIn("距支撑", block)
        self.assertIn("距压力", block)
        self.assertIn("✅ 转为 BUY 的条件", block)
        self.assertIn("1. 收盘 > ¥132.5", block)
        self.assertIn("📅 下一观察点", block)
        self.assertIn("🟡 为什么现在不追", block)
        # 佐证文本仍然保留
        self.assertIn("**风险**：短线已有较大乖离", block)

    def test_action_changed_marks_divergence_in_headline(self):
        candidate = TieredCandidate(
            code="603986.SH",
            name="兆易创新",
            side="add",
            lite_action="add",
            lite_score=78,
            deep_result=_make_deep_result(dashboard=_full_dashboard()),
        )
        block = render_candidate_block(candidate)
        self.assertIn("⚠️ Tier1/Tier2 有分歧", block)


class TestRenderCandidateBlockEmptyDashboard(unittest.TestCase):
    """dashboard 为空字典时，不应抛异常，且各小节整块隐藏。"""

    def test_empty_dashboard_does_not_raise(self):
        candidate = TieredCandidate(
            code="000977.SZ",
            name="浪潮信息",
            side="cut",
            lite_action="reduce",
            lite_score=40,
            deep_result=_make_deep_result(dashboard={}),
        )

        block = render_candidate_block(candidate)

        self.assertIn("000977.SZ", block)
        self.assertIn("| 初筛 | ", block)
        self.assertIn("| 深度复核 | ", block)
        self.assertNotIn("🎯 执行计划", block)
        self.assertNotIn("📊 关键位置", block)
        self.assertNotIn("✅ 转为 BUY 的条件", block)
        self.assertNotIn("📅 下一观察点", block)
        # 空 dashboard 时没有一句话结论，但 risk_warning 仍在，因此
        # 「为什么现在不追」块应当仍然出现（只靠 risk_warning 撑起）。
        self.assertIn("🟡 为什么现在不追", block)

    def test_no_nan_or_blank_placeholder_rows(self):
        candidate = TieredCandidate(
            code="000977.SZ",
            name="浪潮信息",
            side="cut",
            lite_action="reduce",
            lite_score=40,
            deep_result=_make_deep_result(
                dashboard={},
                risk_warning="",
                buy_reason="",
                key_points="",
                analysis_summary="",
            ),
        )
        block = render_candidate_block(candidate)
        self.assertNotIn("N/A", block)
        self.assertNotIn("None", block)


class TestRenderCandidateBlockNoDeepResult(unittest.TestCase):
    """deep_result 为 None 时应回退到初筛结论。"""

    def test_missing_deep_result_falls_back(self):
        candidate = TieredCandidate(
            code="600985.SH",
            name="淮北矿业",
            side="add",
            lite_action="add",
            lite_score=55,
            deep_result=None,
        )

        block = render_candidate_block(candidate)

        self.assertIn("600985.SH", block)
        self.assertIn("深度复核未返回结果，请以初筛结论为准。", block)
        self.assertNotIn("🎯 执行计划", block)


if __name__ == "__main__":
    unittest.main()
