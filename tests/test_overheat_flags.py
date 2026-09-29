# -*- coding: utf-8 -*-
"""Tests for src/overheat_flags.py — thresholds must match the Macd-Qlib definitions verbatim."""

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.overheat_flags import compute_overheat_flags, render_overheat_flags_prompt


def _bars(closes, volumes=None):
    n = len(closes)
    dates = pd.bdate_range("2026-03-02", periods=n)
    volumes = volumes if volumes is not None else [1_000_000.0] * n
    return pd.DataFrame({"date": dates, "close": closes, "volume": volumes})


class TestComputeOverheatFlags(unittest.TestCase):
    def test_too_few_bars_returns_none(self):
        self.assertIsNone(compute_overheat_flags(_bars([10.0] * 59)))

    def test_flat_series_triggers_nothing(self):
        flags = compute_overheat_flags(_bars([10.0] * 130), circ_mv=50e8, price=10.0)
        self.assertTrue(flags["high_zone"])          # 收盘就是 60 日最高
        self.assertFalse(flags["flag_a"])
        self.assertFalse(flags["flag_b"])
        self.assertFalse(flags["flag_c"])
        self.assertAlmostEqual(flags["volume_heat"], 1.0)

    def test_flag_a_needs_dist_above_11_14_pct_at_high(self):
        # 前 110 天 10 元，最后 20 天线性拉到 13.5：MA20 = 11.85，距 MA20 +13.9%，且在 60 日最高
        closes = [10.0] * 110 + list(np.linspace(10.2, 13.5, 20))
        flags = compute_overheat_flags(_bars(closes))
        self.assertGreater(flags["dist_ma20"], 0.1114)
        self.assertTrue(flags["flag_a"])

    def test_flag_a_not_at_high_zone(self):
        # 同样距 MA20 很远，但 60 日内曾到过 20 元，现价不在高位
        closes = [10.0] * 70 + [20.0] + [10.0] * 39 + list(np.linspace(10.2, 13.5, 20))
        flags = compute_overheat_flags(_bars(closes))
        self.assertFalse(flags["high_zone"])
        self.assertFalse(flags["flag_a"])

    def test_flag_b_volume_heat_and_pct(self):
        closes = [10.0] * 129 + [10.4]               # +4% 且新高
        volumes = [1e6] * 129 + [3.0e6]              # 3 倍于前 120 日中位数
        flags = compute_overheat_flags(_bars(closes, volumes))
        self.assertAlmostEqual(flags["volume_heat"], 3.0)
        self.assertAlmostEqual(flags["pct"], 4.0)
        self.assertTrue(flags["flag_b"])

    def test_flag_b_threshold_is_strict(self):
        closes = [10.0] * 129 + [10.4]
        volumes = [1e6] * 129 + [2.95e6]             # 2.95 < 2.953
        self.assertFalse(compute_overheat_flags(_bars(closes, volumes))["flag_b"])

    def test_heat_uses_prior_bars_only_with_min_60(self):
        # 只有 61 根：前 60 根满足 min_periods=60
        closes = [10.0] * 60 + [10.4]
        volumes = [1e6] * 60 + [4e6]
        flags = compute_overheat_flags(_bars(closes, volumes))
        self.assertAlmostEqual(flags["volume_heat"], 4.0)
        # 60 根时前面只有 59 根，算不出常态
        flags = compute_overheat_flags(_bars(closes[1:], volumes[1:]))
        self.assertIsNone(flags["volume_heat"])
        self.assertIsNone(flags["flag_b"])

    def test_flag_c_large_cap_uses_10_pct(self):
        closes = [10.0] * 129 + [10.3]
        # 流通股本 1e9 股（circ_mv 103 亿 / 10.3 元），当日成交 1.1e8 股 → 换手 11%
        volumes = [5e7] * 129 + [1.1e8]
        flags = compute_overheat_flags(_bars(closes, volumes), circ_mv=103e8, price=10.3)
        self.assertAlmostEqual(flags["turnover_pct"], 11.0)
        self.assertEqual(flags["turnover_threshold"], 10.0)
        self.assertTrue(flags["flag_c"])

    def test_flag_c_small_cap_uses_15_pct(self):
        closes = [10.0] * 129 + [10.3]
        volumes = [1e6] * 129 + [1.1e7]              # 流通股本 1e8 股 → 换手 11%
        flags = compute_overheat_flags(_bars(closes, volumes), circ_mv=10.3e8, price=10.3)
        self.assertEqual(flags["turnover_threshold"], 15.0)
        self.assertFalse(flags["flag_c"])

    def test_flag_c_excludes_limit_up_days(self):
        closes = [10.0] * 129 + [10.8]               # +8% >= 7%
        volumes = [5e7] * 129 + [1.2e8]
        flags = compute_overheat_flags(_bars(closes, volumes), circ_mv=108e8, price=10.8)
        self.assertFalse(flags["flag_c"])

    def test_flag_c_unknown_without_market_cap(self):
        flags = compute_overheat_flags(_bars([10.0] * 130))
        self.assertIsNone(flags["flag_c"])
        self.assertIsNone(flags["turnover_pct"])

    def test_unsorted_and_duplicate_dates(self):
        bars = _bars([10.0] * 129 + [10.4], [1e6] * 129 + [3e6])
        shuffled = pd.concat([bars.iloc[::-1], bars.iloc[[-1]]])
        self.assertTrue(compute_overheat_flags(shuffled)["flag_b"])


class TestRenderOverheatFlags(unittest.TestCase):
    def test_empty_renders_nothing(self):
        self.assertEqual(render_overheat_flags_prompt(None), "")

    def test_table_shows_marks_and_values(self):
        closes = [10.0] * 129 + [10.4]
        volumes = [1e6] * 129 + [3e6]
        text = render_overheat_flags_prompt(compute_overheat_flags(_bars(closes, volumes)))
        self.assertIn("### 过热风险旗标", text)
        self.assertIn("| B 高位放量急拉 | ⚠️ 触发 |", text)
        self.assertIn("| A 高位乖离过大 | ✅ 未触发 |", text)
        self.assertIn("| C 高位高换手 | ❓ 数据不足 |", text)
        self.assertIn("3.00 倍", text)
        self.assertNotIn("None", text)


if __name__ == "__main__":
    unittest.main()
