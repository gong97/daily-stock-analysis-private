# -*- coding: utf-8 -*-
"""两个按日线算的价格事实：「近 5 日涨幅居前」与「预期波动区间（±1 ATR）」。

近 5 日涨幅居前
--------------
个股 5 日涨幅 − 中证 1000 的 5 日涨幅 >= 全市场 90% 分位（扣掉指数，普涨普跌的日子不会全标）。
出处：Macd-Qlib-Analyzer scripts/reversal_flow_robustness.py——过去 5 日涨幅最高 20% 的股票之后 10 个交易日
跑输，2011~2019 与 2020~2026 两个时期 t 都约 9，大盘股也成立。分界线由 Macd-Qlib-Analyzer 每周算好
（scripts/compute_market_cutoff.py），上传到私有仓库 release qlib-model-latest 的 market_cutoff.json；这里用只读
token 下载（与 src/valuation_red_flags.py 共用 VALUATION_RED_FLAGS_TOKEN）。取不到分界线、分界线过期（> 14 天）、
或取不到中证 1000 时返回 None（= 不知道），不当成「没有」。

用途：【程序】风险提示的第 4 项；结构稳定规则在它触发时把「买入」降为观望（2026-10-16 起，替换原来依赖
20 日高点的「突破后不买」——支撑 / 压力价位检验不成立，见 Macd-Qlib-Analyzer scripts/sr_levels_reaction.py）。

预期波动区间
------------
ATR（平均真实波幅）= 近 20 个交易日每天「真实波幅」的平均值；真实波幅 = 当天最高 − 最低，若有跳空，则用
前收盘到当天最高 / 最低中更大的那段。它表示这只股票一天通常上下波动多少钱。预期波动区间 = 最新收盘 ± 1 个 ATR。
它不声称价格会在哪里掉头（八种支撑 / 压力算法都不比随机价位准），只告诉你多远之外才算比平时大的波动。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

TOKEN_ENVS = ("VALUATION_RED_FLAGS_TOKEN", "MACD_QLIB_READ_TOKEN")
ENV_REPO = "MARKET_CUTOFF_REPO"
ENV_FILE = "MARKET_CUTOFF_FILE"
DEFAULT_REPO = "gong97/Macd-Qlib-Analyzer"
RELEASE_TAG = "qlib-model-latest"
ASSET_NAME = "market_cutoff.json"
RUNUP_QUANTILE = 0.9
MAX_AGE_DAYS = 14
INDEX_SYMBOL = "sh000852"  # 中证 1000，与 compute_market_cutoff.py 的 RUNUP_INDEX 同一只
ATR_WINDOW = 20
TIMEOUT_SEC = 15

_lock = threading.Lock()
_cutoff_cache: Optional[Dict[str, Any]] = None
_cutoff_loaded = False
_index_cache: Dict[str, Optional[pd.Series]] = {}


def _token() -> str:
    for env in TOKEN_ENVS:
        value = os.getenv(env, "").strip()
        if value:
            return value
    return ""


def _fetch_cutoff_payload() -> Optional[Dict[str, Any]]:
    local = os.getenv(ENV_FILE, "").strip()
    if local:
        with open(local, encoding="utf-8") as fh:
            return json.load(fh)
    token = _token()
    if not token:
        logger.info("[recent_runup] 未配置只读 token，跳过近 5 日涨幅居前")
        return None
    import requests

    repo = os.getenv(ENV_REPO, "").strip() or DEFAULT_REPO
    headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    rel = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{RELEASE_TAG}",
                       headers=headers, timeout=TIMEOUT_SEC)
    rel.raise_for_status()
    asset = next((a for a in rel.json().get("assets", []) if a.get("name") == ASSET_NAME), None)
    if asset is None:
        logger.warning("[recent_runup] release %s 里没有 %s", RELEASE_TAG, ASSET_NAME)
        return None
    resp = requests.get(asset["url"], headers={**headers, "Accept": "application/octet-stream"}, timeout=TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()


def load_runup_cutoff(today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """{"cutoff": 90% 分位, "top": 95% 分位, "date": "YYYY-MM-DD"}；不可用返回 None。整次运行只取一次。"""
    global _cutoff_cache, _cutoff_loaded
    with _lock:
        if _cutoff_loaded:
            return _cutoff_cache
        _cutoff_loaded = True
        try:
            payload = _fetch_cutoff_payload()
            runup = (payload or {}).get("runup") or {}
            latest = runup.get("latest") or {}
            if not latest:
                return None
            if abs(float(runup.get("quantile", 0)) - RUNUP_QUANTILE) > 1e-9:
                logger.info("[recent_runup] 分界线是 %s 分位，要 %s，跳过", runup.get("quantile"), RUNUP_QUANTILE)
                return None
            as_of = datetime.strptime(latest["date"], "%Y-%m-%d").date()
            age = ((today or date.today()) - as_of).days
            if age > MAX_AGE_DAYS:
                logger.info("[recent_runup] 分界线是 %s 的，已过去 %d 天，跳过", latest["date"], age)
                return None
            _cutoff_cache = {"cutoff": float(latest["cutoff"]), "top": float(latest["cutoff_top"]),
                             "date": latest["date"]}
            logger.info("[recent_runup] 已载入近 5 日涨幅分界线 %s（90%% %.4f）", latest["date"], _cutoff_cache["cutoff"])
        except Exception as exc:
            logger.warning("[recent_runup] 读取分界线失败，跳过：%s", exc)
            _cutoff_cache = None
        return _cutoff_cache


def reset_cache() -> None:
    global _cutoff_cache, _cutoff_loaded
    with _lock:
        _cutoff_cache, _cutoff_loaded = None, False
        _index_cache.clear()


def _index_closes() -> Optional[pd.Series]:
    """中证 1000 近 15 个交易日收盘（腾讯日线）。"""
    with _lock:
        if INDEX_SYMBOL in _index_cache:
            return _index_cache[INDEX_SYMBOL]
    closes: Optional[pd.Series] = None
    try:
        import requests

        resp = requests.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                            params={"param": f"{INDEX_SYMBOL},day,,,15,qfq"}, timeout=TIMEOUT_SEC)
        resp.raise_for_status()
        node = resp.json()["data"][INDEX_SYMBOL]
        rows = node.get("qfqday") or node.get("day") or []
        closes = pd.Series([float(r[2]) for r in rows], index=pd.to_datetime([r[0] for r in rows]))
    except Exception as exc:
        logger.warning("[recent_runup] 中证 1000 日线取数失败：%s", exc)
        closes = None
    with _lock:
        _index_cache[INDEX_SYMBOL] = closes
    return closes


def _bars(bars: Any) -> Optional[pd.DataFrame]:
    if bars is None or len(bars) == 0:
        return None
    df = pd.DataFrame(bars).copy()
    if not {"date", "close"} <= set(df.columns):
        return None
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna(subset=["close"]).drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def compute_recent_runup(bars: Any, cutoff: Optional[Dict[str, Any]] = None,
                         index_closes: Optional[pd.Series] = None) -> Optional[Dict[str, Any]]:
    """{"flag", "top", "excess", "stock_ret", "index_ret", "cutoff", "as_of"}；缺数据返回 None。"""
    df = _bars(bars)
    if df is None or len(df) < 6:
        return None
    cutoff = cutoff if cutoff is not None else load_runup_cutoff()
    if cutoff is None:
        return None
    idx = index_closes if index_closes is not None else _index_closes()
    if idx is None:
        return None
    as_of = df["date"].iloc[-1]
    idx = idx[idx.index <= as_of]
    if len(idx) < 6 or idx.index[-1] != as_of:
        return None  # 指数与个股不是同一天，宁可不判
    stock_ret = float(df["close"].iloc[-1]) / float(df["close"].iloc[-6]) - 1
    index_ret = float(idx.iloc[-1]) / float(idx.iloc[-6]) - 1
    excess = stock_ret - index_ret
    return {"flag": excess >= cutoff["cutoff"], "top": excess >= cutoff["top"], "excess": excess,
            "stock_ret": stock_ret, "index_ret": index_ret, "cutoff": cutoff["cutoff"], "cutoff_date": cutoff["date"],
            "as_of": as_of.strftime("%Y-%m-%d")}


def compute_expected_range(bars: Any) -> Optional[Dict[str, Any]]:
    """最新收盘 ± 1 个 ATR(20)。"""
    df = _bars(bars)
    if df is None or len(df) < ATR_WINDOW + 1 or not {"high", "low"} <= set(df.columns):
        return None
    h, l, c = (df[k].astype(float) for k in ("high", "low", "close"))
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr = float(tr.iloc[-ATR_WINDOW:].mean())
    close = float(c.iloc[-1])
    if not atr > 0 or not close > 0:
        return None
    return {"atr": atr, "atr_pct": atr / close, "close": close, "low": close - atr, "high": close + atr,
            "as_of": df["date"].iloc[-1].strftime("%Y-%m-%d")}
