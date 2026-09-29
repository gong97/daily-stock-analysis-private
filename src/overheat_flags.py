# -*- coding: utf-8 -*-
"""过热风险旗标：按 Macd-Qlib-Analyzer 已检验成立的口径，由程序算好再交给 LLM。

阈值必须与检验时逐字一致，不在这里调参——换阈值就是新假设，要回那边重新登记。

来源（Macd-Qlib-Analyzer 仓库）
------------------------------
A 高位乖离过大   scripts/turning_point_confirm.py   高位日 且 收盘 / MA20 − 1 > 0.1114
B 高位放量急拉   同上                               高位日 且 换手 / 自身前 120 日中位数 > 2.953
                                                   且 当日涨幅 > 3.3797%
   高位日 = 收盘 >= 近 60 日最高收盘 × 0.95（60 日窗口含当天）
   2023~2026 确认段：A t -6.24（13/14 季度跑输）、B t -6.60（14/14），持有 10 日
C 高位高换手     signal_detector.skill_3_high_turnover_risk
                 换手率 > 15%（流通市值 >= 100 亿时 > 10%，
                 scripts/large_cap_turnover_threshold.py：大盘档 t -5.17，24/26 季度为负）
                 且 当日涨幅 < 7% 且 收盘 > 近 60 日最高收盘 × 0.8

与检验口径的两处差异
--------------------
- 本项目的日线库不存换手率，B 的「换手 / 自身常态」用成交量之比代替。流通股本不变时两者
  严格相等；120 日内有送转、解禁等股本变动时会偏离。
- C 的当日换手率 = 最新一根日线的成交量 / 流通股本，流通股本 = 实时流通市值 / 实时价格。
  检验里流通市值 = 成交额 / 换手率，同一个量的两种算法。

旗标描述的是「最近一根完整日线」。CI 在收盘后运行，就是当天；盘中运行则是上一交易日。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

import pandas as pd

ZONE_WINDOW = 60
ZONE_TOL = 0.05
A_DIST_MA20 = 0.1114
B_HEAT = 2.953
B_PCT = 3.3797
HEAT_WINDOW = 120
HEAT_MIN_PERIODS = 60
C_TR_SMALL = 15.0
C_TR_LARGE = 10.0
C_LARGE_CAP_YI = 100.0
C_PCT_MAX = 7.0
C_HIGH_RATIO = 0.8

# 算全部旗标所需的最少日线：60 日高位窗口 + 至少 60 日的换手常态（不含当天）
MIN_BARS_FOR_HEAT = HEAT_MIN_PERIODS + 1
# 取数窗口：120 日常态 + 当天，外加长假余量；日历日
LOOKBACK_CALENDAR_DAYS = 200


def _finite(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def compute_overheat_flags(
    bars: pd.DataFrame,
    *,
    circ_mv: Any = None,
    price: Any = None,
) -> Optional[Dict[str, Any]]:
    """bars 至少含 date / close / volume，按日期升序或乱序均可。数据不足 60 根时返回 None。

    circ_mv 单位是元（与实时行情一致），price 是实时价格；两者用来换算当日换手率。
    """
    if bars is None or len(bars) == 0 or not {"date", "close", "volume"} <= set(bars.columns):
        return None
    df = bars[["date", "close", "volume"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"]).drop_duplicates("date", keep="last").sort_values("date")
    df = df.reset_index(drop=True)
    if len(df) < ZONE_WINDOW:
        return None

    c = df["close"].astype(float)
    v = df["volume"].astype(float)
    last = len(df) - 1

    close = float(c.iloc[last])
    high60 = float(c.iloc[-ZONE_WINDOW:].max())
    ma20 = float(c.iloc[-20:].mean())
    prev_close = float(c.iloc[last - 1])
    pct = (close / prev_close - 1) * 100 if prev_close else None
    dist_ma20 = close / ma20 - 1 if ma20 else None
    high_zone = close >= high60 * (1 - ZONE_TOL)

    heat = None
    prior = v.iloc[:last].iloc[-HEAT_WINDOW:]
    if prior.notna().sum() >= HEAT_MIN_PERIODS:
        base = float(prior.median())
        if base > 0:
            heat = float(v.iloc[last]) / base

    circ_mv_f = _finite(circ_mv)
    price_f = _finite(price)
    float_mcap_yi = circ_mv_f / 1e8 if circ_mv_f else None
    turnover_pct = None
    if circ_mv_f and price_f and price_f > 0:
        float_shares = circ_mv_f / price_f
        if float_shares > 0:
            turnover_pct = float(v.iloc[last]) / float_shares * 100

    flag_a = bool(high_zone and dist_ma20 is not None and dist_ma20 > A_DIST_MA20)
    flag_b = None
    if heat is not None and pct is not None:
        flag_b = bool(high_zone and heat > B_HEAT and pct > B_PCT)

    tr_threshold = None
    flag_c = None
    if turnover_pct is not None and float_mcap_yi is not None and pct is not None:
        tr_threshold = C_TR_LARGE if float_mcap_yi >= C_LARGE_CAP_YI else C_TR_SMALL
        flag_c = bool(
            turnover_pct > tr_threshold
            and pct < C_PCT_MAX
            and close > high60 * C_HIGH_RATIO
        )

    return {
        "as_of": df["date"].iloc[last].strftime("%Y-%m-%d"),
        "bars": len(df),
        "close": close,
        "high60": high60,
        "high_zone": bool(high_zone),
        "dist_ma20": dist_ma20,
        "pct": pct,
        "volume_heat": heat,
        "turnover_pct": turnover_pct,
        "float_mcap_yi": float_mcap_yi,
        "turnover_threshold": tr_threshold,
        "flag_a": flag_a,
        "flag_b": flag_b,
        "flag_c": flag_c,
    }


def _fmt(value: Any, spec: str, suffix: str = "") -> str:
    f = _finite(value)
    return f"{f:{spec}}{suffix}" if f is not None else "N/A"


def _mark(flag: Optional[bool]) -> str:
    if flag is None:
        return "❓ 数据不足"
    return "⚠️ 触发" if flag else "✅ 未触发"


def render_overheat_flags_prompt(flags: Optional[Dict[str, Any]]) -> str:
    """提示词里的旗标段落。flags 为空时返回空串，由调用方跳过。"""
    if not flags:
        return ""
    dist = flags.get("dist_ma20")
    tr_threshold = flags.get("turnover_threshold")
    tr_rule = f"> {tr_threshold:g}%" if tr_threshold is not None else "> 15%（流通市值 >= 100 亿时 > 10%）"
    return f"""
