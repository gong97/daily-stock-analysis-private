# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _infer_ak_market(stock_code: Any) -> Optional[str]:
    """
    Infer AkShare's ``market`` argument ('sh'/'sz'/'bj') from an A-share code.

    ``normalize_stock_code()`` strips exchange prefixes/suffixes before the code
    reaches this adapter, so the exchange has to be recovered from the numeric
    prefix. AkShare defaults to ``market='sh'``; without this the Shenzhen and
    Beijing codes silently query the wrong exchange instead of failing loudly.

    Returns None for anything that is not a 6-digit A-share code.
    """
    code = _normalize_code(stock_code)
    if not re.fullmatch(r"\d{6}", code):
        return None
    # 60/68 -> SSE main board & STAR; 000/001/002/003/30 -> SZSE; 43/83/87/92 -> BSE.
    if code.startswith(("60", "68")):
        return "sh"
    if code.startswith(("000", "001", "002", "003", "30")):
        return "sz"
    if code.startswith(("43", "83", "87", "92")):
        return "bj"
    return None


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


_REPORT_PERIOD_COL = re.compile(r"^\d{8}$")

# 新浪「财务摘要」宽表的指标名 → 结构化字段。同名指标会在「常用指标」「成长能力」等分组里重复出现，
# 数值相同，取第一次出现的即可。金额单位是元，比例是百分数（23.13 = 23.13%），与邮件渲染约定一致。
_ABSTRACT_FIELDS: Dict[str, str] = {
    "revenue": "营业总收入",
    "net_profit_parent": "归母净利润",
    "operating_cash_flow": "经营现金流量净额",
    "roe": "净资产收益率(ROE)",
    "gross_margin": "毛利率",
    "revenue_yoy": "营业总收入增长率",
    "net_profit_yoy": "归属母公司净利润增长率",
}


def _finite_float(value: Any) -> Optional[float]:
    """_safe_float 会把 NaN 原样返回；宽表的空单元格就是 NaN，这里一并当作缺失。"""
    f = _safe_float(value)
    return f if f is not None and math.isfinite(f) else None


def _parse_financial_abstract_wide(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """解析 ak.stock_financial_abstract 的宽表：行是指标（「指标」列），列是报告期（YYYYMMDD）。

    旧逻辑把它当成「每行一只股票、每列一个指标」来读，取第一行再按列名找关键词，
    列名全是日期，所以所有字段都落空且不报错（2026-09-29 查明，此前应从未取到过）。
    非宽表返回 None，由调用方沿用逐行解析。
    """
    if df is None or df.empty or "指标" not in df.columns:
        return None
    periods = sorted((c for c in df.columns if _REPORT_PERIOD_COL.match(str(c))), reverse=True)
    if not periods:
        return None

    table = df.drop_duplicates("指标").set_index("指标")
    # 最近报告期 = 核心字段有数的最新一列（个别股票最新一列可能整列为空）
    anchors = [name for name in ("营业总收入", "归母净利润") if name in table.index]
    latest = next(
        (p for p in periods if any(_finite_float(table.at[name, p]) is not None for name in anchors)),
        None,
    )
    if latest is None:
        return None

    values = {
        field: _finite_float(table.at[name, latest]) if name in table.index else None
        for field, name in _ABSTRACT_FIELDS.items()
    }
    return {"report_date": _normalize_report_date(latest), **values}


# ---------------------------------------------------------------------------
# 同花顺（10jqka）：分红、机构盈利预测、股东/高管增减持
# 2026-09-30 在 GitHub Actions 上实测可达（东方财富在 Actions 上被封）。列名按真实返回逐字取，
# 不走 _pick_by_keywords：同花顺分红表里「股东大会预案公告日期」排在「分红方案说明」前面，
# 关键词「预案」会先命中那一列日期，通用解析因此一条都取不到。
# ---------------------------------------------------------------------------


def _build_dividend_payload_ths(
    df: pd.DataFrame,
    *,
    now_date: Optional[Any] = None,
    max_events: int = 5,
) -> Dict[str, Any]:
    """ak.stock_fhps_detail_ths → 与 _build_dividend_payload 相同结构的分红摘要。

    只算已除息的现金分红（除权除息日不晚于今天）；「不分配不转增」和未实施的预案跳过。
    """
    if df is None or df.empty or "分红方案说明" not in df.columns:
        return {}
    today = now_date or datetime.now().date()
    ttm_start = today - timedelta(days=365)

    events: List[Dict[str, Any]] = []
    seen = set()
    for _, row in df.iterrows():
        per_share = _parse_dividend_plan_to_per_share(row.get("分红方案说明"))
        if per_share is None or per_share <= 0:
            continue
        ex_dt = _safe_datetime(row.get("A股除权除息日"))
        if ex_dt is None or ex_dt.date() > today:
            continue
        record_dt = _safe_datetime(row.get("A股股权登记日"))
        announce_dt = _safe_datetime(row.get("实施公告日"))
        key = (ex_dt.date().isoformat(), round(per_share, 6))
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "event_date": ex_dt.date().isoformat(),
            "ex_dividend_date": ex_dt.date().isoformat(),
            "record_date": record_dt.date().isoformat() if record_dt else None,
            "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
            "cash_dividend_per_share": round(per_share, 6),
            "is_pre_tax": True,
        })
    if not events:
        return {}

    events.sort(key=lambda item: item["event_date"], reverse=True)
    ttm = [e for e in events if ttm_start <= datetime.fromisoformat(e["event_date"]).date() <= today]
    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm),
        "ttm_cash_dividend_per_share": round(sum(e["cash_dividend_per_share"] for e in ttm), 6) if ttm else None,
        "coverage": "cash_dividend_pre_tax",
        "as_of": today.isoformat(),
        "source": "stock_fhps_detail_ths",
    }


