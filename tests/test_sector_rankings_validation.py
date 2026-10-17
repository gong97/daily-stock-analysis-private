# -*- coding: utf-8 -*-
"""板块排行的数据校验：数据源返回拦截页（名称 NaN、涨跌幅 0）时不能当成功（2026-10-16 CI 事故）。"""

from unittest import mock

import numpy as np
import pandas as pd

from data_provider.akshare_fetcher import AkshareFetcher
from src.market_analyzer import _valid_sectors


def _fetcher():
    f = AkshareFetcher.__new__(AkshareFetcher)
    f._set_random_user_agent = lambda: None
    f._enforce_rate_limit = lambda: None
    return f


def test_ths_garbage_falls_through_to_sina():
    junk = pd.DataFrame({"板块": [np.nan] * 10, "涨跌幅": [0.0] * 10})
    sina = pd.DataFrame({"板块": [f"行业{i}" for i in range(10)], "涨跌幅": np.linspace(-3, 3, 10)})
    with mock.patch("akshare.stock_board_industry_summary_ths", return_value=junk), \
            mock.patch("akshare.stock_board_industry_name_em", side_effect=RuntimeError("blocked")), \
            mock.patch("akshare.stock_sector_spot", return_value=sina):
        top, bottom = _fetcher().get_sector_rankings(5)
    assert [s["name"] for s in top][:1] == ["行业9"]
    assert [s["name"] for s in bottom][:1] == ["行业0"]


def test_valid_ths_data_is_used_directly():
    ths = pd.DataFrame({"板块": [f"板块{i}" for i in range(8)], "涨跌幅": [5, 4, 3, 2, 1, -1, -2, -3]})
    with mock.patch("akshare.stock_board_industry_summary_ths", return_value=ths):
        top, bottom = _fetcher().get_sector_rankings(5)
    assert top[0] == {"name": "板块0", "change_pct": 5}
    assert bottom[0]["name"] == "板块7"


def test_market_review_drops_nan_named_sectors():
    items = [{"name": float("nan"), "change_pct": 0.0}, {"name": "nan", "change_pct": 0.0},
             {"name": " ", "change_pct": 1.0}, {"name": "军工装备", "change_pct": 5.61}]
    assert _valid_sectors(items) == [{"name": "军工装备", "change_pct": 5.61}]
    assert _valid_sectors(None) == []
