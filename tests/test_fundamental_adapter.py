# -*- coding: utf-8 -*-
"""
Tests for fundamental adapter helpers.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.fundamental_adapter import (
    AkshareFundamentalAdapter,
    _build_dividend_payload,
    _extract_latest_row,
    _parse_dividend_plan_to_per_share,
    _parse_financial_abstract_wide,
)


class TestFundamentalAdapter(unittest.TestCase):
    def test_parse_dividend_plan_to_per_share_supports_cn_patterns(self) -> None:
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("10派3元(含税)"), 0.3, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每10股派发2.5元"), 0.25, places=6)
        self.assertAlmostEqual(_parse_dividend_plan_to_per_share("每股派0.8元"), 0.8, places=6)
        self.assertIsNone(_parse_dividend_plan_to_per_share("仅送股，不现金分红"))

    def test_extract_latest_row_returns_none_when_code_mismatch(self) -> None:
        df = pd.DataFrame(
            {
                "股票代码": ["600000", "000001"],
                "值": [1, 2],
            }
        )
        row = _extract_latest_row(df, "600519")
        self.assertIsNone(row)

    def test_extract_latest_row_fallback_when_no_code_column(self) -> None:
        df = pd.DataFrame({"值": [1, 2]})
        row = _extract_latest_row(df, "600519")
        self.assertIsNotNone(row)
        self.assertEqual(row["值"], 1)

    def test_dragon_tiger_no_match_with_code_column_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        df = pd.DataFrame(
            {
                "股票代码": ["600000"],
                "日期": ["2026-01-01"],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["is_on_list"])
        self.assertEqual(result["recent_count"], 0)

    def test_dragon_tiger_match_is_ok(self) -> None:
        adapter = AkshareFundamentalAdapter()
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "日期": [today],
            }
        )
        with patch.object(adapter, "_call_df_candidates", return_value=(df, "stock_lhb_stock_statistic_em", [])):
            result = adapter.get_dragon_tiger_flag("600519")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["is_on_list"])
        self.assertGreaterEqual(result["recent_count"], 1)

    def test_fundamental_bundle_includes_financial_report_and_dividend_payload(self) -> None:
        adapter = AkshareFundamentalAdapter()
        now = datetime.now()
        within_ttm = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        future_day = (now + timedelta(days=10)).strftime("%Y-%m-%d")
        old_day = (now - timedelta(days=500)).strftime("%Y-%m-%d")
        fin_df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "报告期": [within_ttm],
                "营业总收入": [1000.0],
                "归母净利润": [300.0],
                "经营活动产生的现金流量净额": [500.0],
                "净资产收益率": [18.2],
                "营业收入同比": [12.0],
                "净利润同比": [9.5],
            }
        )
        forecast_df = pd.DataFrame({"股票代码": ["600519"], "预告": ["预增"]})
        quick_df = pd.DataFrame({"股票代码": ["600519"], "快报": ["快报摘要"]})
        dividend_df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519", "600519", "600519"],
                "除息日": [within_ttm, within_ttm, future_day, old_day],
                "分配方案": ["10派3元(含税)", "10派3元(含税)", "10派5元", "10派1元"],
            }
        )

        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=[
                (fin_df, "stock_financial_abstract", []),
                (forecast_df, "stock_yjyg_em", []),
                (quick_df, "stock_yjkb_em", []),
                (dividend_df, "stock_fhps_detail_em", []),
                (None, None, []),
                (None, None, []),
            ],
        ):
            result = adapter.get_fundamental_bundle("600519")

        financial_report = result["earnings"].get("financial_report", {})
        self.assertEqual(financial_report.get("report_date"), within_ttm)
        self.assertEqual(financial_report.get("revenue"), 1000.0)
        self.assertEqual(financial_report.get("net_profit_parent"), 300.0)
        self.assertEqual(financial_report.get("operating_cash_flow"), 500.0)
        self.assertEqual(financial_report.get("roe"), 18.2)

        dividend_payload = result["earnings"].get("dividend", {})
        events = dividend_payload.get("events", [])
        self.assertEqual(len(events), 2)  # duplicate + future day filtered
        self.assertEqual(dividend_payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(dividend_payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)

    @staticmethod
    def _sina_abstract_wide(latest_empty: bool = False) -> pd.DataFrame:
        """按 ak.stock_financial_abstract 的真实形状构造：行是指标，列是报告期（新→旧），
        同名指标在不同「选项」分组里重复出现。数值取自 2026-09-29 兆易创新的真实返回。"""
        rows = [
            ("常用指标", "归母净利润", 6.856786e9, 1.461248e9, 5.754756e8),
            ("常用指标", "营业总收入", 1.156576e10, 4.188076e9, 4.150309e9),
            ("常用指标", "经营现金流量净额", 6.048341e9, 1.783057e9, 9.578209e8),
            ("常用指标", "净资产收益率(ROE)", 23.13, 6.12, 3.41),
            ("常用指标", "毛利率", 63.13045, 57.07672, 37.21007),
            ("成长能力", "归母净利润", 6.856786e9, 1.461248e9, 5.754756e8),
            ("成长能力", "营业总收入增长率", 178.6723, 119.3787, 14.99766),
            ("成长能力", "归属母公司净利润增长率", 1091.499, 522.7881, 11.31056),
        ]
        df = pd.DataFrame(rows, columns=["选项", "指标", "20260630", "20260331", "20250630"])
        if latest_empty:
            df.insert(2, "20260930", float("nan"))
        return df

    def test_parse_sina_abstract_wide_table(self) -> None:
        parsed = _parse_financial_abstract_wide(self._sina_abstract_wide())
        self.assertEqual(parsed["report_date"], "2026-06-30")
        self.assertEqual(parsed["revenue"], 1.156576e10)
        self.assertEqual(parsed["net_profit_parent"], 6.856786e9)
        self.assertEqual(parsed["operating_cash_flow"], 6.048341e9)
        self.assertEqual(parsed["roe"], 23.13)
        self.assertAlmostEqual(parsed["gross_margin"], 63.13045)
        self.assertAlmostEqual(parsed["revenue_yoy"], 178.6723)
        self.assertAlmostEqual(parsed["net_profit_yoy"], 1091.499)

    def test_parse_sina_abstract_skips_empty_latest_period(self) -> None:
        parsed = _parse_financial_abstract_wide(self._sina_abstract_wide(latest_empty=True))
        self.assertEqual(parsed["report_date"], "2026-06-30")
        self.assertEqual(parsed["revenue"], 1.156576e10)

    def test_parse_sina_abstract_rejects_long_format(self) -> None:
        long_df = pd.DataFrame({"股票代码": ["600519"], "营业总收入": [1000.0]})
        self.assertIsNone(_parse_financial_abstract_wide(long_df))

    def test_fundamental_bundle_parses_real_sina_shape(self) -> None:
        """旧测试用逐行格式冒充 stock_financial_abstract，真实接口却返回宽表，导致线上全是 N/A。"""
        adapter = AkshareFundamentalAdapter()
        with patch.object(
            adapter,
            "_call_df_candidates",
            side_effect=[(self._sina_abstract_wide(), "stock_financial_abstract", [])]
            + [(None, None, [])] * 10,
        ):
            result = adapter.get_fundamental_bundle("603986")

        report = result["earnings"]["financial_report"]
        self.assertEqual(report["report_date"], "2026-06-30")
        self.assertEqual(report["revenue"], 1.156576e10)
        self.assertEqual(report["roe"], 23.13)
        self.assertAlmostEqual(result["growth"]["revenue_yoy"], 178.6723)
        self.assertAlmostEqual(result["growth"]["net_profit_yoy"], 1091.499)
        self.assertIn("growth:stock_financial_abstract", result["source_chain"])

    def test_build_dividend_payload_returns_empty_when_code_not_matched(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "除息日": [now],
                "分配方案": ["10派3元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_skips_after_tax_plan(self) -> None:
        now = datetime.now().strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "除息日": [now],
                "分配方案": ["10派3元(税后)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload, {})

    def test_build_dividend_payload_ttm_window_boundary(self) -> None:
        now = datetime.now()
        day_365 = (now - timedelta(days=365)).strftime("%Y-%m-%d")
        day_366 = (now - timedelta(days=366)).strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "股票代码": ["600519", "600519"],
                "除息日": [day_365, day_366],
                "分配方案": ["10派3元(含税)", "10派5元(含税)"],
            }
        )

        payload = _build_dividend_payload(df, stock_code="600519")
        self.assertEqual(payload.get("ttm_event_count"), 1)
        self.assertAlmostEqual(payload.get("ttm_cash_dividend_per_share"), 0.3, places=6)


if __name__ == "__main__":
    unittest.main()
