# -*- coding: utf-8 -*-
"""2026-09-30 两处修复：

1. 盘前不叠加实时行情。CI 定时任务实际在北京凌晨 2~5 点运行，此时「实时价」就是上一交易日收盘：
   叠加会给均线追加一根虚拟 K 线（昨收算两次）、把 today 改标成今天的估算值、让技术面被标成
   partial（置信度不得为「高」）。
2. 配置关闭筹码（ENABLE_CHIP_DISTRIBUTION=false）时，提示词、数据块列表、报告卡片都不再提筹码。
"""

import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote  # noqa: E402
from src.core import pipeline as pipeline_module  # noqa: E402
from src.core.pipeline import StockAnalysisPipeline, _is_premarket  # noqa: E402
from src.core import trading_calendar  # noqa: E402
from src.stock_analyzer import TrendAnalysisResult, TrendStatus  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _quote(price=15.72):
    return UnifiedRealtimeQuote(
        code="600519", name="贵州茅台", source=RealtimeSource.TENCENT,
        price=price, open_price=15.62, high=16.29, low=15.55, volume=13995600, change_pct=0.96,
    )


def _pipeline(**config_overrides):
    tmp = tempfile.mkdtemp()
    with patch.dict(os.environ, {"DATABASE_PATH": os.path.join(tmp, "t.db")}):
        from src.config import Config
        Config._instance = None
        config = Config._load_from_env()
    for key, value in config_overrides.items():
        setattr(config, key, value)
    return StockAnalysisPipeline(config=config)


@unittest.skipUnless(trading_calendar._XCALS_AVAILABLE, "exchange_calendars not installed")
class TestIsPremarket(unittest.TestCase):
    def test_cn_phases(self):
        self.assertTrue(_is_premarket("cn", datetime(2026, 9, 30, 3, 0, tzinfo=SHANGHAI)))
        self.assertFalse(_is_premarket("cn", datetime(2026, 9, 30, 10, 0, tzinfo=SHANGHAI)))
        self.assertFalse(_is_premarket("cn", datetime(2026, 9, 30, 16, 0, tzinfo=SHANGHAI)))

    def test_unknown_market_is_not_premarket(self):
        # 无法判断时保持原有行为（照常叠加）
        self.assertFalse(_is_premarket(None, datetime(2026, 9, 30, 3, 0)))


@unittest.skipUnless(trading_calendar._XCALS_AVAILABLE, "exchange_calendars not installed")
class TestNoRealtimeOverlayPremarket(unittest.TestCase):
    PREMARKET = datetime(2026, 9, 30, 3, 0, tzinfo=SHANGHAI)

    @patch("src.core.pipeline.is_market_open", return_value=True)
    @patch("src.core.pipeline.get_market_now")
    def test_augment_does_not_append_phantom_bar(self, mock_now, _open):
        mock_now.return_value = self.PREMARKET
        p = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        p.config = SimpleNamespace(enable_realtime_technical_indicators=True)
        df = pd.DataFrame([{"code": "600519", "date": date(2026, 9, 29), "open": 15, "high": 16,
                            "low": 15, "close": 15.72, "volume": 100, "amount": 0, "pct_chg": 0}])

        # 带后缀的代码也要识别成 A 股（否则 market=None，盘前判断失效）
        result = p._augment_historical_with_realtime(df, _quote(), "600519.SH")

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[-1]["date"], date(2026, 9, 29))

    @patch("src.core.pipeline.get_market_now")
    def test_enhance_context_keeps_latest_complete_bar(self, mock_now):
        mock_now.return_value = self.PREMARKET
        context = {
            "code": "600519",
            "date": "2026-09-29",
            "today": {"date": "2026-09-29", "close": 15.72, "ma5": 14.8},
            "yesterday": {"date": "2026-09-28", "close": 14.5, "volume": 1000000},
        }
        trend = TrendAnalysisResult(code="600519", trend_status=TrendStatus.BULL, ma5=15.5, ma10=15.2, ma20=14.9)

        enhanced = _pipeline()._enhance_context(context, _quote(), None, trend, "贵州茅台")

        self.assertEqual(enhanced["today"]["date"], "2026-09-29")
        self.assertNotIn("data_source", enhanced["today"])
        self.assertFalse(enhanced["today"].get("is_estimated"))


class TestChipDisabled(unittest.TestCase):
    def test_enhance_context_marks_chip_disabled(self):
        enhanced = _pipeline(enable_chip_distribution=False)._enhance_context(
            {"code": "600519"}, None, None, None, "贵州茅台"
        )
        self.assertTrue(enhanced.get("chip_disabled"))
        enabled = _pipeline(enable_chip_distribution=True)._enhance_context(
            {"code": "600519"}, None, None, None, "贵州茅台"
        )
        self.assertNotIn("chip_disabled", enabled)

    def test_normalizer_drops_chip_fields_when_disabled(self):
        from src.analyzer import normalize_chip_structure_availability

        result = SimpleNamespace(report_language="zh", dashboard={"data_perspective": {
            "chip_structure": {"profit_ratio": "数据缺失"}, "chip_unavailable_reason": "x", "keep": 1,
        }})
        normalize_chip_structure_availability(result, None, disabled=True)
        self.assertEqual(result.dashboard["data_perspective"], {"keep": 1})

    def test_prompt_omits_chip_section_when_disabled(self):
        from src.analyzer import GeminiAnalyzer

        with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
            analyzer = GeminiAnalyzer()
        cfg = SimpleNamespace(news_max_age_days=3, news_strategy_profile="short")
        base = {"code": "600519", "stock_name": "贵州茅台", "date": "2026-09-29", "today": {}}
        with patch("src.analyzer.get_config", return_value=cfg):
            disabled = analyzer._format_prompt({**base, "chip_disabled": True}, "贵州茅台")
            enabled = analyzer._format_prompt(base, "贵州茅台")
        self.assertNotIn("筹码分布数据", disabled)
        self.assertIn("筹码分布数据", enabled)

    def test_context_pack_omits_chip_block_when_disabled(self):
        from src.services.analysis_context_builder import AnalysisContextBuilder, PipelineAnalysisArtifacts
        build_analysis_context_pack = AnalysisContextBuilder.build

        def _artifacts(metadata):
            return PipelineAnalysisArtifacts(
                code="600519", stock_name="贵州茅台", market="cn", phase=None, base_context={},
                enhanced_context={}, realtime_quote=None, trend_result=None, chip_data=None,
                fundamental_context=None, news_context=None, news_result_count=None,
                metadata=metadata, portfolio_context=None,
            )

        disabled = build_analysis_context_pack(_artifacts({"chip_disabled": True}))
        enabled = build_analysis_context_pack(_artifacts({}))
        self.assertNotIn("chip", disabled.blocks)
        self.assertIn("chip", enabled.blocks)
        # 质量评分口径不变：缺这个块按 missing 计，与未关闭且没取到时相同
        self.assertEqual(disabled.data_quality.overall_score, enabled.data_quality.overall_score)


if __name__ == "__main__":
    unittest.main()
