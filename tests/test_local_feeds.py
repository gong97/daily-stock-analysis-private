# -*- coding: utf-8 -*-
"""本机抓取的兜底源（src/local_feeds.py）及其在 Substack / A 股快讯里的使用。"""

from datetime import datetime, timedelta, timezone

import pytest

from src import cn_market_feeds as cf
from src import local_feeds as lf
from src import substack_digest as sd
from tests.test_substack_digest import FEED

NOW = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def release(monkeypatch):
    lf.reset()
    monkeypatch.setattr(lf, "_loaded", True)
    monkeypatch.setattr(lf, "_release", {
        "assets": {"a.xml": "https://api/a", "pboc_omo.xml": "https://api/p", "index.json": "https://api/i"},
        "index": {
            "https://x.substack.com/feed": {"asset": "a.xml", "fetched_at": (NOW - timedelta(hours=10)).isoformat()},
            "pboc_omo": {"asset": "pboc_omo.xml", "fetched_at": (NOW - timedelta(hours=100)).isoformat()},
        },
    })
    monkeypatch.setenv("VALUATION_RED_FLAGS_TOKEN", "t")

    class Resp:
        def __init__(self, body):
            self.content = body

        def raise_for_status(self):
            pass

    import requests
    monkeypatch.setattr(requests, "get", lambda url, **kw: Resp(FEED if url == "https://api/a" else b"<rss/>"))
    yield
    lf.reset()


def test_fresh_entry_is_returned_and_stale_one_is_not(release):
    assert lf.get("https://x.substack.com/feed", now=NOW) == FEED
    assert lf.get("pboc_omo", now=NOW) is None          # 100 小时前，超过 72 小时
    assert lf.get("https://unknown/feed", now=NOW) is None


def test_substack_falls_back_to_local_copy(monkeypatch, tmp_path):
    import requests

    sd.reset()
    monkeypatch.setenv(sd.ENV_FEEDS, "https://x.substack.com/feed")

    def blocked(url, **kw):
        raise requests.HTTPError("403 Forbidden")

    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(lf, "get", lambda key, now=None: FEED if key == "https://x.substack.com/feed" else None)
    articles = sd.load_articles(now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    assert [a["title"] for a in articles] == ["Why Apple is cheap", "Paid post"]
    sd.reset()


def test_cn_feeds_read_pboc_from_local_copy(monkeypatch):
    rss = ("<?xml version='1.0'?><rss version='2.0'><channel><title>t</title><item><title>公开市场业务交易公告 第201号</title>"
           "<description>开展了800亿元7天期逆回购操作</description><pubDate>Mon, 19 Oct 2026 01:00:00 GMT</pubDate></item>"
           "</channel></rss>").encode("utf-8")
    cf.reset()
    monkeypatch.delenv(cf.ENV_RSSHUB, raising=False)
    monkeypatch.setattr(cf, "_get", lambda url: b'{"result": {"data": {"feed": {"list": []}}}}')
    monkeypatch.setattr(lf, "get", lambda key, now=None: rss if key == "pboc_omo" else None)
    items = cf.load_items(now=datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    assert [i["source"] for i in items] == ["央行公开市场操作"]
    assert items[0]["text"] == "公开市场业务交易公告 第201号。开展了800亿元7天期逆回购操作"
    assert items[0]["uses"] == {"market"}
    cf.reset()
