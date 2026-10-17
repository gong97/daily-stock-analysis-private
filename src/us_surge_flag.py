# -*- coding: utf-8 -*-
"""美股的【程序】风险提示：高位放量异动（US-stock-macd-analyzer 的技巧 4「极度危险」）。

A 股的过热旗标（src/overheat_flags.py）在美股复现不成立（overheat-us-a / overheat-us-b），
所以美股不显示它们；这一条是美股自己检验成立的风险信号。

口径（与 US-stock-macd-analyzer signal_detector.skill_4_high_zone_surge_risk 逐字一致，不在这里调参）
----------------------------------------------------------------------------------------------
    相对成交量 RV = 当日成交量 / 前 20 个交易日成交量均值（不含当天）> 4
    且 |当日涨跌幅| > 5%
    且 收盘 > 近 60 日（含当天）最高收盘 × 0.8
检验：skill4-danger-us，标普 500，2016~2026，2329 次，10 日同日百分位 t -1.94（门槛 1.71），63% 的季度跑输。
样本是今天的标普 500 成分股（事后选股），按时点选股的重测已登记（pit-universe-retest）。

只提示、不调分。旗标描述「最近一根完整日线」。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

RV_WINDOW = 20
RV_MIN = 4.0
PCT_MIN = 0.05
HIGH_WINDOW = 60
HIGH_RATIO = 0.8


def compute_us_surge_flag(bars: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """bars 至少含 date / close / volume。不足 61 根或数据缺失时返回 None（= 不知道）。"""
    if bars is None or len(bars) == 0 or not {"date", "close", "volume"} <= set(bars.columns):
        return None
    df = bars[["date", "close", "volume"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"]).drop_duplicates("date", keep="last").sort_values("date")
    df = df.reset_index(drop=True)
    if len(df) < HIGH_WINDOW + 1:
        return None

    c = df["close"].astype(float)
    v = df["volume"].astype(float)
    prior = v.iloc[-RV_WINDOW - 1:-1]
    if prior.isna().any() or len(prior) < RV_WINDOW:
        return None
    base = float(prior.mean())
    prev_close = float(c.iloc[-2])
    if base <= 0 or prev_close <= 0 or pd.isna(v.iloc[-1]):
        return None

    close = float(c.iloc[-1])
    rv = float(v.iloc[-1]) / base
    pct = close / prev_close - 1
    high60 = float(c.iloc[-HIGH_WINDOW:].max())
    flag = bool(rv > RV_MIN and abs(pct) > PCT_MIN and close > HIGH_RATIO * high60)
    return {
        "as_of": df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "rv": rv,
        "pct": pct,
        "close": close,
        "high60": high60,
        "flag": flag,
    }


def render_us_surge_prompt(flag: Optional[Dict[str, Any]]) -> str:
    """提示词段落：只在触发时写，未触发不占篇幅。"""
    if not isinstance(flag, dict) or not flag.get("flag"):
        return ""
    return (
        f"\n### 高位放量异动（程序按美股检验口径计算，截至 {flag.get('as_of')} 收盘）\n"
        f"- 相对成交量 {flag['rv']:.1f} 倍（> 4），当日涨跌 {flag['pct'] * 100:+.1f}%（绝对值 > 5%），"
        f"收盘 {flag['close']:.2f} 在近 60 日最高 {flag['high60']:.2f} 的 80% 以上。\n"
        "> 美股历史检验（标普 500，2016~2026）：这种情形之后 10 个交易日多数跑输同日其他股票。"
        "触发时不要给买入，持仓者把它作为风险提示之一。以上为程序计算的事实，请直接引用。\n"
    )
