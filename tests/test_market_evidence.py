# -*- coding: utf-8 -*-
"""美股提示词换用美股检验结论（src/market_evidence.py）、美股【程序】项（src/us_surge_flag.py）。"""

from pathlib import Path

import numpy as np
import pandas as pd

from src.agent import executor
from src.analyzer import GeminiAnalyzer
from src.market_evidence import US_REPLACEMENTS, evidence_market, localize_prompt
from src.program_checklist import build_program_checklist, program_flag_states
from src.tested_signals import annotate, tested_notes
from src.us_surge_flag import compute_us_surge_flag, render_us_surge_prompt

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = {
    "analyzer.LEGACY": GeminiAnalyzer.LEGACY_DEFAULT_SYSTEM_PROMPT,
    "analyzer.SYSTEM": GeminiAnalyzer.SYSTEM_PROMPT,
    "executor.LEGACY_AGENT": executor.LEGACY_DEFAULT_AGENT_SYSTEM_PROMPT,
    "executor.AGENT": executor.AGENT_SYSTEM_PROMPT,
    "growth_quality.yaml": (ROOT / "strategies" / "growth_quality.yaml").read_text(encoding="utf-8"),
    "event_driven.yaml": (ROOT / "strategies" / "event_driven.yaml").read_text(encoding="utf-8"),
}


def test_every_replacement_source_text_still_exists():
    # 提示词改了措辞而替换表没跟着改，美股就会悄悄拿到 A 股结论
    corpus = "\n".join(PROMPTS.values())
    missing = [cn for cn, _ in US_REPLACEMENTS if cn not in corpus]
    assert missing == []


def test_us_prompts_keep_no_ashare_evidence():
    for name, prompt in PROMPTS.items():
        localized = localize_prompt(prompt, "AAPL")
        for phrase in ("A 股历史检验", "中证 1000", "全市场前 10%", "主力净流入最强", "Macd-Qlib-Analyzer"):
            assert phrase not in localized, (name, phrase)


def test_non_us_prompts_unchanged():
    for prompt in PROMPTS.values():
        assert localize_prompt(prompt, "600519") == prompt
        assert localize_prompt(prompt, "00700") == prompt


def test_evidence_market():
    assert evidence_market("AAPL") == "us"
    assert evidence_market("BRK.B") == "us"
    assert evidence_market("600519") == "cn"
    assert evidence_market("00700") is None


def _bars(n=80, last_volume=1_000.0, last_close=100.0, prev_close=94.0):
    dates = pd.bdate_range("2026-06-01", periods=n)
    close = np.full(n, 95.0)
    close[-2] = prev_close
    close[-1] = last_close
    volume = np.full(n, 100.0)
    volume[-1] = last_volume
    return pd.DataFrame({"date": dates, "close": close, "volume": volume})


def test_us_surge_flag_triggers_on_skill4_conditions():
    flag = compute_us_surge_flag(_bars())
    assert flag["flag"] is True
    assert round(flag["rv"], 1) == 10.0
    assert "高位放量异动" in render_us_surge_prompt(flag)


def test_us_surge_flag_needs_all_three_conditions():
    assert compute_us_surge_flag(_bars(last_volume=350.0))["flag"] is False  # RV 3.5
    assert compute_us_surge_flag(_bars(last_close=97.0, prev_close=94.0))["flag"] is False  # +3.2%
    low = _bars(last_close=60.0, prev_close=70.0)
    low.loc[10, "close"] = 200.0  # 60 日最高 200，收盘不到 80%
    assert compute_us_surge_flag(low)["flag"] is False
    assert compute_us_surge_flag(_bars(n=40)) is None
    assert render_us_surge_prompt(compute_us_surge_flag(_bars(last_volume=350.0))) == ""


def test_us_program_checklist_shows_only_us_tested_item():
    flag = compute_us_surge_flag(_bars())
    items = build_program_checklist(None, None, "zh", None, market="us", us_surge=flag)
    assert len(items) == 1 and items[0].startswith("⚠️") and "高位放量异动" in items[0]
    assert program_flag_states(None, None, None, market="us", us_surge=flag) == {"surge": True}
    unknown = build_program_checklist(None, None, "zh", None, market="us", us_surge=None)
    assert len(unknown) == 1 and unknown[0].startswith("❓")
    # A 股不变：四项
    assert len(build_program_checklist(None, None, "zh", None)) == 4


def test_tested_signals_use_market_rules():
    assert tested_notes("高管增持彰显信心", "cn") == ["股东 / 高管增持之后不跑赢"]
    assert tested_notes("高管增持彰显信心", "us") == []  # 美股内部人买入之后跑赢，不能标成无效
    assert tested_notes("MACD 金叉确认", "us") == ["MACD 金叉 / 底背离在美股没有优势"]
    assert tested_notes("估值偏低，具备安全边际", "cn") == []  # A 股估值因子成立
    assert tested_notes("估值偏低，具备安全边际", "us") == ["美股估值最便宜一组不跑赢"]
    assert tested_notes("RSI 超卖", "us") == []
    assert "形态与突破在美股没有方向信息" in annotate("放量突破前高", "us")


def test_yahoo_share_class_symbol():
    from data_provider.yfinance_fetcher import YfinanceFetcher

    assert YfinanceFetcher()._convert_stock_code("BRK.B") == "BRK-B"
    assert YfinanceFetcher()._convert_stock_code("AAPL") == "AAPL"


def test_amount_unit_follows_market():
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    assert analyzer._format_amount(2.858e9, "JPM") == "28.58 亿美元"
    assert analyzer._format_amount(2.858e9, "600519") == "28.58 亿元"
    assert analyzer._format_amount(2.858e9) == "28.58 亿元"
