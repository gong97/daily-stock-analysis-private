# -*- coding: utf-8 -*-
"""美股【程序】项：近期内部人公开市场买入（弱参考，正面信号）。

为什么
------
A 股内部人增持之后不跑赢（insider-buying），美股方向相反：Form 4 公开市场买入之后 10 个交易日跑赢
（US-stock-macd-analyzer insider-buying-us-holder t 3.98、insider-buying-us-mgmt t 3.34，标普 500 管理层
买入 t 2.52；卖出没有信息，所以不列）。只提示、不调分；前瞻复核 2027-04（insider-buying-us-forward）。

数据来源
--------
不在本仓库（公开）抓 SEC：SEC 要求请求头带联系邮箱，而且 US-stock-macd-analyzer 的美股日报每天已经抓过。
那边 daily-report.yml 把同一份名单（标普 500、近 10 个 EDGAR 工作日的申报）上传到 release dsa-feeds 的
insider_buys.json；这里用只读 token 下载（与 src/valuation_red_flags.py 共用 VALUATION_RED_FLAGS_TOKEN，
fine-grained PAT 要加上 US-stock-macd-analyzer 的 Contents: Read）。本地调试用 US_INSIDER_FEED_FILE。

取不到、文件过旧（> MAX_AGE_DAYS）、或那边当天抓取失败时返回 None（= 不知道），不当成「没有买入」。
名单只含标普 500 公司；不在标普 500 的股票同样返回 None。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TOKEN_ENVS = ("VALUATION_RED_FLAGS_TOKEN", "MACD_QLIB_READ_TOKEN")
ENV_REPO = "US_INSIDER_FEED_REPO"
ENV_FILE = "US_INSIDER_FEED_FILE"
DEFAULT_REPO = "gong97/US-stock-macd-analyzer"
RELEASE_TAG = "dsa-feeds"
ASSET_NAME = "insider_buys.json"
MAX_AGE_DAYS = 5  # 覆盖周末与一个假日；更旧说明那边日报停了
TIMEOUT_SEC = 15

_lock = threading.Lock()
_loaded = False
_feed: Optional[Dict[str, Any]] = None


def _token() -> str:
    for env in TOKEN_ENVS:
        value = os.getenv(env, "").strip()
        if value:
            return value
    return ""


def _fetch() -> Optional[Dict[str, Any]]:
    local = os.getenv(ENV_FILE, "").strip()
    if local:
        with open(local, encoding="utf-8") as fh:
            return json.load(fh)
    token = _token()
    if not token:
        logger.info("[us_insider_feed] 未配置只读 token，跳过美股内部人买入")
        return None
    import requests

    repo = os.getenv(ENV_REPO, "").strip() or DEFAULT_REPO
    headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    rel = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{RELEASE_TAG}",
                       headers=headers, timeout=TIMEOUT_SEC)
    rel.raise_for_status()
    asset = next((a for a in rel.json().get("assets", []) if a.get("name") == ASSET_NAME), None)
    if asset is None:
        logger.warning("[us_insider_feed] release %s 里没有 %s", RELEASE_TAG, ASSET_NAME)
        return None
    resp = requests.get(asset["url"], headers={**headers, "Accept": "application/octet-stream"}, timeout=TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()


def load_insider_feed(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """可用时返回 {"window": [...], "items": {ticker: row}}；否则 None。整次运行只取一次。"""
    global _loaded, _feed
    with _lock:
        if _loaded:
            return _feed
        _loaded = True
        try:
            payload = _fetch() or {}
            if payload.get("status") != "ok":
                if payload:
                    logger.info("[us_insider_feed] 那边当天抓取状态 %s，按不知道处理", payload.get("status"))
                return None
            generated = datetime.fromisoformat(str(payload["generated_at"]))
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            age = ((now or datetime.now(timezone.utc)) - generated).days
            if age > MAX_AGE_DAYS:
                logger.info("[us_insider_feed] 名单是 %s 生成的，已过去 %d 天，跳过", payload["generated_at"], age)
                return None
            items = {_key(r.get("ticker")): r for r in payload.get("items") or [] if r.get("ticker")}
            _feed = {"window": payload.get("window") or ["?", "?"], "items": items,
                     # 标普 500 成分（区分「没买入」与「名单不覆盖」）
                     "universe": {_key(t) for t in payload.get("universe") or []}}
            logger.info("[us_insider_feed] 已载入美股内部人买入名单：%d 家，申报 %s ~ %s",
                        len(items), *_feed["window"][:2])
        except Exception as exc:  # 只是补充信息，取不到不影响日报（常见原因：token 没加美股仓库权限）
            logger.warning("[us_insider_feed] 读取失败，跳过：%s", exc)
            _feed = None
        return _feed


def reset_cache() -> None:
    global _loaded, _feed
    with _lock:
        _loaded, _feed = False, None


def _key(ticker: Any) -> str:
    return str(ticker or "").strip().upper().replace(".", "-")


def insider_buy_for(code: Any) -> Optional[Dict[str, Any]]:
    """{"flag": bool, "window": [...], "row": 名单里的一行或 None}；名单不可用、或该股不在覆盖范围时 None。"""
    feed = load_insider_feed()
    if not feed:
        return None
    key = _key(code)
    row = feed["items"].get(key)
    if row is None and feed["universe"] and key not in feed["universe"]:
        return None  # 不在标普 500：名单不覆盖，不能说「没有买入」
    return {"flag": row is not None, "window": feed["window"], "row": row}


def format_value(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"


def _filed(day: Any) -> str:
    s = str(day or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def describe(info: Dict[str, Any]) -> str:
    row = info.get("row") or {}
    roles = "、".join(row.get("roles") or []) or "内部人"
    return f"{roles} 合计 {format_value(row.get('value'))}，最近申报 {_filed(row.get('last_filed'))}"


def render_insider_prompt(info: Optional[Dict[str, Any]]) -> str:
    """提示词段落：只在有买入时写。"""
    if not isinstance(info, dict) or not info.get("flag"):
        return ""
    window = info.get("window") or ["?", "?"]
    return (
        f"\n### 近期内部人公开市场买入（程序按美股检验口径整理，Form 4 申报 {window[0]} ~ {window[1]}）\n"
        f"- {describe(info)}\n"
        "> 美股历史检验（SEC Form 4，2015~2026）：高管 / 董事 / 10% 大股东公开市场买入之后 10 个交易日平均跑赢"
        "同日其他股票（弱参考，不单独作为买入依据）。以上为程序整理的事实，请直接引用。\n"
    )
