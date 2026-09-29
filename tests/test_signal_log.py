# -*- coding: utf-8 -*-
"""Tests for the daily signal log in src/core/signal_log.py."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core import signal_log
from src.core.tiered_analysis import TieredAnalysisOutcome, TieredCandidate

RUN_AT = datetime(2026, 9, 28, 20, 52, 20, tzinfo=timezone.utc)


def _result(*, code, name, action, score, dashboard=None, snapshot=None, success=True, model="lite"):
    return SimpleNamespace(
        code=code,
        name=name,
        success=success,
        action=action,
        operation_advice=None,
        action_label=None,
        report_type=None,
        report_language="zh",
        sentiment_score=score,
        guardrail_reason=None,
        downgrade_reason=None,
        model_used=model,
        market_snapshot=snapshot,
        dashboard=dashboard if dashboard is not None else {},
    )


def _cut_dashboard(time_sensitivity="今日内"):
    return {
        "core_conclusion": {"time_sensitivity": time_sensitivity},
        "data_perspective": {
            "price_position": {"current_price": 999, "support_level": 340, "resistance_level": 370}
        },
        "battle_plan": {
            "sniper_points": {"stop_loss": "止损位：345元", "take_profit": "目标位：380元"}
        },
    }


class TestBuildSignalRecords(unittest.TestCase):
    def test_lite_fields_and_snapshot_price(self):
        lite = _result(
            code="603986.SH",
            name="兆易创新",
            action="reduce",
            score=30,
            dashboard=_cut_dashboard(),
            snapshot={"date": "2026-09-29", "price": "N/A", "close": "352.10"},
        )
        outcome = TieredAnalysisOutcome(lite_results=[lite])

        [record] = signal_log.build_signal_records(outcome, run_at=RUN_AT)

        self.assertEqual(record["run_at_utc"], "2026-09-28T20:52:20+00:00")
        self.assertEqual(record["snapshot_date"], "2026-09-29")
        self.assertEqual(record["price"], 352.10)
        self.assertEqual(record["price_source"], "close")
        self.assertIsNone(record["deep"])
        self.assertIsNone(record["deep_side"])
        self.assertEqual(
            record["lite"],
            {
                "model": "lite",
                "action": "reduce",
                "raw_action": "reduce",
                "bucket": "cut",
                "score": 30,
                "time_sensitivity": "今日内",
                "stop_loss": 345.0,
                "take_profit": 380.0,
                "ideal_buy": None,
                "secondary_buy": None,
                "support": 340.0,
                "resistance": 370.0,
            },
        )

    def test_llm_price_is_marked_as_such(self):
        lite = _result(code="603986.SH", name="兆易创新", action="reduce", score=30,
                       dashboard=_cut_dashboard())
        [record] = signal_log.build_signal_records(
            TieredAnalysisOutcome(lite_results=[lite]), run_at=RUN_AT
        )
        self.assertEqual(record["price"], 999.0)
        self.assertEqual(record["price_source"], "llm")

    def test_deep_review_attached_to_its_stock(self):
        lite = _result(code="603986.SH", name="兆易创新", action="reduce", score=30,
                       dashboard=_cut_dashboard())
        deep = _result(code="603986.SH", name="兆易创新", action="alert", score=40,
                       dashboard=_cut_dashboard("本周内"), model="deep")
        other = _result(code="600900.SH", name="长江电力", action="hold", score=50)
        outcome = TieredAnalysisOutcome(
            lite_results=[lite, other],
            candidates=[
                TieredCandidate(
                    code="603986.SH", name="兆易创新", side="cut",
                    lite_action="reduce", lite_score=30, deep_result=deep,
                )
            ],
        )

        records = {r["code"]: r for r in signal_log.build_signal_records(outcome, run_at=RUN_AT)}

        self.assertEqual(records["603986.SH"]["deep_side"], "cut")
        self.assertEqual(records["603986.SH"]["deep"]["model"], "deep")
        self.assertEqual(records["603986.SH"]["deep"]["action"], "alert")
        self.assertEqual(records["603986.SH"]["deep"]["time_sensitivity"], "本周内")
        self.assertIsNone(records["600900.SH"]["deep"])

    def test_failed_lite_results_are_skipped(self):
        failed = _result(code="600900.SH", name="长江电力", action="hold", score=50, success=False)
        records = signal_log.build_signal_records(
            TieredAnalysisOutcome(lite_results=[failed]), run_at=RUN_AT
        )
        self.assertEqual(records, [])


class TestSettingsSnapshot(unittest.TestCase):
    def test_resolves_active_skills_like_the_analyzer(self):
        config = SimpleNamespace(
            agent_skills=["growth_quality", "no_such_skill", "overheat_guard"],
            agent_skill_dir=None,
            llm_temperature=1.0,
        )
        with mock.patch.dict(os.environ, {"GITHUB_SHA": "abc123"}):
            settings = signal_log.settings_snapshot(config)

        self.assertEqual(
            settings,
            {"git_sha": "abc123", "skills": ["growth_quality", "overheat_guard"], "temperature": 1.0},
        )

    def test_empty_skills_record_the_default_fallback(self):
        config = SimpleNamespace(agent_skills=[], agent_skill_dir=None, llm_temperature=0.7)
        settings = signal_log.settings_snapshot(config)
        self.assertEqual(settings["skills"], ["bull_trend"])
        self.assertEqual(settings["temperature"], 0.7)

    def test_records_carry_settings(self):
        lite = _result(code="603986.SH", name="兆易创新", action="reduce", score=30)
        config = SimpleNamespace(agent_skills=["overheat_guard"], agent_skill_dir=None, llm_temperature=1.0)

        with tempfile.TemporaryDirectory() as tmp:
            path = signal_log.append_signal_log(
                TieredAnalysisOutcome(lite_results=[lite]),
                config=config, directory=Path(tmp), run_at=RUN_AT,
            )
            record = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(record["schema"], 2)
        self.assertEqual(record["settings"]["skills"], ["overheat_guard"])


class TestAppendSignalLog(unittest.TestCase):
    def test_appends_to_monthly_file_without_dedup(self):
        lite = _result(code="603986.SH", name="兆易创新", action="reduce", score=30,
                       dashboard=_cut_dashboard())
        outcome = TieredAnalysisOutcome(lite_results=[lite])

        with tempfile.TemporaryDirectory() as tmp:
            path = signal_log.append_signal_log(outcome, directory=Path(tmp), run_at=RUN_AT)
            signal_log.append_signal_log(outcome, directory=Path(tmp), run_at=RUN_AT)

            self.assertEqual(path.name, "2026-09.jsonl")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["name"], "兆易创新")

    def test_nothing_to_write_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = signal_log.append_signal_log(
                TieredAnalysisOutcome(lite_results=[]), directory=Path(tmp), run_at=RUN_AT
            )
            self.assertIsNone(path)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_enabled_only_by_env(self):
        with mock.patch.dict(os.environ, {"SIGNAL_LOG_ENABLED": "true"}):
            self.assertTrue(signal_log.is_enabled())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(signal_log.is_enabled())


if __name__ == "__main__":
    unittest.main()
