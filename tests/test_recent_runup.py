# -*- coding: utf-8 -*-
"""近 5 日涨幅居前与预期波动区间（src/recent_runup.py）。"""

import json

import pandas as pd
import pytest

from src import recent_runup as rr


def _bars(closes, highs=None, lows=None):
    dates = pd.bdate_range("2026-09-01", periods=len(closes))
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    return pd.DataFrame({"date": dates, "close": closes, "high": highs, "low": lows})


@pytest.fixture(autouse=True)
def _reset():
    rr.reset_cache()
    yield
    rr.reset_cache()


def test_runup_flag_uses_excess_over_index_against_cutoff():
    bars = _bars([10.0] * 20 + [10.0, 10.2, 10.5, 10.8, 11.2, 11.5])
    idx = pd.Series([100.0, 100.5, 101.0, 101.5, 102.0, 102.0], index=bars["date"].iloc[-6:].to_numpy())
    cutoff = {"cutoff": 0.10, "top": 0.14, "date": "2026-10-10"}
    out = rr.compute_recent_runup(bars, cutoff, idx)
    assert out["stock_ret"] == pytest.approx(0.15)
    assert out["index_ret"] == pytest.approx(0.02)
    assert out["excess"] == pytest.approx(0.13)
    assert out["flag"] is True and out["top"] is False


def test_runup_unknown_when_index_date_mismatches():
    bars = _bars([10.0] * 26)
    idx = pd.Series([100.0] * 6, index=pd.bdate_range("2026-08-01", periods=6))
    assert rr.compute_recent_runup(bars, {"cutoff": 0.1, "top": 0.14, "date": "x"}, idx) is None


def test_cutoff_from_local_file_checks_quantile_and_age(tmp_path, monkeypatch):
    path = tmp_path / "market_cutoff.json"
    path.write_text(json.dumps({"runup": {"quantile": 0.9, "latest": {"date": "2026-10-10", "cutoff": 0.08,
                                                                        "cutoff_top": 0.12}}}), encoding="utf-8")
    monkeypatch.setenv(rr.ENV_FILE, str(path))
    from datetime import date
    assert rr.load_runup_cutoff(today=date(2026, 10, 16)) == {"cutoff": 0.08, "top": 0.12, "date": "2026-10-10"}
    rr.reset_cache()
    assert rr.load_runup_cutoff(today=date(2026, 11, 30)) is None      # 超过 14 天


def test_expected_range_is_close_plus_minus_atr():
    closes = [10.0] * 25
    bars = _bars(closes, highs=[10.5] * 25, lows=[9.5] * 25)
    out = rr.compute_expected_range(bars)
    assert out["atr"] == pytest.approx(1.0)
    assert (out["low"], out["high"]) == (pytest.approx(9.0), pytest.approx(11.0))
    assert out["atr_pct"] == pytest.approx(0.1)
    assert out["method"] == "atr20"


def test_expected_range_har_averages_5_20_60_day_atr():
    # 前 41 天波幅 2，后 20 天波幅 1，最后 5 天波幅 0.5：ATR5 0.5、ATR20 (15*1+5*0.5)/20、ATR60 含 40 天的 2
    widths = [2.0] * 41 + [1.0] * 15 + [0.5] * 5
    closes = [10.0] * len(widths)
    bars = _bars(closes, highs=[10 + w / 2 for w in widths], lows=[10 - w / 2 for w in widths])
    out = rr.compute_expected_range(bars, method="har")
    atr5, atr20, atr60 = 0.5, (15 * 1.0 + 5 * 0.5) / 20, (40 * 2.0 + 15 * 1.0 + 5 * 0.5) / 60
    assert out["method"] == "har"
    assert out["atr"] == pytest.approx((atr5 + atr20 + atr60) / 3)
    assert out["atr20"] == pytest.approx(atr20)
    assert out["high"] == pytest.approx(10 + out["atr"])


def test_expected_range_har_falls_back_to_atr20_when_history_short():
    bars = _bars([10.0] * 40, highs=[10.5] * 40, lows=[9.5] * 40)
    out = rr.compute_expected_range(bars, method="har")
    assert out["method"] == "atr20" and out["atr"] == pytest.approx(1.0)
