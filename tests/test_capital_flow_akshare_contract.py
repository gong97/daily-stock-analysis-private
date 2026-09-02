# -*- coding: utf-8 -*-
"""
Contract tests for the AkShare capital-flow call signature.

These tests exist because the previous candidate list called AkShare with
parameter names that do not exist (``symbol=``) and omitted the required
``market`` argument. Every call failed, but the mock-based tests in
test_data_tools_get_capital_flow.py still passed because they stubbed the
manager out entirely. These tests assert against the *real* signature
(via inspect, no network) so a rename upstream fails loudly.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.fundamental_adapter import (
    AkshareFundamentalAdapter,
    _infer_ak_market,
)


class TestInferAkMarket(unittest.TestCase):
    """market must be derived from the code, since it is stripped upstream."""

    def test_shanghai_codes(self) -> None:
        for code in ("600519", "601318", "688981", "600519.SH", "SH600519"):
            self.assertEqual(_infer_ak_market(code), "sh", code)

    def test_shenzhen_codes(self) -> None:
        for code in ("000001", "002415", "300750", "000001.SZ", "SZ000001"):
            self.assertEqual(_infer_ak_market(code), "sz", code)

    def test_beijing_codes(self) -> None:
        for code in ("920748", "830799", "430139", "920748.BJ"):
            self.assertEqual(_infer_ak_market(code), "bj", code)

    def test_non_a_share_returns_none(self) -> None:
        for code in ("AAPL", "HK00700", "7203.T", "", "12345"):
            self.assertIsNone(_infer_ak_market(code), code)


class TestAkshareCallSignature(unittest.TestCase):
    """The kwargs we send must be accepted by the installed AkShare."""

    def test_kwargs_bind_to_real_akshare_signature(self) -> None:
        import inspect
        try:
            import akshare as ak
        except ImportError:
            self.skipTest("akshare not installed")

        # Exactly the kwargs the adapter sends.
        inspect.signature(ak.stock_individual_fund_flow).bind(
            stock="600519", market="sh"
        )
        inspect.signature(ak.stock_sector_fund_flow_rank).bind(
            indicator="今日", sector_type="行业资金流"
        )

    def test_adapter_passes_market_argument(self) -> None:
        """Regression: a SZ code must not be queried with the default market='sh'."""
        calls = []

        def _fake_call(candidates):
            calls.append(candidates)
            return None, None, []

        adapter = AkshareFundamentalAdapter()
        with patch.object(adapter, "_call_df_candidates", side_effect=_fake_call):
            adapter.get_capital_flow("000001")

        # calls[0] is the per-stock probe; calls[1] is the sector ranking probe.
        func_name, kwargs = calls[0][0]
        self.assertEqual(func_name, "stock_individual_fund_flow")
        self.assertEqual(kwargs, {"stock": "000001", "market": "sz"})
        self.assertNotIn("symbol", kwargs)


class TestFailureIsNotReportedAsUnsupported(unittest.TestCase):
    """A failed fetch on a valid A-share must not masquerade as not_supported."""

    def test_all_candidates_failing_yields_failed(self) -> None:
        adapter = AkshareFundamentalAdapter()
        with patch.object(
            adapter,
            "_call_df_candidates",
            return_value=(None, None, ["stock_individual_fund_flow:ConnectionError"]),
        ):
            result = adapter.get_capital_flow("600519")

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["errors"])

    def test_clean_empty_yields_not_supported(self) -> None:
        adapter = AkshareFundamentalAdapter()
        with patch.object(adapter, "_call_df_candidates", return_value=(None, None, [])):
            result = adapter.get_capital_flow("600519")

        self.assertEqual(result["status"], "not_supported")


if __name__ == "__main__":
    unittest.main()
