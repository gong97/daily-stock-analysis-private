# -*- coding: utf-8 -*-
"""A 股的快讯与政策：个股命中的当日快讯（不调 LLM），以及大盘复盘的「政策与快讯」卡片（每天 1 次 LLM）。

为什么
------
A 股 DSA 的盘面和宏观信息原来只靠搜索引擎（召回不稳、免费额度有限），快讯资讯池没有启用。
2026-10-18 用户选定来源（先在 Actions 上实测过，见 RSSHub 探测：东方财富研报、财联社、金十、证监会可用，
雪球、证券时报、货币网公开市场操作不可用）：

    个股（关键词命中持仓股名称 / 代码，不调 LLM）
        新浪财经 7×24（直连接口，可翻页到 24 小时前；RSSHub 的快讯路由只给最新 15~20 条，覆盖不了一天）
        财联社「看盘」、金十「重要快讯」（RSSHub）
    大盘复盘（一次 LLM 整理成卡片）
        金十重要快讯、财联社看盘、证监会新闻、LPR 公告、央行公开市场操作（RSSHub，取不到就跳过）
        Pekingnology 等宏观 Substack 走 src/substack_digest.py 的大盘卡片（SUBSTACK_FEEDS 里映射到 CN）

RSSHub 不用公共实例（限流、财联社路由在公共实例上失败）：workflow 在 job 里起一个 diygod/rsshub 服务容器，
RSSHUB_BASE 指向它。没有 RSSHUB_BASE 时只用新浪直连，其余跳过。所有来源都 fail-open。

卡片与快讯都标「未核实」，不改评分、不改结论。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ENV_RSSHUB = "RSSHUB_BASE"
CN_TZ = timezone(timedelta(hours=8))
LOOKBACK_HOURS = 26          # 定时任务常被 GitHub 推迟几个小时，多留一点
SINA_URL = "https://zhibo.sina.com.cn/api/zhibo/feed?page={page}&page_size=100&zhibo_id=152"
SINA_MAX_PAGES = 9
TIMEOUT_SEC = 20
MAX_STOCK_ITEMS = 5
MAX_DIGEST_INPUT_CHARS = 24000

# 个股快讯（RSSHub 部分）与大盘卡片的来源：(名称, 路由, 用途)
RSSHUB_SOURCES = (
    ("金十重要快讯", "/jin10/1", {"stock", "market"}),
    ("财联社看盘", "/cls/telegraph/watch", {"stock", "market"}),
    ("证监会", "/gov/csrc/news", {"market"}),
    ("LPR", "/chinamoney/3686", {"market"}),
    ("央行公开市场操作", "/gov/pbc/tradeAnnouncement", {"market"}),
)

_lock = threading.Lock()
_items: Optional[List[Dict[str, Any]]] = None


# --------------------------------------------------------------------------- fetching

def _get(url: str) -> bytes:
    import requests

    resp = requests.get(url, timeout=TIMEOUT_SEC, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.content


def _clean(text: str) -> str:
    text = re.sub(r"(?s)<[^>]+>", " ", text or "")
    text = re.sub(r"&nbsp;|\xa0", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_sina(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for row in ((payload.get("result") or {}).get("data") or {}).get("feed", {}).get("list", []) or []:
        try:
            ts = datetime.strptime(row["create_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
        except (KeyError, ValueError):
            continue
        text = _clean(row.get("rich_text", ""))
        if text:
            focus = bool(row.get("is_focus"))
            # 新浪标为重点（is_focus）的也进大盘卡片：工作日金十「重要」只有十几条
            out.append({"source": "新浪7×24", "time": ts, "text": text, "focus": focus,
                        "uses": {"stock", "market"} if focus else {"stock"}})
    return out


def parse_rss(content: bytes, source: str, uses: set) -> List[Dict[str, Any]]:
    root = ET.fromstring(content)
    out = []
    for node in root.iter("item"):
        title = _clean(node.findtext("title") or "")
        desc = _clean(node.findtext("description") or "")
        try:
            ts = parsedate_to_datetime(node.findtext("pubDate") or "")
            ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        text = desc if desc and (not title or title in desc) else f"{title}。{desc}".strip("。 ")
        out.append({"source": source, "time": ts, "text": text[:1500], "title": title,
                    "link": (node.findtext("link") or "").strip(), "uses": set(uses)})
    return out


def load_items(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """回看期内的全部快讯（各来源合并、按时间倒序）。整次运行只取一次。"""
    global _items
    with _lock:
        if _items is not None:
            return _items
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=LOOKBACK_HOURS)
        items: List[Dict[str, Any]] = []
        try:
            for page in range(1, SINA_MAX_PAGES + 1):
                rows = parse_sina(json.loads(_get(SINA_URL.format(page=page))))
                if not rows:
                    break
                items.extend(r for r in rows if r["time"] >= cutoff)
                if rows[-1]["time"] < cutoff:
                    break
        except Exception as exc:
            logger.warning("[cn_feeds] 新浪 7×24 读取失败：%s", exc)
        base = os.getenv(ENV_RSSHUB, "").strip().rstrip("/")
        if base:
            for name, route, uses in RSSHUB_SOURCES:
                try:
                    rows = parse_rss(_get(base + route), name, uses)
                except Exception as exc:
                    logger.warning("[cn_feeds] %s（%s）读取失败：%s", name, route, exc)
                    continue
                if name == "LPR":  # 每月一次：总是带上最新一期报价，作为利率背景
                    kept = sorted(rows, key=lambda r: r["time"], reverse=True)[:1]
                else:
                    kept = [r for r in rows if r["time"] >= cutoff]
                items.extend(kept)
                logger.info("[cn_feeds] %s：%d 条，回看期内 %d 条", name, len(rows), len(kept))
        else:
            logger.info("[cn_feeds] 未设置 %s，只用新浪 7×24", ENV_RSSHUB)
        items.sort(key=lambda r: r["time"], reverse=True)
        _items = items
        return items


def reset() -> None:
    global _items
    with _lock:
        _items = None


# --------------------------------------------------------------------------- per-stock (no LLM)

def stock_flash(code: str, name: str, items: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """快讯里提到该股（名称或 6 位代码）的条目，最新在前，最多 MAX_STOCK_ITEMS 条。"""
    items = load_items() if items is None else items
    digits = (re.search(r"\d{6}", str(code)) or [None])[0]
    patterns = []
    if name and len(name) >= 2:
        patterns.append(re.escape(name))
    if digits:
        patterns.append(rf"(?<!\d){digits}(?!\d)")
    if not patterns:
        return []
    rx = re.compile("|".join(patterns))
    out, seen = [], set()
    for it in items:
        if "stock" not in it["uses"] or not rx.search(it["text"]):
            continue
        key = it["text"][:40]
        if key in seen:
            continue
        seen.add(key)
        out.append({"source": it["source"], "time": it["time"].astimezone(CN_TZ).strftime("%m-%d %H:%M"),
                    "text": it["text"][:300]})
        if len(out) >= MAX_STOCK_ITEMS:
            break
    return out


def render_stock_prompt(flash: List[Dict[str, Any]]) -> str:
    if not flash:
        return ""
    lines = ["\n### 当日快讯（新浪 7×24 / 财联社 / 金十，程序按名称匹配，未核实）"]
    lines += [f"- {f['time']} {f['source']}：{f['text']}" for f in flash]
    lines.append("> 快讯是媒体转述，可能不完整；与公告、财务数据冲突时以后者为准。可以写进新闻摘要和风险排查并注明来源。")
    return "\n".join(lines) + "\n"


def render_stock_report(flash: List[Dict[str, Any]]) -> List[str]:
    if not flash:
        return []
    out = ["**⚡ 当日快讯（媒体转述，未核实）**", ""]
    out += [f"- {f['time']} {f['source']}：{f['text']}" for f in flash]
    out.append("")
    return out


# --------------------------------------------------------------------------- market review (one LLM call)

DIGEST_PROMPT = """你是严谨的 A 股宏观研究助理。下面是过去一天的快讯与政策公告（来源和时间已标注），以及用户的持仓。
请整理成用户 A 股开盘前要读的三段。只依据下面的内容，不要补充任何外部信息，不要预测涨跌、不要给买卖或择时建议。

