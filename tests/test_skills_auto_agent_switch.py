# -*- coding: utf-8 -*-
"""AGENT_SKILLS_AUTO_AGENT: configured skills may stay on the single-call analyzer path."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _run_pipeline(*, agent_skills, auto_agent):
    with patch('src.core.pipeline.get_config') as mock_config, \
         patch('src.core.pipeline.get_db'), \
         patch('src.core.pipeline.DataFetcherManager'), \
         patch('src.core.pipeline.GeminiAnalyzer'), \
         patch('src.core.pipeline.NotificationService'), \
         patch('src.core.pipeline.SearchService'):

        mock_cfg = MagicMock()
        mock_cfg.max_workers = 2
        mock_cfg.agent_mode = False
        mock_cfg.is_agent_available.return_value = False
        mock_cfg.agent_max_steps = 10
        mock_cfg.agent_skills = agent_skills
        mock_cfg.agent_skills_auto_agent = auto_agent
        mock_cfg.bocha_api_keys = []
        mock_cfg.tavily_api_keys = []
        mock_cfg.brave_api_keys = []
        mock_cfg.serpapi_keys = []
        mock_cfg.searxng_base_urls = []
        mock_cfg.searxng_public_instances_enabled = False
        mock_cfg.news_max_age_days = 7
        mock_cfg.enable_realtime_quote = True
        mock_cfg.enable_chip_distribution = True
        mock_cfg.realtime_source_priority = []
        mock_cfg.save_context_snapshot = False
        mock_config.return_value = mock_cfg

        from src.core.pipeline import StockAnalysisPipeline
        from src.enums import ReportType
        pipeline = StockAnalysisPipeline(config=mock_cfg)
        pipeline.fetcher_manager.get_realtime_quote.return_value = None
        pipeline.fetcher_manager.get_chip_distribution.return_value = None
        pipeline.search_service.is_available = False
        pipeline.db.get_analysis_context.return_value = None
        pipeline.analyzer.analyze.return_value = None
        pipeline._analyze_with_agent = MagicMock(return_value=None)

        pipeline.analyze_stock("600519", ReportType.SIMPLE, "q1")
        return pipeline


class TestSkillsAutoAgentSwitch(unittest.TestCase):
    SKILLS = ["growth_quality", "event_driven", "overheat_guard"]

    def test_configured_skills_switch_to_agent_by_default(self):
        pipeline = _run_pipeline(agent_skills=self.SKILLS, auto_agent=True)
        pipeline._analyze_with_agent.assert_called_once()
        pipeline.analyzer.analyze.assert_not_called()

    def test_switch_off_keeps_single_call_analyzer(self):
        pipeline = _run_pipeline(agent_skills=self.SKILLS, auto_agent=False)
        pipeline._analyze_with_agent.assert_not_called()
        pipeline.analyzer.analyze.assert_called_once()

    def test_env_parsing(self):
        from src.config import parse_env_bool
        self.assertTrue(parse_env_bool(None, default=True))
        self.assertFalse(parse_env_bool("false", default=True))


class TestConfiguredSkillsReachAnalyzerPrompt(unittest.TestCase):
    """The three chosen strategies must land in the single-call system prompt,
    and the implicit bull-trend baseline must not be layered on top."""

    def _prompt(self, skills):
        from src.analyzer import GeminiAnalyzer
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        config = SimpleNamespace(agent_skills=skills, agent_skill_dir=None)
        analyzer._get_runtime_config = lambda: config
        return analyzer._get_analysis_system_prompt("zh", "600519")

    def test_chosen_skills_in_prompt_without_bull_trend_baseline(self):
        prompt = self._prompt(["growth_quality", "event_driven", "overheat_guard"])
        self.assertIn("成长质量策略", prompt)
        self.assertIn("事件驱动", prompt)
        self.assertIn("过热风险（Overheat Guard）", prompt)
        self.assertIn("现价 / MA20 − 1 > 0.11", prompt)
        self.assertNotIn("默认技能基线（必须严格遵守）", prompt)
        self.assertNotIn("默认多头趋势", prompt)

    def test_empty_skills_keep_legacy_baseline(self):
        prompt = self._prompt([])
        self.assertIn("默认技能基线（必须严格遵守）", prompt)
        self.assertNotIn("过热风险（Overheat Guard）", prompt)


if __name__ == "__main__":
    unittest.main()
