"""结构化公司公告采集（巨潮资讯 / cninfo）。

停牌、控制权变更、要约收购这类事件是**结构化事实**，依赖通用搜索引擎
按关键词召回并不可靠：query 里没有对应词、或公告发布超过新闻时效窗口
（NEWS_MAX_AGE_DAYS 默认3天）时会被静默丢弃。

本模块直连交易所披露接口取公告标题，作为 `announcements` 搜索维度的
权威补充，fail-open：取不到不阻塞主分析链路。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

# 公告落地窗口。比新闻窗口长，因为停牌等事件在复牌前始终有效。
ANNOUNCEMENT_LOOKBACK_DAYS = 30
MAX_ANNOUNCEMENTS = 8

# 命中这些词的公告优先置顶——它们直接改变可交易性或控制权归属。
_HIGH_SIGNAL_KEYWORDS = (
    "停牌",
    "复牌",
    "控制权",
    "实际控制人",
    "控股股东",
    "股权转让",
    "要约收购",
    "重大资产重组",
    "吸收合并",
    "退市",
    "风险警示",
    "立案",
    "处罚",
    "问询函",
    "关注函",
)

_A_SHARE_CODE_RE = re.compile(r"^(\d{6})")


def _normalize_code(code: str) -> str:
    """从 600533.SH / sh600533 / 600533 中提取6位数字代码。"""
    text = str(code or "").strip()
    digits = re.sub(r"\D", "", text)
    match = _A_SHARE_CODE_RE.match(digits)
    return match.group(1) if match else ""


def _is_high_signal(title: str) -> bool:
    return any(keyword in title for keyword in _HIGH_SIGNAL_KEYWORDS)


def fetch_announcements(
    code: str,
    *,
    lookback_days: int = ANNOUNCEMENT_LOOKBACK_DAYS,
    limit: int = MAX_ANNOUNCEMENTS,
) -> List[str]:
    """返回 ["YYYY-MM-DD 公告标题", ...]，高信号公告排在前面。

    任何异常都吞掉并返回空列表——这是对搜索结果的补充，不是必需输入。
    """
    normalized = _normalize_code(code)
    if not normalized:
        return []

    try:
        import akshare as ak

        end = datetime.now()
        start = end - timedelta(days=max(1, lookback_days))
        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=normalized,
            market="沪深京",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as exc:
        logger.warning("公告采集失败 %s: %s", normalized, exc)
        return []

    if df is None or df.empty:
        return []

    high_signal: List[str] = []
    normal: List[str] = []
    seen = set()

    for _, row in df.iterrows():
        title = ""
        date = ""
        for column in ("公告标题", "标题", "announcementTitle", "title"):
            if column in row.index and str(row.get(column)).strip():
                title = str(row.get(column)).strip()
                break
        for column in ("公告时间", "公告日期", "date"):
            if column in row.index and str(row.get(column)).strip():
                date = str(row.get(column)).strip()[:10]
                break
        if not title or title in seen:
            continue
        seen.add(title)
        entry = f"{date} {title}".strip()
        (high_signal if _is_high_signal(title) else normal).append(entry)

    return (high_signal + normal)[: max(1, limit)]


def build_announcement_context(
    code: str,
    stock_name: str = "",
    *,
    lookback_days: int = ANNOUNCEMENT_LOOKBACK_DAYS,
    limit: int = MAX_ANNOUNCEMENTS,
) -> Optional[str]:
    """把公告格式化成可直接拼进 news_context 的文本块。"""
    entries = fetch_announcements(code, lookback_days=lookback_days, limit=limit)
    if not entries:
        return None

    label = f"{stock_name}（{code}）" if stock_name else str(code)
    lines = [
        f"【交易所披露公告 - {label}】（来源：巨潮资讯，近{lookback_days}天，权威结构化数据）",
    ]
    lines.extend(f"- {entry}" for entry in entries)
    lines.append(
        "注：以上为交易所正式披露公告，其事实性优先于搜索引擎召回的新闻报道。"
    )
    return "\n".join(lines)
