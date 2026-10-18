# -*- coding: utf-8 -*-
"""A 股快讯与政策（src/cn_market_feeds.py）：解析、个股匹配、大盘卡片缓存与渲染。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src import cn_market_feeds as cf
from src import substack_digest as sd

CN = timezone(timedelta(hours=8))

SINA = {"result": {"data": {"feed": {"list": [
    {"create_time": "2026-10-19 09:30:00", "rich_text": "<p>宁德时代：前三季度净利润同比增长 30%</p>", "is_focus": 1},
    {"create_time": "2026-10-19 09:00:00", "rich_text": "央行开展 2000 亿元逆回购操作", "is_focus": 0},
    {"create_time": "2026-10-19 08:00:00", "rich_text": "300750 获北向资金增持", "is_focus": 0},
]}}}}

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>证监会发布新规</title><description>证监会发布新规，自 11 月起实施</description>
<pubDate>Mon, 19 Oct 2026 01:00:00 GMT</pubDate><link>https://x/1</link></item>
</channel></rss>""".encode("utf-8")


def test_parse_sources():
    rows = cf.parse_sina(SINA)
    assert [r["text"] for r in rows][0] == "宁德时代：前三季度净利润同比增长 30%"
    assert rows[0]["time"] == datetime(2026, 10, 19, 9, 30, tzinfo=CN) and rows[0]["focus"] is True
    rss = cf.parse_rss(RSS, "证监会", {"market"})
    assert rss[0]["text"] == "证监会发布新规，自 11 月起实施" and rss[0]["uses"] == {"market"}


def test_stock_flash_matches_name_or_code_and_ignores_market_only_sources():
    items = cf.parse_sina(SINA) + cf.parse_rss(RSS, "证监会", {"market"})
    flash = cf.stock_flash("300750", "宁德时代", items)
    assert [f["text"][:6] for f in flash] == ["宁德时代：前", "300750"]
    assert flash[0]["time"] == "10-19 09:30" and flash[0]["source"] == "新浪7×24"
    assert cf.stock_flash("600519", "贵州茅台", items) == []
    assert "未核实" in cf.render_stock_prompt(flash) and cf.render_stock_prompt([]) == ""


@pytest.fixture
def market_items(tmp_path, monkeypatch):
    monkeypatch.setenv(sd.ENV_CACHE, str(tmp_path / "cache.json"))
    sd.reset()
    cf.reset()
    cf._items = cf.parse_rss(RSS, "证监会", {"market"}) + cf.parse_sina(SINA)
    yield
    cf.reset()
    sd.reset()


def test_market_digest_is_cached_and_rendered(market_items):
    card = {"summary": "监管新规出台", "policy": [{"text": "证监会发布新规", "source": "证监会", "time": "10-19"}],
            "data": [], "overseas": [], "sectors": [], "commentary": [{"text": "某券商称利好", "source": "", "time": ""}]}
    calls = []
    got = cf.market_digest(lambda p: calls.append(p) or json.dumps(card, ensure_ascii=False))
    assert got["summary"] == "监管新规出台" and got["sources"] == ["证监会"]
    assert "证监会发布新规" in calls[0] and "宁德时代" not in calls[0]   # 只用 market 来源
    sd._cache = None
    again = cf.market_digest(lambda p: pytest.fail("should use cache"))
    assert again["policy"][0]["text"] == "证监会发布新规"
    report = cf.render_market_report(got)
    assert "【政策】证监会发布新规（证监会 10-19）" in report and "【观点（非事实）】某券商称利好" in report
    assert "不要据此给出择时建议" in cf.render_market_prompt(got)
    assert cf.render_market_report(None) == ""


def test_cn_market_substack_mapping():
    assert "CN" in sd.MARKET_TICKERS["cn"] and sd.MARKET_NAMES["cn"] == "A 股"
