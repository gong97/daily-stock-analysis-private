# -*- coding: utf-8 -*-
"""Substack 观点卡片（src/substack_digest.py）：feed 解析、粗筛、提炼缓存、渲染。"""

import json

import pytest

from src import substack_digest as sd

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
<channel><title>Deep Value Notes</title>
<item><title>Why Apple is cheap</title><link>https://x.substack.com/p/apple</link>
<dc:creator>Jane</dc:creator><pubDate>Thu, 15 Oct 2026 12:00:00 GMT</pubDate>
<content:encoded><![CDATA[<p>Apple (AAPL) grew services revenue 14% last year.</p><p>We think Apple can compound.</p>""" + b"<p>filler</p>" * 400 + b"""]]></content:encoded></item>
<item><title>Paid post</title><link>https://x.substack.com/p/paid</link><pubDate>Wed, 14 Oct 2026 12:00:00 GMT</pubDate>
<description>Short teaser about $AAPL and Apple.</description></item>
</channel></rss>"""

CARD = {"relevant": True, "direction": "看多", "thesis": "服务收入带动估值重估",
        "facts": [{"claim": "服务收入增长 14%", "quote": "grew services revenue 14% last year"}],
        "judgments": ["可以持续复利"], "valuation": {"method": "DCF", "assumptions": "增长 8%", "target": "$400"},
        "catalysts": [], "risks_mentioned": [], "risks_missing": ["反垄断"],
        "falsifiers": ["服务收入增速跌破 8%"], "author_position": "未披露"}


def test_feed_urls_normalise():
    assert sd.feed_urls("x.substack.com, https://y.substack.com/feed\nhttps://z.com/") == [
        "https://x.substack.com/feed", "https://y.substack.com/feed", "https://z.com/feed"]


def test_parse_feed_keeps_full_text_and_marks_teasers():
    items = sd.parse_feed(FEED, "https://x.substack.com/feed")
    assert [i["title"] for i in items] == ["Why Apple is cheap", "Paid post"]
    assert items[0]["publication"] == "Deep Value Notes" and items[0]["author"] == "Jane"
    assert "grew services revenue 14%" in items[0]["text"] and items[0]["partial"] is False
    assert items[1]["partial"] is True


def test_mention_count_needs_real_mentions():
    text = "Apple (AAPL) is cheap. Apple Inc. ships phones."
    assert sd.mention_count(text, "AAPL", ["苹果", "Apple Inc."]) >= 2
    assert sd.mention_count("We like meta-analysis.", "META", ["Meta Platforms, Inc."]) == 0
    assert sd.mention_count("宁德时代（300750）发布三季报，宁德时代", "300750", ["宁德时代"]) == 3


@pytest.fixture
def articles(tmp_path, monkeypatch):
    monkeypatch.setenv(sd.ENV_CACHE, str(tmp_path / "cache.json"))
    sd.reset()
    sd._articles = sd.parse_feed(FEED, "https://x.substack.com/feed")
    yield tmp_path
    sd.reset()


def test_cards_are_extracted_once_and_cached(articles):
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return "```json\n" + json.dumps(CARD, ensure_ascii=False) + "\n```"

    cards = sd.cards_for("AAPL", ["苹果", "Apple Inc."], generate)
    assert [c["title"] for c in cards] == ["Why Apple is cheap", "Paid post"]
    assert cards[1]["partial"] is True and "付费文章的公开摘录" in calls[1]
    sd._cache = None  # 模拟下一次运行：从文件读
    again = sd.cards_for("AAPL", ["Apple Inc."], lambda p: pytest.fail("should use cache"))
    assert len(again) == 2


def test_irrelevant_results_are_cached_unparseable_ones_are_not(articles, monkeypatch):
    assert sd.cards_for("AAPL", ["Apple Inc."], lambda p: json.dumps({"relevant": False})) == []
    cache = json.loads((articles / "cache.json").read_text(encoding="utf-8"))
    assert len(cache) == 2 and all(v["data"]["relevant"] is False for v in cache.values())

    monkeypatch.setenv(sd.ENV_CACHE, str(articles / "fresh.json"))
    sd.reset()
    sd._articles = sd.parse_feed(FEED, "https://x.substack.com/feed")
    assert sd.cards_for("AAPL", ["Apple Inc."], lambda p: "not json") == []
    assert not (articles / "fresh.json").exists()   # 解析失败的不缓存，下次重试


def test_render_marks_cards_as_unverified():
    card = {**CARD, "title": "Why Apple is cheap", "link": "https://x", "publication": "Deep Value Notes",
            "published": "2026-10-15", "partial": False}
    prompt = sd.render_prompt([card])
    assert "未核实的观点" in prompt and "不要直接采用作者的目标价" in prompt and "服务收入增长 14%" in prompt
    lines = "\n".join(sd.render_report_lines([card]))
    assert "[Why Apple is cheap](https://x)" in lines and "证伪条件：服务收入增速跌破 8%" in lines
    assert sd.render_prompt([]) == "" and sd.render_report_lines([]) == []


def test_feed_specs_read_the_ticker_table():
    raw = ("AAPL / MSFT\tApp Economy Insights\thttps://www.appeconomyinsights.com/feed\n"
           "同上\tBig Technology\thttps://www.bigtechnology.com\n"
           "BRK.B\tKingswell\thttps://www.kingswell.io/feed\n"
           "https://free.substack.com\n")
    specs = {s["url"]: s["tickers"] for s in sd.feed_specs(raw)}
    assert specs == {
        "https://www.appeconomyinsights.com/feed": {"AAPL", "MSFT"},
        "https://www.bigtechnology.com/feed": {"AAPL", "MSFT"},
        "https://www.kingswell.io/feed": {"BRK.B"},
        "https://free.substack.com/feed": None,
    }


def test_short_company_names_count_but_generic_first_words_do_not():
    names = sd._name_variants(["Meta Platforms, Inc.", "Amazon.com, Inc.", "Brown Forman Inc"])
    assert "Meta" in names and "Amazon" in names and "Brown" not in names


def test_mapped_feed_only_feeds_its_tickers(articles):
    for a in sd._articles:
        a["tickers"] = ["MSFT"]
    assert sd.cards_for("AAPL", ["Apple Inc."], lambda p: pytest.fail("not for AAPL")) == []


def test_market_cards_take_the_latest_post_of_each_macro_feed(articles):
    from datetime import datetime, timezone

    base = sd._articles[0]
    sd._articles = [
        {**base, "link": "https://m/1", "publication": "Macro A", "published": "2026-10-16T00:00:00+00:00", "tickers": ["SPY"]},
        {**base, "link": "https://m/0", "publication": "Macro A", "published": "2026-10-10T00:00:00+00:00", "tickers": ["SPY"]},
        {**base, "link": "https://m/2", "publication": "Rates B", "published": "2026-10-15T00:00:00+00:00", "tickers": ["TLT"]},
        {**base, "link": "https://s/1", "publication": "Stocks", "published": "2026-10-16T00:00:00+00:00", "tickers": ["AAPL"]},
        {**base, "link": "https://m/old", "publication": "Old", "published": "2026-09-01T00:00:00+00:00", "tickers": ["QQQ"]},
    ]
    seen = []

    def generate(prompt):
        seen.append(prompt)
        return json.dumps({"relevant": True, "stance": "对股市偏空", "thesis": "估值过高", "implications": ["防御"],
                           "horizon": "几个月", "falsifiers": ["盈利上修"]}, ensure_ascii=False)

    cards = sd.market_cards("us", generate, now=datetime(2026, 10, 18, tzinfo=timezone.utc))
    assert [c["link"] for c in cards] == ["https://m/1", "https://m/2"]
    assert len(seen) == 2 and "美股大盘" in seen[0]
    report = sd.render_market_report(cards)
    assert "外部宏观观点（Substack，未核实）" in report and "证伪条件：盈利上修" in report
    assert "不要把它们当成你自己的结论" in sd.render_market_prompt(cards)
    assert sd.market_cards("cn", generate) == []
