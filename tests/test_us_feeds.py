# -*- coding: utf-8 -*-
"""美股内部人买入名单（src/us_insider_feed.py）与美股估值红旗（src/valuation_red_flags.py 的 us 分支）。"""

import json
from datetime import date, datetime, timezone

import pytest

from src import us_insider_feed as feed
from src import valuation_red_flags as vrf
from src.core.tiered_report import _program_flag_text
from src.program_checklist import build_program_checklist

FEED = {
    "status": "ok",
    "generated_at": "2026-10-16T22:10:00+00:00",
    "window": ["2026-10-02", "2026-10-15"],
    "items": [{"ticker": "BRK-B", "name": "Berkshire", "value": 2.5e6, "roles": ["Director"],
               "last_filed": "20261014"}],
    "universe": ["AAPL", "BRK-B", "MSFT"],
}


@pytest.fixture
def insider_file(tmp_path, monkeypatch):
    def write(payload):
        path = tmp_path / "insider_buys.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv(feed.ENV_FILE, str(path))
        feed.reset_cache()
    yield write
    feed.reset_cache()


def test_insider_hit_none_and_not_covered(insider_file, monkeypatch):
    insider_file(FEED)
    monkeypatch.setattr(feed, "datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: datetime(2026, 10, 17, tzinfo=timezone.utc)),
        "fromisoformat": staticmethod(datetime.fromisoformat)}))
    hit = feed.insider_buy_for("BRK.B")
    assert hit["flag"] is True and "Director 合计 $2.5M" in feed.describe(hit)
    assert feed.insider_buy_for("AAPL") == {"flag": False, "window": FEED["window"], "row": None}
    assert feed.insider_buy_for("BF.B") is None  # 不在覆盖范围，不能说「没有买入」
    assert "近期内部人公开市场买入" in feed.render_insider_prompt(hit)
    assert feed.render_insider_prompt(feed.insider_buy_for("AAPL")) == ""

    items = build_program_checklist(None, None, "zh", None, market="us", us_surge=None, us_insider=hit)
    assert items[1].startswith("💡")
    none = build_program_checklist(None, None, "zh", None, market="us", us_surge=None,
                                   us_insider=feed.insider_buy_for("AAPL"))
    assert none[1].startswith("➖") and "2026-10-02 ~ 2026-10-15" in none[1]


def test_insider_failed_or_stale_feed_is_unknown(insider_file):
    insider_file({**FEED, "status": "failed", "items": []})
    assert feed.insider_buy_for("AAPL") is None
    insider_file({**FEED, "generated_at": "2026-09-01T00:00:00+00:00"})
    assert feed.insider_buy_for("AAPL") is None


def test_summary_column_marks_insider_buy_separately():
    assert _program_flag_text({"program_flags": {"surge": True, "insider_buy": True}}) == "⚠️ 高位放量异动；💡 近期内部人买入"
    assert _program_flag_text({"program_flags": {"surge": False, "insider_buy": False}}) == "—"


def test_us_red_flags_lookup_by_ticker(tmp_path, monkeypatch):
    payload = {"stocks": {"BRK-B": {"date": "2026-10-10", "confidence": "Medium",
                                    "flags": ["Material weakness"], "company_risks": ["Succession"]}}}
    path = tmp_path / "us.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(vrf.US_ENV_FILE, str(path))
    monkeypatch.delenv(vrf.ENV_FILE, raising=False)
    monkeypatch.delenv(vrf.ENV_TOKEN, raising=False)
    vrf.reset_cache()
    try:
        entry = vrf.red_flags_for("BRK.B", date(2026, 10, 17))
        assert entry["flags"] == ["Material weakness"] and entry["source"] == "美股估值报告"
        assert "美股估值报告" in vrf.render_red_flags_prompt(entry)
        assert vrf.red_flags_for("AAPL", date(2026, 10, 17)) is None
        assert vrf.red_flags_for("600519", date(2026, 10, 17)) is None  # A 股文件未配置
    finally:
        vrf.reset_cache()