只输出一个 JSON 对象，不要任何其他文字：
{{
  "key_points": [{{"point": "要点（事实）", "why": "为什么对 A 股大盘重要（一句话）", "source": "来源", "time": "时间"}}],
  "calendar": [{{"when": "今天 10:00 / 本周三 等", "event": "要公布的数据或要开的会", "prior": "前值（原文有才写）",
                 "expected": "预期（原文有才写）", "actual": "已公布的实际值（原文有才写）"}}],
  "holdings": [{{"holding": "持仓名称(代码)", "news": "相关的快讯事实", "link": "直接提到" 或 "关联推断",
                 "reason": "关联推断时写清推断理由（所在行业 / 上下游 / 同类公司）", "source": "", "time": ""}}],
  "others": [{{"category": "政策 / 数据 / 海外 / 行业 / 观点", "text": "其余值得留意的一条", "source": "", "time": ""}}]
}}

规则：
- key_points 最多 3 条，只挑会影响 A 股大盘或市场风险偏好的事实，按重要性排序；与 A 股关系不大的海外政治、社会新闻不要放。
- calendar 只写快讯里明确提到的日程；已经公布的写 actual，并在 event 里注明与预期 / 前值相比是高还是低（原文有才写）。
- holdings 只能是下面列出的持仓；「直接提到」= 快讯里出现了这家公司；「关联推断」= 快讯讲的是它所在行业、上下游或同类公司，
  必须写 reason。没有相关消息的持仓不要写。