### 过热风险旗标（程序按历史检验口径计算，截至 {flags.get('as_of')} 收盘）
| 旗标 | 结果 | 关键数值 | 触发条件 |
|------|------|----------|----------|
| A 高位乖离过大 | {_mark(flags.get('flag_a'))} | 距 MA20 {_fmt(dist * 100 if dist is not None else None, '+.1f', '%')}，高位日={'是' if flags.get('high_zone') else '否'} | 高位日 且 距 MA20 > 11.14% |
| B 高位放量急拉 | {_mark(flags.get('flag_b'))} | 量能/前 120 日中位 {_fmt(flags.get('volume_heat'), '.2f', ' 倍')}，涨幅 {_fmt(flags.get('pct'), '+.2f', '%')} | 高位日 且 量能 > 2.95 倍 且 涨幅 > 3.38% |
| C 高位高换手 | {_mark(flags.get('flag_c'))} | 换手 {_fmt(flags.get('turnover_pct'), '.2f', '%')}，流通市值 {_fmt(flags.get('float_mcap_yi'), '.0f', ' 亿')} | 换手 {tr_rule} 且 涨幅 < 7% 且 收盘 > 60 日最高 × 0.8 |

> 高位日 = 收盘 >= 近 60 日最高收盘（{_fmt(flags.get('high60'), '.2f')}）× 0.95。以上为程序计算的事实，请直接引用，不要自行重算或改判。
"""
