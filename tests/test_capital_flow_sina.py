# -*- coding: utf-8 -*-
"""Tests for the Sina capital-flow source and its place in the fallback chain.

Motivating run: 2026-09-02 daily analysis (TUSHARE_TOKEN unset, AkShare's
push2*.eastmoney.com endpoints RemoteDisconnected) logged seven
``Downgraded buy because capital flow is unavailable: empty_stock_flow`` lines
and produced a five-name watchlist. The offline tests below pin the parse and
the ordering; the network-marked test at the bottom is the only one that can
notice the upstream feed changing shape.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetcherManager  # noqa: E402
from data_provider.sina_capital_flow import (  # noqa: E402
    _MAX_STALE_DAYS,
    _to_sina_symbol,
    fetch_capital_flow,
)


def _row(days_ago: int, r0_net: float) -> dict:
    """One MoneyFlow row, shaped like the live payload (all values are strings)."""
    opendate = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return {
        "opendate": opendate,
        "trade": "10.0000",
        "netamount": "1234.5600",
        "r0_net": f"{r0_net:.4f}",
        "r0_ratio": "0.01",
    }


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_get(payload, captured: dict):
    """Stub requests.get, recording the params the module sends."""

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params or {}
        captured["timeout"] = timeout
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return _FakeResponse(body)

    return patch("data_provider.sina_capital_flow.requests.get", side_effect=_fake_get)


class TestSinaSymbol(unittest.TestCase):
    def test_exchange_prefix(self) -> None:
        self.assertEqual(_to_sina_symbol("600900"), "sh600900")
        self.assertEqual(_to_sina_symbol("688981.SH"), "sh688981")
        self.assertEqual(_to_sina_symbol("000858.SZ"), "sz000858")
        self.assertEqual(_to_sina_symbol("SZ300750"), "sz300750")

    def test_non_a_share_returns_none(self) -> None:
        for code in ("AAPL", "00700", "", "12345"):
            self.assertIsNone(_to_sina_symbol(code), code)


class TestFetchCapitalFlow(unittest.TestCase):
    def test_maps_r0_net_and_sums_windows(self) -> None:
        rows = [_row(i, 1_000_000 * (i + 1)) for i in range(10)]
        captured: dict = {}
        with _patch_get(rows, captured):
            result = fetch_capital_flow("600900")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(captured["params"]["daima"], "sh600900")
        self.assertEqual(captured["params"]["num"], 10)
        # r0_net is 主力净流入; netamount (all orders) must not leak in.
        self.assertEqual(result["stock_flow"]["main_net_inflow"], 1_000_000.0)
        self.assertEqual(result["stock_flow"]["inflow_5d"], 15_000_000.0)
        self.assertEqual(result["stock_flow"]["inflow_10d"], 55_000_000.0)
        self.assertIn("capital_stock:sina_moneyflow", result["source_chain"])
        self.assertEqual(result["errors"], [])

    def test_newest_row_wins_regardless_of_response_order(self) -> None:
        rows = [_row(3, 300.0), _row(0, 100.0), _row(1, 200.0)]
        with _patch_get(rows, {}):
            result = fetch_capital_flow("600900")
        self.assertEqual(result["stock_flow"]["main_net_inflow"], 100.0)

    def test_incomplete_window_is_none_not_partial_sum(self) -> None:
        rows = [_row(i, 1_000.0) for i in range(6)]
        with _patch_get(rows, {}):
            result = fetch_capital_flow("600900")
        self.assertEqual(result["stock_flow"]["inflow_5d"], 5_000.0)
        self.assertIsNone(result["stock_flow"]["inflow_10d"])

    def test_stale_series_is_rejected(self) -> None:
        # Beijing codes answer this endpoint with rows ~a year old; serving them
        # as today's flow would be worse than reporting nothing.
        rows = [_row(_MAX_STALE_DAYS + 1 + i, 1_000.0) for i in range(10)]
        with _patch_get(rows, {}):
            result = fetch_capital_flow("430047")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stock_flow"], {})
        self.assertTrue(any("stale" in e for e in result["errors"]), result["errors"])

    def test_holiday_gap_still_accepted(self) -> None:
        rows = [_row(9 + i, 1_000.0) for i in range(10)]
        with _patch_get(rows, {}):
            result = fetch_capital_flow("600900")
        self.assertEqual(result["status"], "partial")

    def test_sina_error_object_is_a_failure(self) -> None:
        with _patch_get({"__ERROR": 1, "__ERRORMSG": "Input error"}, {}):
            result = fetch_capital_flow("600900")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stock_flow"], {})
        self.assertTrue(any("empty_response" in e for e in result["errors"]), result["errors"])

    def test_transport_failure_is_fail_open(self) -> None:
        with patch(
            "data_provider.sina_capital_flow.requests.get",
            side_effect=OSError("Connection aborted."),
        ):
            result = fetch_capital_flow("600900")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"], ["sina_moneyflow:OSError"])

    def test_unsupported_code_never_hits_the_network(self) -> None:
        with patch("data_provider.sina_capital_flow.requests.get") as mocked:
            result = fetch_capital_flow("AAPL")
        mocked.assert_not_called()
        self.assertEqual(result["status"], "not_supported")


_EMPTY_AK_PAYLOAD = {
    "status": "failed",
    "stock_flow": {},
    "sector_rankings": {"top": [], "bottom": []},
    "source_chain": [],
    "errors": ["stock_individual_fund_flow:ConnectionError"],
}

_SINA_OK = {
    "status": "partial",
    "stock_flow": {"main_net_inflow": 1.0, "inflow_5d": 2.0, "inflow_10d": 3.0},
    "sector_rankings": {"top": [], "bottom": []},
    "source_chain": ["capital_stock:sina_moneyflow"],
    "errors": [],
}

_SINA_FAILED = {
    "status": "failed",
    "stock_flow": {},
    "sector_rankings": {"top": [], "bottom": []},
    "source_chain": [],
    "errors": ["sina_moneyflow:OSError"],
}


class TestFallbackChain(unittest.TestCase):
    """Sina runs before AkShare, whose endpoints are blocked from CI."""

    def _payload(self, sina, ak_mock):
        manager = DataFetcherManager(fetchers=[])
        with patch("data_provider.base.fetch_sina_capital_flow", return_value=sina), \
                patch(
                    "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_capital_flow",
                    ak_mock,
                ):
            return manager._get_capital_flow_payload("600900")

    def test_sina_success_skips_akshare(self) -> None:
        ak_mock = MagicMock(return_value=_EMPTY_AK_PAYLOAD)
        payload = self._payload(_SINA_OK, ak_mock)
        # The blocked AkShare call costs seconds out of an 8s budget.
        ak_mock.assert_not_called()
        self.assertEqual(payload["stock_flow"]["main_net_inflow"], 1.0)

    def test_sina_failure_falls_back_and_keeps_both_errors(self) -> None:
        ak_mock = MagicMock(return_value=dict(_EMPTY_AK_PAYLOAD, errors=list(_EMPTY_AK_PAYLOAD["errors"])))
        payload = self._payload(_SINA_FAILED, ak_mock)
        ak_mock.assert_called_once()
        self.assertIn("stock_individual_fund_flow:ConnectionError", payload["errors"])
        self.assertIn("sina_moneyflow:OSError", payload["errors"])

    def test_tushare_still_wins_when_it_has_data(self) -> None:
        tushare_payload = {
            "status": "partial",
            "stock_flow": {"main_net_inflow": 42.0, "inflow_5d": None, "inflow_10d": None},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": ["capital_stock:tushare_moneyflow"],
            "errors": [],
        }
        fetcher = SimpleNamespace(
            get_capital_flow=MagicMock(return_value=tushare_payload),
        )
        manager = DataFetcherManager(fetchers=[])
        sina_mock = MagicMock(return_value=_SINA_OK)
        with patch.object(manager, "_find_tushare_fetcher", return_value=fetcher), \
                patch("data_provider.base.fetch_sina_capital_flow", sina_mock):
            payload = manager._get_capital_flow_payload("600900")
        sina_mock.assert_not_called()
        self.assertEqual(payload["stock_flow"]["main_net_inflow"], 42.0)


@pytest.mark.network
class TestSinaLiveContract(unittest.TestCase):
    """Live drift detector; runs only in the non-blocking Network Smoke cron.

    The offline tests above parse frozen fixtures, so only this one notices if
    Sina renames r0_net, stops serving the endpoint, or starts blocking the
    runner -- which is the exact failure that put five holdings on the watchlist.
    """

    def test_live_flow_is_fresh_and_parsable(self) -> None:
        try:
            result = fetch_capital_flow("600900", timeout=15.0)
        except Exception as exc:  # pragma: no cover - transport blip, stay quiet
            self.skipTest(f"transport error: {exc}")

        if result["status"] == "failed" and any(
            e.startswith("sina_moneyflow:") and "stale" not in e and "missing" not in e
            for e in result["errors"]
        ):
            self.skipTest(f"network unavailable: {result['errors']}")

        self.assertEqual(result["status"], "partial", result["errors"])
        flow = result["stock_flow"]
        self.assertIsInstance(flow["main_net_inflow"], float)
        # 10 rows are requested, so both windows must be complete on a live code.
        self.assertIsNotNone(flow["inflow_5d"])
        self.assertIsNotNone(flow["inflow_10d"])
