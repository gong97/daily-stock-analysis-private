# -*- coding: utf-8 -*-
"""大盘估值背景（乐咕乐股 legulegu），供大盘复盘使用。

2026-09-30 在 GitHub Actions 上实测 7 个接口全部可达（东方财富在 Actions 上被封）。都是全市场/指数级
数据；这个 akshare 版本已没有单只股票的估值历史接口，所以不用于个股。

每个指标给出最新值和近 10 年历史分位。分位方向不同，渲染时写明：
PE、PB、总市值/GDP、拥挤度越高越贵（越热）；股债利差、股息率越高越便宜。
每个接口单独限时，取不到的指标直接省略，不影响其余指标和大盘复盘。
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

LOOKBACK_YEARS = 10
FETCH_TIMEOUT_SECONDS = 12
# 最新数据距今超过这么多天就不输出：大盘拥挤度 2026-09-30 取到的最新值停在 07-21，
# 拿两个多月前的「99% 分位」当今天的背景会误导
MAX_STALE_DAYS = 14


@dataclass(frozen=True)
class _Indicator:
    key: str
    label: str
    fetch: Callable[[], pd.DataFrame]
    date_col: str
    value_col: str
    higher_is_expensive: bool
    unit: str = ""
    transform: Optional[Callable[[pd.DataFrame], pd.Series]] = None


def _indicators() -> List[_Indicator]:
    import akshare as ak

    return [
        _Indicator("sh_pe", "上证平均市盈率", lambda: ak.stock_market_pe_lg(symbol="上证"),
                   "日期", "平均市盈率", True),
        _Indicator("sh_pb", "上证市净率", lambda: ak.stock_market_pb_lg(symbol="上证"),
                   "日期", "市净率", True),
        _Indicator("hs300_pe_ttm", "沪深300滚动市盈率", lambda: ak.stock_index_pe_lg(symbol="沪深300"),
                   "日期", "滚动市盈率", True),
        _Indicator("buffett", "总市值/GDP", lambda: ak.stock_buffett_index_lg(),
                   "日期", "总市值", True, unit="%",
                   transform=lambda df: pd.to_numeric(df["总市值"], errors="coerce")
                   / pd.to_numeric(df["GDP"], errors="coerce") * 100),
        # 拥挤度、股债利差原始值是小数（0.51、0.06），换成百分数显示
        _Indicator("congestion", "大盘拥挤度", lambda: ak.stock_a_congestion_lg(),
                   "date", "congestion", True, unit="%",
                   transform=lambda df: pd.to_numeric(df["congestion"], errors="coerce") * 100),
        _Indicator("equity_bond_spread", "股债利差（沪深300）", lambda: ak.stock_ebs_lg(),
                   "日期", "股债利差", False, unit="%",
                   transform=lambda df: pd.to_numeric(df["股债利差"], errors="coerce") * 100),
        _Indicator("dividend_yield", "上证A股股息率", lambda: ak.stock_a_gxl_lg(symbol="上证A股"),
                   "日期", "股息率", False, unit="%"),
    ]


def _fetch_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    box: Dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - 记录后跳过该指标
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"timeout after {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def summarize_series(dates: pd.Series, values: pd.Series, *, lookback_years: int = LOOKBACK_YEARS) -> Optional[Dict[str, Any]]:
    """最新值、日期及其在近 N 年里的分位（0~100，最新值不高于它的样本占比）。"""
    s = pd.DataFrame({"date": pd.to_datetime(dates, errors="coerce"),
                      "value": pd.to_numeric(values, errors="coerce")}).dropna()
    if s.empty:
        return None
    s = s.sort_values("date")
    latest_date = s["date"].iloc[-1]
    window = s[s["date"] >= latest_date - pd.DateOffset(years=lookback_years)]
    latest = float(window["value"].iloc[-1])
    if not math.isfinite(latest) or len(window) < 10:
        return None
    percentile = float((window["value"] <= latest).mean() * 100)
    return {
        "value": latest,
        "date": latest_date.strftime("%Y-%m-%d"),
        "percentile": percentile,
        "samples": int(len(window)),
    }


def fetch_market_valuation(
    timeout: float = FETCH_TIMEOUT_SECONDS,
    *,
    today: Optional[pd.Timestamp] = None,
) -> Dict[str, Any]:
    """{"indicators": [...], "errors": [...]}；每个指标单独限时，取不到的省略并记录原始错误。"""
    indicators: List[Dict[str, Any]] = []
    errors: List[str] = []
    try:
        specs = _indicators()
    except Exception as exc:  # akshare 缺失等
        return {"indicators": [], "errors": [f"import_akshare:{type(exc).__name__}"]}

    for spec in specs:
        try:
            df = _fetch_with_timeout(spec.fetch, timeout)
            if df is None or df.empty or spec.date_col not in df.columns:
                errors.append(f"{spec.key}:empty")
                continue
            values = spec.transform(df) if spec.transform else df[spec.value_col]
            summary = summarize_series(df[spec.date_col], values)
            if summary is None:
                errors.append(f"{spec.key}:insufficient")
                continue
            now = (today if today is not None else pd.Timestamp.now()).normalize()
            if (now - pd.Timestamp(summary["date"])).days > MAX_STALE_DAYS:
                errors.append(f"{spec.key}:stale({summary['date']})")
                continue
            indicators.append({
                "key": spec.key,
                "label": spec.label,
                "unit": spec.unit,
                "higher_is_expensive": spec.higher_is_expensive,
                **summary,
            })
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{spec.key}:{type(exc).__name__}:{str(exc)[:80]}")
    if errors:
        logger.warning("[大盘估值] 部分指标取数失败: %s", errors)
    return {"indicators": indicators, "errors": errors, "lookback_years": LOOKBACK_YEARS}


def render_market_valuation_block(context: Optional[Dict[str, Any]]) -> str:
    """大盘复盘提示词里的「市场估值背景」段落；没有任何指标时返回空串。"""
    items = (context or {}).get("indicators") or []
    if not items:
        return ""
    years = (context or {}).get("lookback_years", LOOKBACK_YEARS)
    lines = [f"## 市场估值背景（乐咕乐股，近{years}年历史分位）"]
    for item in items:
        direction = "越高越贵" if item["higher_is_expensive"] else "越高越便宜"
        unit = item.get("unit") or ""
        lines.append(
            f"- {item['label']}：{item['value']:.2f}{unit}（{item['date']}），"
            f"处于近{years}年 {item['percentile']:.0f}% 分位（{direction}）"
        )
    lines.append(
        "> 估值分位描述的是中长期性价比，对短期涨跌没有择时能力；只作背景，不要据此给出当日买卖结论。"
    )
    return "\n".join(lines)
