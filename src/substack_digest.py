# -*- coding: utf-8 -*-
"""Substack 深度文章提炼：讲到名单里股票的文章，由 LLM 按固定格式拆成「观点卡片」，放进该股的分析和日报。

为什么
------
Substack 上的长文常有完整的投资逻辑、报表拆解、估值模型和竞争格局，但读起来很有说服力，
自由总结只会把叙事原样复述。所以按固定格式拆：一句话论点、可核实的事实（附原文引句，防止编造）、
作者的判断、估值假设、催化剂、作者写的风险与没写的风险、证伪条件。卡片在提示词和报告里都标明
「外部观点、未核实」，不改评分、不改结论。

数据来源（2026-10-18 用户选择：先用 RSS）
----------------------------------------
SUBSTACK_FEEDS（GitHub Variables）：每行一个 feed，两种写法都行——
    https://xxx.substack.com/feed                       不限股票，靠粗筛匹配
    TSLA / NVDA<TAB>出版物名<TAB>https://.../feed        只用于这些股票（「同上」= 沿用上一行的股票）
指定了股票的 feed 是专门写这些公司的，粗筛门槛降到出现 1 次；名单里没有的代码（如 SPY、TLT）不会被用到。
公开 RSS 里免费文章是全文，付费文章只有开头一段——卡片会标「仅摘录」。不登录、不抓付费内容。

流程
----
1. 每次运行取一次全部 feed（进程内缓存），只留 SUBSTACK_LOOKBACK_DAYS 天内的文章。
2. 粗筛：文章里出现该股代码（$AAPL、(AAPL)、600519）或名称至少两次，才交给 LLM——避免每只股票 × 每篇文章都调一次。
3. LLM 判断是否真的在分析这只股票（顺带提一句的返回 relevant=false），是的话按格式提炼。
4. 结果按「文章链接 | 股票代码」缓存到 data/substack_cache.json（不进版本库；CI 用 actions/cache 跨天保留），
   同一篇文章不重复调用 LLM。

本仓库是公开的：文章原文和卡片都不提交。
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ENV_FEEDS = "SUBSTACK_FEEDS"
ENV_LOOKBACK = "SUBSTACK_LOOKBACK_DAYS"
ENV_CACHE = "SUBSTACK_CACHE_PATH"
DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "data" / "substack_cache.json"
DEFAULT_LOOKBACK_DAYS = 30
MAX_ARTICLE_CHARS = 30000      # 送给 LLM 的正文上限（约 8k token）
MAX_CARDS_PER_STOCK = 2
MAX_NEW_EXTRACTIONS = 4        # 每只股票每次运行最多新调几次 LLM（粗筛误报多时不至于吃掉额度）
PARTIAL_CHARS = 2500           # 正文短于此视为付费文章的公开摘录
TIMEOUT_SEC = 15
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"

_lock = threading.Lock()
_articles: Optional[List[Dict[str, Any]]] = None
_cache: Optional[Dict[str, Any]] = None


# --------------------------------------------------------------------------- feeds

def _norm_ticker(code: str) -> str:
    return str(code or "").strip().upper().replace("-", ".")


def _norm_url(part: str) -> str:
    url = part if part.startswith("http") else f"https://{part}"
    url = url.rstrip("/")
    return url if url.endswith("/feed") else url + "/feed"


def feed_specs(raw: Optional[str] = None) -> List[Dict[str, Any]]:
    """[{"url", "tickers": set | None}]。见模块说明的两种写法。"""
    raw = os.getenv(ENV_FEEDS, "") if raw is None else raw
    specs: Dict[str, Dict[str, Any]] = {}
    previous: Optional[set] = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls = re.findall(r"https?://\S+|[\w.-]+\.(?:substack\.com|io|com|info|co)(?:/\S*)?", line)
        if not urls:
            continue
        head = line.split(urls[0])[0]
        cells = [c.strip() for c in re.split(r"\t|\s{2,}", head) if c.strip()]
        tickers: Optional[set] = None
        if len(urls) == 1 and cells:
            first = cells[0]
            if first in ("同上", "ditto", "same"):
                tickers = set(previous) if previous else None
            elif re.fullmatch(r"[A-Za-z0-9.\-]+(?:\s*/\s*[A-Za-z0-9.\-]+)*", first):
                tickers = {_norm_ticker(t) for t in first.split("/") if t.strip()}
        previous = tickers
        for u in urls:
            url = _norm_url(u.rstrip(",;"))
            specs.setdefault(url, {"url": url, "tickers": tickers})
    return list(specs.values())


def feed_urls(raw: Optional[str] = None) -> List[str]:
    return [s["url"] for s in feed_specs(raw)]


def html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style|figure|figcaption)[^>]*>.*?</\1>", " ", raw or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>|</tr>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_feed(content: bytes, feed_url: str) -> List[Dict[str, Any]]:
    root = ET.fromstring(content)
    channel = root.find("channel")
    publication = (channel.findtext("title") if channel is not None else "") or feed_url
    items = []
    for node in root.iter("item"):
        body = node.findtext(CONTENT_NS) or node.findtext("description") or ""
        text = html_to_text(body)
        published = None
        try:
            published = parsedate_to_datetime(node.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
        items.append({
            "title": (node.findtext("title") or "").strip(),
            "link": (node.findtext("link") or "").strip(),
            "author": (node.findtext(DC_CREATOR) or "").strip(),
            "publication": publication.strip(),
            "published": published.isoformat() if published else "",
            "text": text,
            "partial": len(text) < PARTIAL_CHARS,
        })
    return items


def load_articles(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """全部 feed 在回看期内的文章；未配置或全部失败时返回 []。整次运行只取一次。"""
    global _articles
    with _lock:
        if _articles is not None:
            return _articles
        specs = feed_specs()
        articles: List[Dict[str, Any]] = []
        if specs:
            import requests

            days = int(os.getenv(ENV_LOOKBACK, "") or DEFAULT_LOOKBACK_DAYS)
            cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
            for spec in specs:
                url = spec["url"]
                try:
                    resp = requests.get(url, timeout=TIMEOUT_SEC, headers={"User-Agent": "Mozilla/5.0 (RSS reader)"})
                    resp.raise_for_status()
                    items = parse_feed(resp.content, url)
                except Exception as exc:  # 单个 feed 失败不影响其他
                    logger.warning("[substack] %s 读取失败：%s", url, exc)
                    continue
                kept = [a for a in items if a["link"] and (not a["published"] or datetime.fromisoformat(a["published"]) >= cutoff)]
                for a in kept:
                    a["tickers"] = sorted(spec["tickers"]) if spec["tickers"] else None
                articles.extend(kept)
                logger.info("[substack] %s：%d 篇，回看期内 %d 篇", url, len(items), len(kept))
        _articles = articles
        return articles


def reset() -> None:
    global _articles, _cache
    with _lock:
        _articles, _cache = None, None


# --------------------------------------------------------------------------- matching

_GENERIC_FIRST_WORDS = {"general", "american", "united", "first", "national", "international", "global",
                        "brown", "bank", "new", "great", "royal", "china", "the"}


def _name_variants(names: List[str]) -> List[str]:
    out = []
    for n in names:
        n = (n or "").strip()
        if not n:
            continue
        out.append(n)
        short = re.sub(r"(?i)[,.]?\s+(inc|corp|corporation|co|ltd|plc|holdings?|group|company|class [ab])\.?$", "", n).strip()
        short = re.sub(r"(?i)[,.]?\s+(inc|corp|corporation|co|ltd|plc)\.?$", "", short).strip()
        short = re.sub(r"(?i)\.com$", "", short).strip().rstrip("&").strip()
        if short and short != n:
            out.append(short)
        # 文章里通常只写首个词（Meta、Berkshire、Amazon）：首词够长、不是泛用词时也算
        first = re.split(r"[\s,]+", short)[0] if short else ""
        if (len(first) >= 4 and first[:1].isupper() and first != short
                and first.lower() not in _GENERIC_FIRST_WORDS):
            out.append(first)
    return [n for n in dict.fromkeys(out) if len(n) >= 2]


def mention_count(text: str, code: str, names: List[str]) -> int:
    """代码与名称在正文里出现的次数（粗筛用）。"""
    count = 0
    plain = code.split(".")[0] if code[:1].isdigit() else code
    if plain.isdigit():
        count += len(re.findall(rf"(?<!\d){plain}(?!\d)", text))
    else:
        ticker = re.escape(code.upper()).replace(r"\.", r"[.\-]")
        count += len(re.findall(rf"\${ticker}\b|\({ticker}\)|(?:NYSE|NASDAQ|Nasdaq):\s*{ticker}\b", text))
        if len(code) >= 3:
            count += len(re.findall(rf"(?<![A-Za-z$]){ticker}(?![A-Za-z])", text)) // 2
    for name in _name_variants(names):
        if re.search(r"[一-鿿]", name):
            count += text.count(name)
        else:
            count += len(re.findall(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text))
    return count


# --------------------------------------------------------------------------- extraction

PROMPT = """你是严谨的证券研究助理。下面是一篇外部文章（Substack），请判断它是否在实质性地分析 {name}（{code}），
是的话按格式提炼。只依据文章本身，不要补充文章里没有的信息。

