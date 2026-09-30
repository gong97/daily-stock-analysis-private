# -*- coding: utf-8 -*-
"""大盘估值背景（乐咕乐股）：分位、时效、单指标失败隔离、提示词接入。"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import market_valuation_lg as mv  # noqa: E402


def _monthly(values, end="2026-09-30"):
    dates = pd.date_range(end=end, periods=len(values), freq="MS")
    return pd.DataFrame({"日期": dates, "v": values})


class TestSummarizeSeries(unittest.TestCase):
    def test_percentile_uses_last_ten_years_only(self):
        # 15 年月度数据：前 5 年极高（100），后 10 年 1..120 递增
        values = [100.0] * 60 + [float(i) for i in range(1, 121)]
        df = _monthly(values)
        s = mv.summarize_series(df["日期"], df["v"])
        self.assertEqual(s["value"], 120.0)
        self.assertEqual(s["percentile"], 100.0)  # 窗口外那 5 年的 100 不参与
        self.assertLessEqual(s["samples"], 121)

    def test_too_few_samples_returns_none(self):
        df = _monthly([1.0, 2.0, 3.0])
        self.assertIsNone(mv.summarize_series(df["日期"], df["v"]))


class TestFetchMarketValuation(unittest.TestCase):
    def _spec(self, key, fetch, value_col="v", higher=True, unit="", transform=None):
        return mv._Indicator(key, key, fetch, "日期", value_col, higher, unit, transform)

    def test_each_indicator_isolated_and_stale_dropped(self):
        def _daily(values, end):
            return pd.DataFrame({"日期": pd.date_range(end=end, periods=len(values), freq="D"), "v": values})

        fresh = _daily([float(i) for i in range(1, 40)], end="2026-09-30")
        stale = _daily([float(i) for i in range(1, 40)], end="2026-07-01")
        specs = [
            self._spec("ok", lambda: fresh),
            self._spec("stale", lambda: stale),
            self._spec("boom", lambda: (_ for _ in ()).throw(ConnectionError("blocked"))),
            self._spec("empty", lambda: pd.DataFrame()),
            self._spec("pct", lambda: fresh, transform=lambda df: df["v"] / 100, unit="%"),
        ]
        with patch.object(mv, "_indicators", return_value=specs):
            ctx = mv.fetch_market_valuation(today=pd.Timestamp("2026-09-30"))

        self.assertEqual([i["key"] for i in ctx["indicators"]], ["ok", "pct"])
        self.assertAlmostEqual(ctx["indicators"][1]["value"], 0.39)
        errors = " ".join(ctx["errors"])
        self.assertIn("stale:stale(2026-07-01)", errors)
        self.assertIn("boom:ConnectionError", errors)
        self.assertIn("empty:empty", errors)


class TestRender(unittest.TestCase):
    def test_render_directions_and_units(self):
        ctx = {"lookback_years": 10, "indicators": [
            {"key": "sh_pe", "label": "上证平均市盈率", "unit": "", "higher_is_expensive": True,
             "value": 16.94, "date": "2026-09-30", "percentile": 82.4, "samples": 120},
            {"key": "equity_bond_spread", "label": "股债利差（沪深300）", "unit": "%",
             "higher_is_expensive": False, "value": 6.33, "date": "2026-09-30", "percentile": 76.0, "samples": 500},
        ]}
        text = mv.render_market_valuation_block(ctx)
        self.assertIn("- 上证平均市盈率：16.94（2026-09-30），处于近10年 82% 分位（越高越贵）", text)
        self.assertIn("- 股债利差（沪深300）：6.33%（2026-09-30），处于近10年 76% 分位（越高越便宜）", text)
        self.assertIn("没有择时能力", text)

    def test_render_empty(self):
        self.assertEqual(mv.render_market_valuation_block({"indicators": []}), "")
        self.assertEqual(mv.render_market_valuation_block(None), "")


class TestMarketReviewPrompt(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self._tmp.name, "mv.db")
        from src.config import Config, get_config
        from src.storage import DatabaseManager
        Config._instance = None
        DatabaseManager.reset_instance()
        self.config = get_config()

    def tearDown(self):
        from src.storage import DatabaseManager
        DatabaseManager.reset_instance()
        self._tmp.cleanup()

    def test_cn_prompt_includes_valuation_block(self):
        from src.market_analyzer import MarketAnalyzer, MarketOverview

        analyzer = MarketAnalyzer(config=self.config, region="cn")
        overview = MarketOverview(date="2026-09-30", valuation_context={"lookback_years": 10, "indicators": [
            {"key": "sh_pe", "label": "上证平均市盈率", "unit": "", "higher_is_expensive": True,
             "value": 16.94, "date": "2026-09-30", "percentile": 82.0, "samples": 120},
        ]})
        prompt = analyzer._build_review_prompt(overview, [])
        self.assertIn("## 市场估值背景（乐咕乐股，近10年历史分位）", prompt)

        empty_prompt = analyzer._build_review_prompt(MarketOverview(date="2026-09-30"), [])
        self.assertNotIn("市场估值背景", empty_prompt)

    def test_overview_fetches_valuation_only_for_cn(self):
        from src.market_analyzer import MarketAnalyzer

        for region, expected in (("cn", 1), ("us", 0)):
            analyzer = MarketAnalyzer(config=self.config, region=region)
            with patch("src.market_valuation_lg.fetch_market_valuation",
                       return_value={"indicators": [], "errors": []}) as fetch, \
                    patch.object(analyzer, "_get_main_indices", return_value=[]), \
                    patch.object(analyzer, "_get_market_statistics"), \
                    patch.object(analyzer, "_get_sector_rankings"), \
                    patch.object(analyzer, "_get_concept_rankings"):
                analyzer.get_market_overview()
            self.assertEqual(fetch.call_count, expected, region)


if __name__ == "__main__":
    unittest.main()
