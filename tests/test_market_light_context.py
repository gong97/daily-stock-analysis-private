# -*- coding: utf-8 -*-
"""大盘上下文：带出结构化红绿灯；有红绿灯时风险标签只看它（与个股护栏同一口径）。"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.services.daily_market_context import DailyMarketContextService  # noqa: E402

SUMMARY = "市场分化，建议谨慎观望，等待确认，轻仓。"


def _build(payload, summary=SUMMARY):
    service = DailyMarketContextService(db_manager=object())
    return service._build_context_from_payload(
        region="cn", trade_date=date(2026, 9, 30), payload=payload, source="test", fallback_summary=summary,
    )


def test_yellow_light_carries_no_risk_tags_despite_cautious_words():
    ctx = _build({"market_light": {"status": "yellow", "guidance": "信号分化，控制仓位并等待量价确认。"}})
    assert ctx.market_light_status == "yellow"
    assert ctx.risk_tags == []
    assert ctx.to_safe_dict()["market_light_status"] == "yellow"


def test_red_light_tags_strong_risk():
    ctx = _build({"market_light": {"status": "red"}})
    assert set(ctx.risk_tags) == {"high_risk", "market_cooling"}


def test_without_light_only_strong_words_become_tags():
    assert _build({}).risk_tags == []
    assert "high_risk" in _build({}, summary="大盘退潮，高风险。").risk_tags
    assert _build({}).market_light_status is None
