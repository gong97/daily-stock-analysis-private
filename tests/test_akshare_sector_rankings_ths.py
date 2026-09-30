# -*- coding: utf-8 -*-
"""行业板块排行：同花顺优先，失败才退到东财、新浪（2026-09-30）。"""

import os
import sys
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.akshare_fetcher import AkshareFetcher  # noqa: E402


def _ths_df():
    return pd.DataFrame({
        "序号": [1, 2, 3],
        "板块": ["生物制品", "医疗服务", "煤炭开采"],
        "涨跌幅": [4.63, 3.11, -2.05],
        "净流入": [14.36, 20.08, -5.0],
    })


def _em_df():
    return pd.DataFrame({"板块名称": ["东财行业A", "东财行业B"], "涨跌幅": [1.0, -1.0]})


class TestSectorRankingsThsFirst(unittest.TestCase):
    def setUp(self):
        self.fetcher = AkshareFetcher(sleep_min=0, sleep_max=0)

    def test_uses_ths_and_skips_eastmoney(self):
        with patch("akshare.stock_board_industry_summary_ths", return_value=_ths_df()), \
                patch("akshare.stock_board_industry_name_em") as em, \
                patch("akshare.stock_sector_spot") as sina:
            top, bottom = self.fetcher.get_sector_rankings(2)

        self.assertEqual([s["name"] for s in top], ["生物制品", "医疗服务"])
        self.assertEqual(bottom[0], {"name": "煤炭开采", "change_pct": -2.05})
        em.assert_not_called()
        sina.assert_not_called()

    def test_falls_back_to_eastmoney_when_ths_fails(self):
        with patch("akshare.stock_board_industry_summary_ths", side_effect=ConnectionError("blocked")), \
                patch("akshare.stock_board_industry_name_em", return_value=_em_df()), \
                patch("akshare.stock_sector_spot") as sina:
            top, _bottom = self.fetcher.get_sector_rankings(1)

        self.assertEqual(top[0]["name"], "东财行业A")
        sina.assert_not_called()

    def test_falls_back_when_ths_missing_columns(self):
        with patch("akshare.stock_board_industry_summary_ths", return_value=pd.DataFrame({"x": [1]})), \
                patch("akshare.stock_board_industry_name_em", return_value=_em_df()):
            top, _bottom = self.fetcher.get_sector_rankings(1)
        self.assertEqual(top[0]["name"], "东财行业A")


if __name__ == "__main__":
    unittest.main()