def _parse_profit_forecast_ths(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """ak.stock_profit_forecast_ths(indicator="预测年报每股收益") → 按年度的每股收益一致预期。"""
    if df is None or df.empty or "年度" not in df.columns:
        return []
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        year = _finite_float(row.get("年度"))
        mean = _finite_float(row.get("均值"))
        if year is None or mean is None:
            continue
        institutions = _finite_float(row.get("预测机构数"))
        rows.append({
            "year": int(year),
            "institutions": int(institutions) if institutions is not None else None,
            "eps_min": _finite_float(row.get("最小值")),
            "eps_mean": mean,
            "eps_max": _finite_float(row.get("最大值")),
            "industry_avg": _finite_float(row.get("行业平均数")),
        })
    return sorted(rows, key=lambda r: r["year"])


_SHARE_CHANGE_RE = re.compile(r"^(增持|减持)\s*([0-9.]+)\s*(亿|万)?")


def _parse_share_change(text: Any) -> Optional[float]:
    """「减持1062.50万」→ -10625000.0；「增持1.66亿」→ 166000000.0；认不出返回 None。"""
    m = _SHARE_CHANGE_RE.match(_safe_str(text))
    if not m:
        return None
    qty = float(m.group(2)) * {"亿": 1e8, "万": 1e4}.get(m.group(3) or "", 1.0)
    return -qty if m.group(1) == "减持" else qty


def _summarize_share_changes(
    df: Optional[pd.DataFrame],
    *,
    date_col: str,
    who_col: str,
    via_col: str,
    now_date: Any,
    lookback_days: int,
    max_events: int = 5,
) -> Dict[str, Any]:
    """增持、减持分开计。不给「净变动」：询价转让时大股东减持、一批机构按同价接盘，
    净额恰好为 0，会把真正的风险信号（谁在卖、卖了多少）抹掉（宁德时代 2025-11-24 即如此）。
    明细里减持排在前面。
    """
    summary: Dict[str, Any] = {
        "reduce_count": 0, "reduce_shares": 0.0, "increase_count": 0, "increase_shares": 0.0, "events": [],
    }
    if df is None or df.empty or date_col not in df.columns or "变动数量" not in df.columns:
        return summary
    start = now_date - timedelta(days=lookback_days)
    events: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        dt = _safe_datetime(row.get(date_col))
        shares = _parse_share_change(row.get("变动数量"))
        if dt is None or shares is None or not (start <= dt.date() <= now_date):
            continue
        events.append({
            "date": dt.date().isoformat(),
            "who": _safe_str(row.get(who_col)),
            "change": _safe_str(row.get("变动数量")),
            "shares": shares,
            "avg_price": _finite_float(row.get("交易均价")),
            "via": _safe_str(row.get(via_col)),
        })
    reduces = sorted((e for e in events if e["shares"] < 0), key=lambda e: e["date"], reverse=True)
    increases = sorted((e for e in events if e["shares"] > 0), key=lambda e: e["date"], reverse=True)
    summary.update({
        "reduce_count": len(reduces),
        "reduce_shares": -sum(e["shares"] for e in reduces),
        "increase_count": len(increases),
        "increase_shares": sum(e["shares"] for e in increases),
        "events": (reduces + increases)[:max_events],
    })
    return summary


def _summarize_holder_changes_ths(
    holder_df: Optional[pd.DataFrame],
    mgmt_df: Optional[pd.DataFrame],
    *,
    now_date: Optional[Any] = None,
    lookback_days: int = 180,
) -> Dict[str, Any]:
    """股东（stock_shareholder_change_ths）与高管（stock_management_change_ths）近 N 天增减持摘要。"""
    today = now_date or datetime.now().date()
    return {
        "lookback_days": lookback_days,
        "as_of": today.isoformat(),
        "shareholder": _summarize_share_changes(
            holder_df, date_col="公告日期", who_col="变动股东", via_col="变动途径",
            now_date=today, lookback_days=lookback_days,
        ),
        "management": _summarize_share_changes(
            mgmt_df, date_col="变动日期", who_col="变动人", via_col="股份变动途径",
            now_date=today, lookback_days=lookback_days,
        ),
    }


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                errors.append(f"{func_name}:{type(exc).__name__}")
                continue
        return None, None, errors

    def get_financial_summary(self, stock_code: str) -> Dict[str, Any]:
        """只取财务摘要（营收、利润、现金流、ROE、同比），供调用方单独限时。

        get_fundamental_bundle 里还有业绩预告、分红、十大股东等多个接口，整包常常超过单次超时
        （2026-09-29 本机实测 13~14 秒 vs 超时 8 秒），超时后连 1.5 秒就能拿到的财报也一起丢掉。
        """
        result: Dict[str, Any] = {"growth": {}, "earnings": {}, "source_chain": [], "errors": []}
        self._fill_financial_summary(result, stock_code)
        return result

    def get_dividend_ths(self, stock_code: str) -> Dict[str, Any]:
        """同花顺分红 → {"dividend": {...}, "source_chain": [...], "errors": [...]}。"""
        df, source, errors = self._call_df_candidates([("stock_fhps_detail_ths", {"symbol": stock_code})])
        payload = _build_dividend_payload_ths(df) if df is not None else {}
        return {
            "dividend": payload,
            "source_chain": [f"dividend:{source}"] if payload else [],
            "errors": errors if df is not None or errors else ["stock_fhps_detail_ths:empty"],
        }

    def get_profit_forecast_ths(self, stock_code: str) -> Dict[str, Any]:
        """同花顺机构盈利预测（每股收益一致预期）。"""
        df, source, errors = self._call_df_candidates([
            ("stock_profit_forecast_ths", {"symbol": stock_code, "indicator": "预测年报每股收益"}),
        ])
        rows = _parse_profit_forecast_ths(df) if df is not None else []
        return {
            "rows": rows,
            "source_chain": [f"profit_forecast:{source}"] if rows else [],
            "errors": errors if df is not None or errors else ["stock_profit_forecast_ths:empty"],
        }

    def get_holder_changes_ths(self, stock_code: str, lookback_days: int = 180) -> Dict[str, Any]:
        """同花顺股东与高管增减持，近 lookback_days 天。任一张表取不到时另一张照常汇总。"""
        holder_df, holder_src, holder_err = self._call_df_candidates(
            [("stock_shareholder_change_ths", {"symbol": stock_code})]
        )
        mgmt_df, mgmt_src, mgmt_err = self._call_df_candidates(
            [("stock_management_change_ths", {"symbol": stock_code})]
        )
        summary = _summarize_holder_changes_ths(holder_df, mgmt_df, lookback_days=lookback_days)
        chain = [f"holder_changes:{s}" for s in (holder_src, mgmt_src) if s]
        return {"summary": summary if chain else {}, "source_chain": chain, "errors": holder_err + mgmt_err}

    def _fill_financial_summary(self, result: Dict[str, Any], stock_code: str) -> None:
        fin_df, fin_source, fin_errors = self._call_df_candidates([
            ("stock_financial_abstract", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {}),
        ])
        result["errors"].extend(fin_errors)
        wide = _parse_financial_abstract_wide(fin_df) if fin_df is not None else None
        if wide is not None:
            result["growth"] = {
                "revenue_yoy": wide["revenue_yoy"],
                "net_profit_yoy": wide["net_profit_yoy"],
                "roe": wide["roe"],
                "gross_margin": wide["gross_margin"],
            }
            financial_report_payload = {
                "report_date": wide["report_date"],
                "revenue": wide["revenue"],
                "net_profit_parent": wide["net_profit_parent"],
                "operating_cash_flow": wide["operating_cash_flow"],
                "roe": wide["roe"],
            }
            if any(v is not None for v in financial_report_payload.values()):
                result["earnings"]["financial_report"] = financial_report_payload
            result["source_chain"].append(f"growth:{fin_source}")
        elif fin_df is not None:
            row = _extract_latest_row(fin_df, stock_code)
            if row is not None:
                revenue_yoy = _safe_float(_pick_by_keywords(row, ["营业收入同比", "营收同比", "收入同比", "同比增长"]))
                profit_yoy = _safe_float(_pick_by_keywords(row, ["净利润同比", "净利同比", "归母净利润同比"]))
                roe = _safe_float(_pick_by_keywords(row, ["净资产收益率", "ROE", "净资产收益"]))
                gross_margin = _safe_float(_pick_by_keywords(row, ["毛利率"]))
                report_date = _normalize_report_date(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["report_date"]))
                revenue = _safe_float(_pick_by_keywords(row, ["营业总收入", "营业收入", "营收"]))
                net_profit_parent = _safe_float(_pick_by_keywords(row, ["归母净利润", "母公司股东净利润", "净利润"]))
                operating_cash_flow = _safe_float(
                    _pick_by_keywords(row, ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流"])
                )
                result["growth"] = {
                    "revenue_yoy": revenue_yoy,
                    "net_profit_yoy": profit_yoy,
                    "roe": roe,
                    "gross_margin": gross_margin,
                }
                financial_report_payload = {
                    "report_date": report_date,
                    "revenue": revenue,
                    "net_profit_parent": net_profit_parent,
                    "operating_cash_flow": operating_cash_flow,
                    "roe": roe,
                }
                if any(v is not None for v in financial_report_payload.values()):
                    result["earnings"]["financial_report"] = financial_report_payload
                result["source_chain"].append(f"growth:{fin_source}")

    def get_fundamental_bundle(self, stock_code: str, include_financials: bool = True) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.

        include_financials=False 时跳过财务摘要——调用方已用 get_financial_summary 单独取过。
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # Financial indicators
        if include_financials:
            self._fill_financial_summary(result, stock_code)

        # Earnings forecast
        forecast_df, forecast_source, forecast_errors = self._call_df_candidates([
            ("stock_yjyg_em", {"symbol": stock_code}),
            ("stock_yjyg_em", {}),
            ("stock_yjbb_em", {"symbol": stock_code}),
            ("stock_yjbb_em", {}),
        ])
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            row = _extract_latest_row(forecast_df, stock_code)
            if row is not None:
                result["earnings"]["forecast_summary"] = _safe_str(
                    _pick_by_keywords(row, ["预告", "业绩变动", "内容", "摘要", "公告"])
                )[:200]
                result["source_chain"].append(f"earnings_forecast:{forecast_source}")

        # Earnings quick report
        quick_df, quick_source, quick_errors = self._call_df_candidates([
            ("stock_yjkb_em", {"symbol": stock_code}),
            ("stock_yjkb_em", {}),
        ])
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, stock_code)
            if row is not None:
                result["earnings"]["quick_report_summary"] = _safe_str(
                    _pick_by_keywords(row, ["快报", "摘要", "公告", "说明"])
                )[:200]
                result["source_chain"].append(f"earnings_quick:{quick_source}")

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = self._call_df_candidates([
            ("stock_fhps_detail_em", {"symbol": stock_code}),
            ("stock_history_dividend_detail", {"symbol": stock_code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": stock_code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(dividend_df, stock_code, max_events=5)
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        # Institution / top shareholders
        inst_df, inst_source, inst_errors = self._call_df_candidates([
            ("stock_institute_hold", {}),
            ("stock_institute_recommend", {}),
        ])
        result["errors"].extend(inst_errors)
        if inst_df is not None:
            row = _extract_latest_row(inst_df, stock_code)
            if row is not None:
                inst_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "变动", "持股变化"]))
                result["institution"]["institution_holding_change"] = inst_change
                result["source_chain"].append(f"institution:{inst_source}")

        top10_df, top10_source, top10_errors = self._call_df_candidates([
            ("stock_gdfx_top_10_em", {"symbol": stock_code}),
            ("stock_gdfx_top_10_em", {}),
            ("stock_zh_a_gdhs_detail_em", {"symbol": stock_code}),
            ("stock_zh_a_gdhs_detail_em", {}),
        ])
        result["errors"].extend(top10_errors)
        if top10_df is not None:
            row = _extract_latest_row(top10_df, stock_code)
            if row is not None:
                holder_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "持股变化", "变动"]))
                result["institution"]["top10_holder_change"] = holder_change
                result["source_chain"].append(f"top10:{top10_source}")

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        """
        Return stock + sector capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        # AkShare signature is stock_individual_fund_flow(stock, market='sh').
        # `market` is required for correctness, not just availability: omitting it
        # makes every SZ/BJ code silently return Shanghai data. Candidates that
        # take no code at all are deliberately absent -- they return AkShare's
        # built-in default stock, i.e. another company's flow.
        market = _infer_ak_market(stock_code)
        if market is None:
            result["errors"].append(f"unsupported_code:{_safe_str(stock_code)}")
            stock_df, stock_source, stock_errors = None, None, []
        else:
            stock_df, stock_source, stock_errors = self._call_df_candidates([
                ("stock_individual_fund_flow", {"stock": _normalize_code(stock_code), "market": market}),
            ])
        result["errors"].extend(stock_errors)
        if stock_df is not None:
            row = _extract_latest_row(stock_df, stock_code)
            if row is not None:
                net_inflow = _safe_float(_pick_by_keywords(row, ["主力净流入", "净流入", "净额"]))
                inflow_5d = _safe_float(_pick_by_keywords(row, ["5日", "五日"]))
                inflow_10d = _safe_float(_pick_by_keywords(row, ["10日", "十日"]))
                result["stock_flow"] = {
                    "main_net_inflow": net_inflow,
                    "inflow_5d": inflow_5d,
                    "inflow_10d": inflow_10d,
                }
                result["source_chain"].append(f"capital_stock:{stock_source}")

        # stock_sector_fund_flow_rank(indicator, sector_type) -- pass both
        # explicitly so the ranking window and board type are pinned rather than
        # inherited from AkShare's defaults.
        sector_df, sector_source, sector_errors = self._call_df_candidates([
            ("stock_sector_fund_flow_rank", {"indicator": "今日", "sector_type": "行业资金流"}),
        ])
        result["errors"].extend(sector_errors)
        if sector_df is not None:
            name_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("板块", "行业", "名称", "name"))), None)
            flow_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("净流入", "主力", "flow", "净额"))), None)
            if name_col and flow_col:
                work_df = sector_df[[name_col, flow_col]].copy()
                work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
                work_df = work_df.dropna(subset=[flow_col])
                top_df = work_df.nlargest(top_n, flow_col)
                bottom_df = work_df.nsmallest(top_n, flow_col)
                result["sector_rankings"] = {
                    "top": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in top_df.iterrows()],
                    "bottom": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in bottom_df.iterrows()],
                }
                result["source_chain"].append(f"capital_sector:{sector_source}")

        has_content = bool(result["stock_flow"] or result["sector_rankings"]["top"] or result["sector_rankings"]["bottom"])
        if has_content:
            result["status"] = "partial"
        elif result["errors"]:
            # Every candidate raised (import error, network block, bad params).
            # This is a failure, not an unsupported instrument -- reporting it as
            # "not_supported" is what kept this outage invisible in the report.
            result["status"] = "failed"
        else:
            result["status"] = "not_supported"
        return result

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
