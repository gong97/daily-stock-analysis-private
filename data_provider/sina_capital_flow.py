# -*- coding: utf-8 -*-
"""Sina direct capital-flow source for A-shares (token-free).

AkShare's flow endpoints go through ``push2*.eastmoney.com``, which is reliably
RemoteDisconnected from GitHub Actions.  The 2026-09-02 daily run logged seven
``[decision_stability] Downgraded buy because capital flow is unavailable:
empty_stock_flow`` lines and pushed five holdings onto the watchlist -- not
because the money was leaving, but because no source answered.  Tushare closes
that gap only for token holders (``TUSHARE_TOKEN`` is unset in that workflow),
so this endpoint sits between them: no token, and reachable from Actions, where
the daily K-line fallback already relies on Sina.

Per-stock flow only.  Sina's sector-ranking endpoints either reject the request
outright or return rows frozen in 2015, so sector rankings stay with the
Tushare / AkShare paths.

Field mapping: Sina's ``r0_net`` is 主力净流入 (super-large orders, in yuan),
which is the column the guardrail reads as "主力".  ``netamount`` is the
all-orders net and is deliberately unused -- mixing the two calibres across the
1/5/10-day fields would manufacture direction conflicts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from .fundamental_adapter import _infer_ak_market, _normalize_code, _safe_float

logger = logging.getLogger(__name__)

_ENDPOINT = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://vip.stock.finance.sina.com.cn/mkt/",
}
_HTTP_TIMEOUT_SECONDS = 6.0
# The whole capital_flow task runs on an 8s budget, so one request has to leave
# room for the AkShare fallback behind it.

_ROWS = 10
# Longest A-share holiday gap is Spring Festival / National Day at ~9 calendar
# days, so anything older than this is a dead series rather than a closed
# market.  Beijing (43/83/87/92) codes answer this endpoint with rows nearly a
# year old, which is exactly what this guard is here to reject.
_MAX_STALE_DAYS = 14


def _to_sina_symbol(stock_code: Any) -> Optional[str]:
    """``600900`` -> ``sh600900``; None for anything not an A-share code."""
    market = _infer_ak_market(stock_code)
    if market is None:
        return None
    return f"{market}{_normalize_code(stock_code)}"


def _sorted_rows(payload: Any) -> List[Dict[str, Any]]:
    """Newest-first rows, ignoring anything without a parsable date."""
    if not isinstance(payload, list):
        return []
    rows = [row for row in payload if isinstance(row, dict) and row.get("opendate")]
    return sorted(rows, key=lambda row: str(row.get("opendate")), reverse=True)


def _days_since(opendate: str) -> Optional[int]:
    try:
        parsed = datetime.strptime(str(opendate).strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return (datetime.now() - parsed).days


def _window_sum(rows: List[Dict[str, Any]], field: str, size: int) -> Optional[float]:
    """Sum the newest ``size`` rows, or None when the window is incomplete.

    A partial window would be reported under a field named ``inflow_5d``
    while holding three days of data, so it is dropped instead.
    """
    if len(rows) < size:
        return None
    values = [_safe_float(row.get(field)) for row in rows[:size]]
    if any(value is None for value in values):
        return None
    return float(sum(values))


def fetch_capital_flow(
    stock_code: str,
    *,
    timeout: float = _HTTP_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Return the shared capital-flow payload shape, fail-open.

    Same contract as ``AkshareFundamentalAdapter.get_capital_flow`` and
    ``TushareFetcher.get_capital_flow`` (status / stock_flow / sector_rankings /
    source_chain / errors) so the caller does not branch on the source.
    """
    result: Dict[str, Any] = {
        "status": "not_supported",
        "stock_flow": {},
        "sector_rankings": {"top": [], "bottom": []},
        "source_chain": [],
        "errors": [],
    }

    symbol = _to_sina_symbol(stock_code)
    if symbol is None:
        result["errors"].append(f"sina_unsupported_code:{_normalize_code(stock_code)}")
        return result

    try:
        response = requests.get(
            _ENDPOINT,
            params={
                "page": 1,
                "num": _ROWS,
                "sort": "opendate",
                "asc": 0,
                "daima": symbol,
            },
            headers=_HEADERS,
            timeout=timeout,
        )
        response.raise_for_status()
        # The endpoint serves GBK without declaring it; the payload itself is
        # ASCII, but decoding it as GBK keeps any Chinese error text readable.
        response.encoding = "gbk"
        payload = json.loads(response.text)
    except Exception as exc:  # noqa: BLE001 - fail-open, the caller falls back
        result["status"] = "failed"
        result["errors"].append(f"sina_moneyflow:{type(exc).__name__}")
        logger.warning("[CapitalFlow] Sina 取数失败 %s: %s", symbol, exc)
        return result

    rows = _sorted_rows(payload)
    if not rows:
        result["status"] = "failed"
        # Sina answers a bad symbol with {"__ERROR": 1} instead of an HTTP error.
        result["errors"].append(f"sina_moneyflow:empty_response:{symbol}")
        return result

    latest = rows[0]
    stale_days = _days_since(latest.get("opendate"))
    if stale_days is None or stale_days > _MAX_STALE_DAYS:
        result["status"] = "failed"
        result["errors"].append(f"sina_moneyflow:stale:{latest.get('opendate')}")
        logger.warning(
            "[CapitalFlow] Sina %s 最新资金流日期 %s 已过期，视为不可用",
            symbol,
            latest.get("opendate"),
        )
        return result

    main_net_inflow = _safe_float(latest.get("r0_net"))
    if main_net_inflow is None:
        result["status"] = "failed"
        result["errors"].append(f"sina_moneyflow:missing_r0_net:{symbol}")
        return result

    result["stock_flow"] = {
        "main_net_inflow": main_net_inflow,
        "inflow_5d": _window_sum(rows, "r0_net", 5),
        "inflow_10d": _window_sum(rows, "r0_net", 10),
    }
    result["source_chain"].append("capital_stock:sina_moneyflow")
    result["status"] = "partial"
    return result
