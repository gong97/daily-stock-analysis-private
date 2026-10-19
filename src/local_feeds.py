# -*- coding: utf-8 -*-
"""兜底读取：GitHub Actions 上取不到的资讯源，由用户本机每天抓一份放在私有仓库的 release 里。

为什么
------
有些来源屏蔽云服务器 IP（2026-10-18 实测）：`*.substack.com` 的 RSS 在 Actions 上 403，央行公开市场业务
交易公告没有可用的 RSSHub 路由。用户本机每天 9:30（美东）的行情任务顺带把它们抓下来，上传到
Macd-Qlib-Analyzer（私有）的 release `local-feeds`（该仓库 fetch_local_feeds.py）。这里用只读 token
（VALUATION_RED_FLAGS_TOKEN，本来就有该仓库权限）读取。

index.json 的 key 是 feed URL，或 "pboc_omo"（央行公开市场操作，已整理成 RSS）。超过 MAX_AGE_HOURS 的视为
没有（本机没开或任务没跑），调用方照常跳过。
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TOKEN_ENVS = ("VALUATION_RED_FLAGS_TOKEN", "MACD_QLIB_READ_TOKEN")
ENV_REPO = "LOCAL_FEEDS_REPO"
DEFAULT_REPO = "gong97/Macd-Qlib-Analyzer"
RELEASE_TAG = "local-feeds"
MAX_AGE_HOURS = 72          # 覆盖周末：周五上传的到周一晚上还能用
TIMEOUT_SEC = 20

_lock = threading.Lock()
_release: Optional[Dict[str, Any]] = None   # {"assets": {name: api_url}, "index": {...}}
_loaded = False


def _token() -> str:
    for env in TOKEN_ENVS:
        value = os.getenv(env, "").strip()
        if value:
            return value
    return ""


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}


def _load_release() -> Optional[Dict[str, Any]]:
    global _release, _loaded
    with _lock:
        if _loaded:
            return _release
        _loaded = True
        token = _token()
        if not token:
            return None
        try:
            import requests

            repo = os.getenv(ENV_REPO, "").strip() or DEFAULT_REPO
            rel = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{RELEASE_TAG}",
                               headers=_headers(token), timeout=TIMEOUT_SEC)
            rel.raise_for_status()
            assets = {a["name"]: a["url"] for a in rel.json().get("assets", [])}
            if "index.json" not in assets:
                return None
            idx = requests.get(assets["index.json"], headers={**_headers(token), "Accept": "application/octet-stream"},
                               timeout=TIMEOUT_SEC)
            idx.raise_for_status()
            _release = {"assets": assets, "index": idx.json().get("feeds", {})}
            logger.info("[local_feeds] 已载入本机抓取的 %d 个源", len(_release["index"]))
        except Exception as exc:
            logger.warning("[local_feeds] 读取 release 失败：%s", exc)
            _release = None
        return _release


def reset() -> None:
    global _release, _loaded
    with _lock:
        _release, _loaded = None, False


def get(key: str, now: Optional[datetime] = None) -> Optional[bytes]:
    """本机抓的那份内容（feed URL 或 "pboc_omo"）；没有、太旧或失败返回 None。"""
    rel = _load_release()
    if not rel:
        return None
    entry = rel["index"].get(key)
    if not entry or entry.get("asset") not in rel["assets"]:
        return None
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
        age_h = ((now or datetime.now(timezone.utc)) - fetched).total_seconds() / 3600
    except (KeyError, ValueError):
        return None
    if age_h > MAX_AGE_HOURS:
        logger.info("[local_feeds] %s 是 %.0f 小时前抓的，太旧，跳过", key, age_h)
        return None
    try:
        import requests

        resp = requests.get(rel["assets"][entry["asset"]],
                            headers={**_headers(_token()), "Accept": "application/octet-stream"}, timeout=TIMEOUT_SEC)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.warning("[local_feeds] %s 下载失败：%s", key, exc)
        return None