判断标准：文章有专门的段落讨论这家公司的业务、财务、估值或前景，才算实质性分析；只在列表里出现、
顺带提一句、或作为同行对比出现一两句，都算不相关。

只输出一个 JSON 对象，不要任何其他文字：
{{
  "relevant": true 或 false,
  "direction": "看多" / "看空" / "中性",
  "thesis": "一句话核心论点（中文，不超过 60 字）",
  "facts": [{{"claim": "可核实的事实陈述（数字、历史数据）", "quote": "文章原句（原文语言，不超过 40 词）"}}],
  "judgments": ["作者的主观判断或预测（中文）"],
  "valuation": {{"method": "估值方法", "assumptions": "关键假设（增长率、利润率、倍数、折现率等）", "target": "目标价或隐含空间（没有就写空字符串）"}},
  "catalysts": ["催化剂及时间点"],
  "risks_mentioned": ["作者自己写的风险"],
  "risks_missing": ["这类公司常见、但文章没提的风险（你的补充，最多 3 条）"],
  "falsifiers": ["什么数据或事件出现就说明作者错了（文章没写就根据论点推出，并在开头注明「推断：」）"],
  "author_position": "作者是否披露持仓（原文怎么说；没披露写「未披露」）"
}}
relevant 为 false 时其余字段留空。facts 最多 5 条，每条必须附原句；找不到原句的事实不要写。
付费文章在 RSS 里只有付费墙之前的部分：正文如果在中途戛然而止，只提炼看得到的内容，不要推测后面写了什么。

