# -*- coding: utf-8 -*-
"""交易日检查的两处修复（2026-09-30）：

1. 名单是「600900.SH」格式，get_market_for_stock 只认裸代码；不规范化时返回 None，被当成
   未知市场放行，节假日照常全量运行（2026-09-25 中秋即如此）。
2. 定时任务名义北京 23:00，实际常拖到次日凌晨 2~5 点；按当地「当天」判断会把周五的运行
   当成周六跳过。session_cutoff_hour=9：9 点前开跑的算前一天。
"""

import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core import trading_calendar  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _frozen_at(year, month, day, hour):
    fixed = datetime(year, month, day, hour, 0, tzinfo=SHANGHAI)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    return patch.object(trading_calendar, "datetime", _Frozen)


@unittest.skipUnless(trading_calendar._XCALS_AVAILABLE, "exchange_calendars not installed")
class TestSessionCutoff(unittest.TestCase):
    def _cn_open(self, *when, cutoff=9):
        with _frozen_at(*when):
            return "cn" in trading_calendar.get_open_markets_today(session_cutoff_hour=cutoff)

    def test_friday_run_that_starts_saturday_morning_counts_as_friday(self):
        # 周一 9-28 的定时任务在周二凌晨开跑 → 按周一判断，开市
        self.assertTrue(self._cn_open(2026, 9, 29, 3))
        # 周五 9-25 的任务在周六凌晨开跑 → 按周五判断；9-25 是中秋休市
        self.assertFalse(self._cn_open(2026, 9, 26, 3))

    def test_national_day_boundaries(self):
        # 9-30 的任务在 10-1 凌晨开跑 → 按 9-30 判断，开市（节前最后一个交易日不能被跳过）
        self.assertTrue(self._cn_open(2026, 10, 1, 3))
        # 10-7 的任务在 10-8 凌晨开跑 → 按 10-7 判断，休市
        self.assertFalse(self._cn_open(2026, 10, 8, 3))
        # 10-8 的任务在 10-9 凌晨开跑 → 按 10-8 判断，开市
        self.assertTrue(self._cn_open(2026, 10, 9, 3))

    def test_daytime_runs_use_same_day(self):
        self.assertFalse(self._cn_open(2026, 10, 1, 10))
        self.assertTrue(self._cn_open(2026, 9, 30, 10))

    def test_no_cutoff_keeps_calendar_day_for_realtime_callers(self):
        # 盘中预警、bot 不传 cutoff：10-1 凌晨就是 10-1，休市
        self.assertFalse(self._cn_open(2026, 10, 1, 3, cutoff=None))


class TestTradingDayFilterNormalizesCodes(unittest.TestCase):
    def _run(self, open_markets):
        import main

        config = SimpleNamespace(trading_day_check_enabled=True, market_review_enabled=False)
        args = SimpleNamespace(force_run=False, no_market_review=True)
        with patch("src.core.trading_calendar.get_open_markets_today", return_value=open_markets) as mocked:
            result = main._compute_trading_day_filter(config, args, ["600900.SH", "300750.SZ", "600887"])
        mocked.assert_called_once_with(session_cutoff_hour=main.TRADING_DAY_CUTOFF_HOUR)
        return result

    def test_suffixed_codes_are_skipped_on_closed_day(self):
        filtered, _region, should_skip = self._run(set())
        self.assertEqual(filtered, [])
        self.assertTrue(should_skip)

    def test_suffixed_codes_kept_on_open_day(self):
        filtered, _region, should_skip = self._run({"cn"})
        self.assertEqual(filtered, ["600900.SH", "300750.SZ", "600887"])
        self.assertFalse(should_skip)


if __name__ == "__main__":
    unittest.main()
