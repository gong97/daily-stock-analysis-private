# -*- coding: utf-8 -*-
"""scripts/holdings_stock_list.py：A 股 DSA 名单 = 仓位 >= 0.5% 或近 20 个交易日交易过的个股 + 待买回。"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import holdings_stock_list as h  # noqa: E402


def _tables():
    pos = [
        {"date": "2026-11-11", "code": "000001", "name": "旧快照", "weight": "0.5"},
        {"date": "2026-11-12", "code": "300750", "name": "宁德时代", "weight": "0.2045"},
        {"date": "2026-11-12", "code": "510300", "name": "沪深300ETF", "weight": "0.0704"},
        {"date": "2026-11-12", "code": "600900", "name": "长江电力", "weight": "0.1388"},
        {"date": "2026-11-12", "code": "002298", "name": "中电鑫龙", "weight": "0.0017"},   # 小，但天天做 T
        {"date": "2026-11-12", "code": "002743", "name": "ST富煌", "weight": "0.0023"},     # 小，又没交易
        {"date": "2026-11-12", "code": "600031", "name": "三一重工", "weight": "0.0065"},
        {"date": "2026-11-12", "code": "CASH", "name": "现金", "weight": "0.02"},
    ]
    activity = [{"date": "2026-11-12", "code": "2298", "name": "中电鑫龙", "buy_shares": "300", "sell_shares": "300"},
                {"date": "2026-10-01", "code": "002743", "name": "ST富煌", "buy_shares": "100", "sell_shares": "0"}]
    swing = [
        {"day": "2026-11-02", "code": "600258", "name": "首旅酒店", "side": "sell", "left": "2000", "status": "进行中"},
        {"day": "2026-11-03", "code": "600258", "name": "首旅酒店", "side": "sell", "left": "2500", "status": "进行中"},
        {"day": "2026-11-04", "code": "600900", "name": "长江电力", "side": "sell", "left": "500", "status": "进行中"},
        {"day": "2026-10-27", "code": "002223", "name": "鱼跃医疗", "side": "sell", "left": "0", "status": "已买回"},
        {"day": "2026-11-05", "code": "000338", "name": "潍柴动力", "side": "buy", "left": "400", "status": "进行中"},
    ]
    return {"positions.csv": pos, "trade_activity.csv": activity, "trades.csv": [], "swing_review.csv": swing}


class BuildTest(unittest.TestCase):
    def test_selection(self):
        codes, notes, lines = h.build(_tables(), date(2026, 11, 12))
        # 最新快照、按仓位排序；ETF 不进；小仓位只有最近交易过的进；待买回接在后面，还持有的不重复
        self.assertEqual(codes, ["300750", "600900", "600031", "002298", "600258"])
        self.assertEqual(list(notes), ["600258"])
        self.assertIn("2 次卖出", notes["600258"])
        self.assertIn("4,500 股", notes["600258"])
        self.assertTrue(any("ST富煌" in line and "不分析" in line for line in lines))

    def test_recent_window_counts_weekdays(self):
        # 2026-11-12 往回 20 个工作日是 10-15：10-01 的交易不算
        self.assertEqual(h.weekdays_back(date(2026, 11, 12), 20), date(2026, 10, 15))

    def test_a_share_stock_filter(self):
        self.assertTrue(h.is_a_share_stock("600900") and h.is_a_share_stock("002298") and h.is_a_share_stock("300750"))
        self.assertFalse(h.is_a_share_stock("510300") or h.is_a_share_stock("159915") or h.is_a_share_stock("CASH"))

    def test_without_token_falls_back(self):
        import os
        saved = {e: os.environ.pop(e, None) for e in h.TOKEN_ENVS}
        try:
            self.assertEqual(h.main([]), 1)
        finally:
            for e, v in saved.items():
                if v is not None:
                    os.environ[e] = v


if __name__ == "__main__":
    unittest.main()