文章标题：{title}
作者 / 出版物：{author} / {publication}
发布时间：{published}
{partial_note}
正文：
{text}
"""


def _parse_json(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json

            return json.loads(repair_json(m.group(0)))
        except Exception:
            return None


def extract_card(article: Dict[str, Any], code: str, name: str,
                 generate: Callable[[str], Optional[str]]) -> Optional[Dict[str, Any]]:
    prompt = PROMPT.format(
        name=name, code=code, title=article["title"], author=article["author"] or "未署名",
        publication=article["publication"], published=article["published"][:10] or "未知",
        partial_note="（注意：这是付费文章的公开摘录，正文不完整，不要推测缺失部分。）" if article["partial"] else "",
        text=article["text"][:MAX_ARTICLE_CHARS],
    )
    data = _parse_json(generate(prompt))
    if not isinstance(data, dict):
        return None
    return data


def _cache_path() -> Path:
    return Path(os.getenv(ENV_CACHE, "") or DEFAULT_CACHE)


def _load_cache() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_cache or {}, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("[substack] 缓存写入失败：%s", exc)


def cards_for(code: str, names: List[str], generate: Callable[[str], Optional[str]]) -> List[Dict[str, Any]]:
    """该股在回看期内的观点卡片（最新在前，最多 MAX_CARDS_PER_STOCK 张）。失败或没有都返回 []。"""
    articles = load_articles()
    if not articles:
        return []
    name = next((n for n in names if n), code)
    me = _norm_ticker(code)

    def eligible(a: Dict[str, Any]) -> bool:
        if a.get("tickers") is not None and me not in a["tickers"]:
            return False  # 指定了股票的 feed 只用于那些股票
        need = 1 if a.get("tickers") else 2
        return mention_count(a["text"] + " " + a["title"], code, names) >= need

    candidates = sorted((a for a in articles if eligible(a)), key=lambda a: a["published"], reverse=True)
    cards: List[Dict[str, Any]] = []
    new_calls = 0
    for article in candidates:
        key = f"{article['link']}|{code}"
        with _lock:
            cached = _load_cache().get(key)
        if cached is None:
            if new_calls >= MAX_NEW_EXTRACTIONS:
                continue
            new_calls += 1
            try:
                data = extract_card(article, code, name, generate)
            except Exception as exc:
                logger.warning("[substack] %s 提炼失败：%s", article["link"], exc)
                continue
            if data is None:
                continue  # 不缓存失败，下次再试
            cached = {"data": data, "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            with _lock:
                _load_cache()[key] = cached
                _save_cache()
        data = cached["data"]
        if data.get("relevant") is True:
            cards.append({**data, "title": article["title"], "link": article["link"],
                          "author": article["author"], "publication": article["publication"],
                          "published": article["published"][:10], "partial": article["partial"]})
        if len(cards) >= MAX_CARDS_PER_STOCK:
            break
    if cards:
        logger.info("[substack] %s：%d 张观点卡片", code, len(cards))
    return cards


# --------------------------------------------------------------------------- rendering

def _join(items: Any, limit: int = 3) -> str:
    vals = [str(x).strip() for x in (items or []) if str(x).strip()]
    return "；".join(vals[:limit])


def render_prompt(cards: List[Dict[str, Any]]) -> str:
    if not cards:
        return ""
    lines = ["\n### 外部深度文章（Substack，未核实的观点，程序提炼）"]
    for c in cards:
        val = c.get("valuation") or {}
        lines.append(f"- 《{c['title']}》{c.get('publication', '')} {c['published']}{'（付费文章，仅摘录）' if c.get('partial') else ''}："
                     f"{c.get('direction', '')}。论点：{c.get('thesis', '')}")
        facts = [f"{f.get('claim')}" for f in (c.get("facts") or []) if isinstance(f, dict) and f.get("claim")]
        if facts:
            lines.append(f"  作者列举的事实（待核对）：{_join(facts, 4)}")
        if val.get("assumptions") or val.get("target"):
            lines.append(f"  估值：{val.get('method', '')} {val.get('assumptions', '')} {val.get('target', '')}".rstrip())
        if c.get("falsifiers"):
            lines.append(f"  证伪条件：{_join(c['falsifiers'])}")
        if c.get("risks_missing"):
            lines.append(f"  文章未提的风险：{_join(c['risks_missing'])}")
    lines.append(
        "> 以上是外部作者的观点，不是事实。其中的数字要与上面的行情、财务数据核对，对不上的以数据为准；"
        "不要直接采用作者的目标价，也不要因为文章看多 / 看空就改变结论。可以在风险排查和新闻摘要里引用并注明来源。"
    )
    return "\n".join(lines) + "\n"


def render_report_lines(cards: List[Dict[str, Any]]) -> List[str]:
    """日报里每只股票的「Substack 观点」小节（Markdown）。"""
    if not cards:
        return []
    out = ["**📰 Substack 深度观点（外部观点，未核实）**", ""]
    for c in cards:
        val = c.get("valuation") or {}
        head = f"- [{c['title']}]({c['link']}) · {c.get('publication', '')} · {c['published']}"
        if c.get("partial"):
            head += " · 付费文章仅摘录"
        out.append(head)
        out.append(f"  - {c.get('direction', '')}：{c.get('thesis', '')}")
        if val.get("assumptions") or val.get("target"):
            out.append(f"  - 估值假设：{val.get('method', '')} {val.get('assumptions', '')} {val.get('target', '')}".rstrip())
        if c.get("falsifiers"):
            out.append(f"  - 证伪条件：{_join(c['falsifiers'])}")
        if c.get("risks_missing"):
            out.append(f"  - 文章没提的风险：{_join(c['risks_missing'])}")
        out.append(f"  - 作者持仓：{c.get('author_position') or '未披露'}")
    out.append("")
    return out


# --------------------------------------------------------------------------- market review (macro feeds)
#
# 用户 2026-10-18：映射到指数 / 债券 ETF 的 feed（SPY、QQQ、DIA、IWM、TLT…）不对应名单里的个股，
# 改为提炼成「宏观观点卡片」放进该市场的大盘复盘。每个 feed 只取回看期内最新一篇，同样缓存、标未核实。

MARKET_TICKERS = {
    "us": {"SPY", "QQQ", "DIA", "IWM", "TLT", "VOO", "IVV", "VTI", "IEF", "SHY", "HYG", "GLD"},
    # A 股大盘：SUBSTACK_FEEDS 里写 CN（如 Pekingnology，2026-10-18 用户加入）
    "cn": {"CN", "CSI300", "000300"},
}
MARKET_NAMES = {"us": "美股", "cn": "A 股"}
MARKET_LOOKBACK_DAYS = 14
MAX_MARKET_CARDS = 4

MARKET_PROMPT = """你是严谨的宏观研究助理。下面是一篇外部文章（Substack），请判断它是否在表达对{market}大盘、利率或宏观经济的观点，
是的话按格式提炼。只依据文章本身，不要补充文章里没有的信息。纯图表合集、个股分析、广告或活动通知都算不相关。

