# -*- coding: utf-8 -*-
"""删掉追涨型买点：价格要先涨上去才买的点位与「突破后再买」的建议。

为什么
------
2026-10-15 检查日报发现，模型几乎给每只持仓都写了「放量突破压力位后跟进 / 补仓」的买点，
总览表还把压力位当成「> ¥X 再买」放在最显眼的位置——用户盘前读报告、挂条件单，照这一列挂的
就是突破买单。但历史检验里这类买点站在错的一边（Macd-Qlib-Analyzer）：
    - 过去 5 日涨幅居前 20% 的股票之后 10 个交易日跑输，2011~2019 与 2020~2026 两段都 t≈9
      （reversal_flow_robustness）；
    - 结构类形态的突破确认没有优势（pattern_breakout_backtest，看涨合并方向反而为负）；
    - 观察名单里「放量突破」策略的候选之后 10 日无方向、「资金热度」显著跑输（market_scan_replay）。
提示词已经要求不写这类买点，但模型不总照做（资金流「只作描述」写进提示词后照样被列为利好），
所以由程序兜底删除，不靠提示词。

规则（只删、不改分、不改买卖结论）
--------------------------------
- `ideal_buy` / `secondary_buy`：数值高于现价（容差 0.2%），或文字里带「突破」，换成说明文字。
- 建仓策略 `entry_plan`、空仓 / 持仓建议：含「突破」且含买入动词的分句整句删掉；
  「……确认或放量突破后再行考虑」这类并列尾巴只删「或……突破……」那一段。
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

from src.utils.sniper_points import parse_sniper_value

PRICE_TOLERANCE = 0.002

_REMOVED = {
    "zh": "已移除（追涨型买点：要等价格先涨上去才买，历史检验中这类买点之后多数跑输）",
    "en": "Removed (chase-type entry: buying only after price rises; such entries mostly underperformed in backtests)",
    "ko": "삭제됨(추격형 매수점: 가격이 먼저 올라야 매수하는 방식으로, 백테스트에서 대체로 부진)",
}
_EMPTIED = {
    "zh": "不追涨：只在回踩支撑确认时考虑。",
    "en": "Do not chase; only consider entries on a confirmed pullback to support.",
    "ko": "추격 매수 금지: 지지선 눌림 확인 시에만 고려.",
}
_CHASE = {"zh": ("突破",), "en": ("breakout", "break out", "breaks out", "breaks above"), "ko": ("돌파",)}
_BUY = {
    "zh": ("买", "补仓", "加仓", "跟进", "介入", "吸纳", "建仓", "入场", "进场", "参与", "行动", "考虑", "上车", "布局"),
    "en": ("buy", "add", "enter", "entry", "position", "accumulate", "act", "consider"),
    "ko": ("매수", "진입", "추가", "편입", "행동", "고려"),
}
_CLAUSE_SPLIT = re.compile(r"(?<=[；;。！!])")
# 「……确认或放量突破后再行考虑」「…, or after a breakout」里的并列尾巴
_ALT_TAIL = {
    "zh": re.compile(r"[，,]?\s*或(?:者)?[^，,。；;！!]*?突破[^，,。；;！!]*"),
    "en": re.compile(r",?\s*or\s+(?:after\s+|on\s+)?(?:a\s+)?(?:volume\s+)?break(?:out| out| above)[^,.;!]*", re.I),
    "ko": re.compile(r"[,，]?\s*[^,，.;。]*돌파\s*후"),
}


def _lang(language: Optional[str]) -> str:
    return language if language in _REMOVED else "zh"


def _has_chase(text: str, lang: str) -> bool:
    low = text.lower()
    return any(word in low for word in _CHASE[lang])


def _has_buy(text: str, lang: str) -> bool:
    low = text.lower()
    return any(word in low for word in _BUY[lang])


def scrub_chase_text(text: Any, language: str = "zh") -> Optional[str]:
    """删掉文字里的突破追涨建议；没有可删的返回 None（调用方不改原文）。"""
    if not isinstance(text, str) or not text.strip():
        return None
    lang = _lang(language)
    if not _has_chase(text, lang):
        return None
    kept = []
    for clause in _CLAUSE_SPLIT.split(text):
        if not clause.strip():
            continue
        if _has_chase(clause, lang):
            trimmed = _ALT_TAIL[lang].sub("", clause)
            if _has_chase(trimmed, lang):
                if _has_buy(trimmed, lang):
                    continue
            elif trimmed.strip(" ，,。；;") and trimmed != clause:
                # 删掉尾巴后补回句末标点
                if clause.rstrip()[-1:] in "。；;！!." and trimmed.rstrip()[-1:] not in "。；;！!.":
                    trimmed = trimmed.rstrip() + clause.rstrip()[-1]
                clause = trimmed
        kept.append(clause)
    out = "".join(kept).strip()
    if out == text.strip():
        return None
    out = out.rstrip("；;")
    if out and out[-1] not in "。.!！":
        out += "。" if lang == "zh" else "."
    return out or _EMPTIED[lang]


def is_chase_buy_point(value: Any, current_price: Optional[float], language: str = "zh") -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    lang = _lang(language)
    if isinstance(value, str) and _has_chase(value, lang):
        return True
    price = parse_sniper_value(value)
    return (
        price is not None
        and current_price is not None
        and current_price > 0
        and price > current_price * (1 + PRICE_TOLERANCE)
    )


def strip_chase_buy_points(result: Any, current_price: Optional[float], language: str = "zh") -> List[str]:
    """就地删掉 dashboard 里的追涨型买点与突破建议，返回改动过的字段名（写日志用）。"""
    dashboard = getattr(result, "dashboard", None)
    if not isinstance(dashboard, dict):
        return []
    lang = _lang(language)
    changed: List[str] = []

    battle = dashboard.get("battle_plan")
    if isinstance(battle, dict):
        sniper = battle.get("sniper_points")
        if isinstance(sniper, dict):
            for key in ("ideal_buy", "secondary_buy"):
                if is_chase_buy_point(sniper.get(key), current_price, lang):
                    sniper[key] = _REMOVED[lang]
                    changed.append(f"sniper_points.{key}")
        position = battle.get("position_strategy")
        if isinstance(position, dict):
            new = scrub_chase_text(position.get("entry_plan"), lang)
            if new is not None:
                position["entry_plan"] = new
                changed.append("position_strategy.entry_plan")

    core = dashboard.get("core_conclusion")
    advice = core.get("position_advice") if isinstance(core, dict) else None
    if isinstance(advice, dict):
        for key in ("no_position", "has_position"):
            new = scrub_chase_text(advice.get(key), lang)
            if new is not None:
                advice[key] = new
                changed.append(f"position_advice.{key}")

    if changed:
        dashboard["chase_guard"] = {"removed": list(changed)}
    return changed


def current_price_of(result: Any) -> Optional[float]:
    """现价：优先实时快照，其次结果自带，最后 dashboard 里的价格位置。"""
    snapshot = getattr(result, "market_snapshot", None)
    candidates: List[Any] = []
    if isinstance(snapshot, dict):
        candidates += [snapshot.get("price"), snapshot.get("close")]
    candidates.append(getattr(result, "current_price", None))
    dashboard = getattr(result, "dashboard", None)
    if isinstance(dashboard, dict):
        pp = (dashboard.get("data_perspective") or {}).get("price_position") or {}
        if isinstance(pp, dict):
            candidates.append(pp.get("current_price"))
    for value in candidates:
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None