- others 最多 5 条；机构或人士的观点只能放在这里，category 写「观点」，并写明是谁说的。
- 没有内容的部分给空列表。

用户持仓：{holdings}

快讯与公告：
{items}
"""


def _digest_key(items: List[Dict[str, Any]], holdings: List[str]) -> str:
    raw = "|".join(i["text"][:60] for i in items) + "#" + ",".join(holdings)
    return f"CN_MARKET_DIGEST_V2|{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def market_digest(generate: Callable[[str], Optional[str]],
                  holdings: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """大盘复盘的「政策与快讯」三段卡片；没有素材或失败返回 None。同一批素材不重复调 LLM。

    holdings：「名称(代码)」列表，用于第三段「与持仓相关」。
    """
    from src import substack_digest as sd  # 共用卡片缓存

    holdings = [h for h in (holdings or []) if h]
    items = [i for i in load_items() if "market" in i["uses"]]
    if not items:
        return None
    key = _digest_key(items, holdings)
    with sd._lock:
        cached = sd._load_cache().get(key)
    if cached is None:
        lines, total = [], 0
        for i in items:
            line = f"[{i['source']} {i['time'].astimezone(CN_TZ).strftime('%m-%d %H:%M')}] {i['text']}"
            total += len(line)
            if total > MAX_DIGEST_INPUT_CHARS:
                break
            lines.append(line)
        prompt = DIGEST_PROMPT.format(holdings="、".join(holdings) or "（未提供）", items="\n".join(lines))
        try:
            data = sd._parse_json(generate(prompt))
        except Exception as exc:
            logger.warning("[cn_feeds] 政策与快讯整理失败：%s", exc)
            return None
        if not isinstance(data, dict):
            return None
        cached = {"data": data, "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with sd._lock:
            sd._load_cache()[key] = cached
            sd._save_cache()
    data = dict(cached["data"])
    data["sources"] = sorted({i["source"] for i in items})
    return data


def _rows(card: Dict[str, Any], key: str, limit: int) -> List[Dict[str, Any]]:
    return [r for r in (card.get(key) or []) if isinstance(r, dict)][:limit]


def _meta(r: Dict[str, Any]) -> str:
    meta = " ".join(x for x in (r.get("source"), r.get("time")) if x)
    return f"（{meta}）" if meta else ""


def _sections(card: Dict[str, Any]) -> List[tuple]:
    """[(标题, [行])]，三段 + 其他；没有内容的段落不出现。"""
    out = []
    keys = [f"- **{r['point']}**：{r.get('why', '')}{_meta(r)}" for r in _rows(card, "key_points", 3) if r.get("point")]
    if keys:
        out.append(("一、今日要点", keys))
    cal = []
    for r in _rows(card, "calendar", 8):
        if not r.get("event"):
            continue
        nums = "，".join(f"{label}{r[k]}" for k, label in (("actual", "实际 "), ("expected", "预期 "), ("prior", "前值 ")) if r.get(k))
        cal.append(f"- {r.get('when', '')} {r['event']}" + (f"（{nums}）" if nums else ""))
    if cal:
        out.append(("二、日程与事件风险", cal))
    hold = []
    for r in _rows(card, "holdings", 10):
        if not (r.get("holding") and r.get("news")):
            continue
        if r.get("link") == "关联推断":
            hold.append(f"- {r['holding']}【关联推断：{r.get('reason') or '未说明'}】{r['news']}{_meta(r)}")
        else:
            hold.append(f"- {r['holding']}【直接提到】{r['news']}{_meta(r)}")
    if hold:
        out.append(("三、与持仓相关", hold))
    others = [f"- 【{r.get('category') or '其他'}】{r['text']}{_meta(r)}" for r in _rows(card, "others", 5) if r.get("text")]
    if others:
        out.append(("其他（供参考）", others))
    return out


def render_market_prompt(card: Optional[Dict[str, Any]]) -> str:
    if not card:
        return ""
    sections = _sections(card)
    if not sections:
        return ""
    body = "\n".join(f"{title}\n" + "\n".join(lines) for title, lines in sections)
    return ("\n\n## 政策与快讯（程序从 " + "、".join(card.get("sources") or []) + " 整理，未核实）\n" + body +
            "\n> 以上是媒体快讯与公告的整理；「关联推断」是程序推断，不是快讯原文。复盘时可以引用并注明来源；"
            "数据以上面提供的行情为准，不要据此给出择时建议。\n")


def render_market_report(card: Optional[Dict[str, Any]]) -> str:
    if not card:
        return ""
    sections = _sections(card)
    if not sections:
        return ""
    parts = ["\n### 政策与快讯（" + "、".join(card.get("sources") or []) + "，程序整理，未核实）\n"]
    for title, lines in sections:
        parts.append(f"**{title}**\n\n" + "\n".join(lines) + "\n")
    parts.append("> 「关联推断」是程序根据行业 / 上下游推断的，快讯原文没有提到这家公司。\n")
    return "\n".join(parts)