只输出一个 JSON 对象，不要任何其他文字：
{{
  "relevant": true 或 false,
  "stance": "对股市偏多" / "对股市偏空" / "中性" / "主要谈利率或经济，未表态股市",
  "thesis": "一句话核心观点（中文，不超过 60 字）",
  "data_points": [{{"claim": "文章引用的数据或事实", "quote": "文章原句（原文语言，不超过 40 词）"}}],
  "implications": ["作者认为对股票、债券、板块或风格意味着什么"],
  "horizon": "观点的时间维度（几周 / 几个月 / 一年以上；文章没说写「未说明」）",
  "risks": ["作者自己写的风险或反方观点"],
  "falsifiers": ["什么数据出现就说明作者错了（文章没写就推出，开头注明「推断：」）"]
}}
relevant 为 false 时其余字段留空。data_points 最多 4 条，每条必须附原句。
付费文章在 RSS 里只有付费墙之前的部分：正文如果戛然而止，只提炼看得到的内容。

文章标题：{title}
作者 / 出版物：{author} / {publication}
发布时间：{published}
正文：
{text}
"""


def market_cards(region: str, generate: Callable[[str], Optional[str]],
                 now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """该市场的宏观观点卡片（每个 feed 最新一篇、最多 MAX_MARKET_CARDS 张）；没有配置或失败返回 []。"""
    tickers = MARKET_TICKERS.get(region)
    if not tickers:
        return []
    articles = load_articles()
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=MARKET_LOOKBACK_DAYS)).isoformat()
    latest_per_feed: Dict[str, Dict[str, Any]] = {}
    for a in articles:
        if not a.get("tickers") or not set(a["tickers"]) & tickers:
            continue
        if a["published"] and a["published"] < cutoff:
            continue
        cur = latest_per_feed.get(a["publication"])
        if cur is None or a["published"] > cur["published"]:
            latest_per_feed[a["publication"]] = a
    cards: List[Dict[str, Any]] = []
    new_calls = 0
    for a in sorted(latest_per_feed.values(), key=lambda x: x["published"], reverse=True):
        key = f"{a['link']}|MARKET:{region}"
        with _lock:
            cached = _load_cache().get(key)
        if cached is None:
            if new_calls >= MAX_NEW_EXTRACTIONS:
                continue
            new_calls += 1
            prompt = MARKET_PROMPT.format(
                market=MARKET_NAMES.get(region, region), title=a["title"], author=a["author"] or "未署名",
                publication=a["publication"], published=a["published"][:10] or "未知", text=a["text"][:MAX_ARTICLE_CHARS])
            try:
                data = _parse_json(generate(prompt))
            except Exception as exc:
                logger.warning("[substack] %s 宏观提炼失败：%s", a["link"], exc)
                continue
            if not isinstance(data, dict):
                continue
            cached = {"data": data, "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            with _lock:
                _load_cache()[key] = cached
                _save_cache()
        if cached["data"].get("relevant") is True:
            cards.append({**cached["data"], "title": a["title"], "link": a["link"], "publication": a["publication"],
                          "published": a["published"][:10], "partial": a["partial"]})
        if len(cards) >= MAX_MARKET_CARDS:
            break
    if cards:
        logger.info("[substack] 大盘复盘（%s）：%d 张宏观观点卡片", region, len(cards))
    return cards


def render_market_prompt(cards: List[Dict[str, Any]]) -> str:
    if not cards:
        return ""
    lines = ["\n\n## 外部宏观观点（Substack，未核实，程序提炼）"]
    for c in cards:
        lines.append(f"- 《{c['title']}》{c['publication']} {c['published']}：{c.get('stance', '')}。{c.get('thesis', '')}")
        data = [d.get("claim") for d in (c.get("data_points") or []) if isinstance(d, dict) and d.get("claim")]
        if data:
            lines.append(f"  引用的数据（待核对）：{_join(data, 3)}")
        if c.get("implications"):
            lines.append(f"  作者推论：{_join(c['implications'])}")
    lines.append(
        "> 以上是外部作者的观点，不是事实；数据以上面提供的行情为准。可以在复盘里作为「市场上的观点」引用并注明来源，"
        "不要把它们当成你自己的结论，也不要因此给出择时建议。"
    )
    return "\n".join(lines) + "\n"


def render_market_report(cards: List[Dict[str, Any]]) -> str:
    if not cards:
        return ""
    out = ["", "### 外部宏观观点（Substack，未核实）", ""]
    for c in cards:
        head = f"- [{c['title']}]({c['link']}) · {c['publication']} · {c['published']}"
        if c.get("partial"):
            head += " · 付费文章仅摘录"
        out.append(head)
        out.append(f"  - {c.get('stance', '')}：{c.get('thesis', '')}（时间维度：{c.get('horizon') or '未说明'}）")
        if c.get("implications"):
            out.append(f"  - 作者推论：{_join(c['implications'])}")
        if c.get("falsifiers"):
            out.append(f"  - 证伪条件：{_join(c['falsifiers'])}")
    out.append("")
    return "\n".join(out)
