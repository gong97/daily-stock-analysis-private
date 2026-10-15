# -*- coding: utf-8 -*-
"""给模型写的看多理由标注历史检验结论：已检验过、在 A 股上没有优势或方向相反的理由，后面加一句说明。

为什么
------
2026-10-15 检查日报：24 只持仓的「最强看多信号」「利好催化」里反复出现缩量洗盘、RSI 超卖反弹、
均线多头排列、中报业绩大增、主力资金净流入——这些在 Macd-Qlib-Analyzer 的研究台账里都检验过，
没有一条站得住，有的方向还是反的。提示词早已写明资金流「只作描述」，模型仍把它列为利好，
所以由程序在展示层标注，不靠提示词约束。

只标注、不删除、不改分：理由本身可能是对公司的正确描述，标注说的是「把它当成买入理由，历史上
没有优势」。估值偏低不标注——估值因子是台账里确认成立的（value-factor）。

规则与出处（Macd-Qlib-Analyzer research_ledger.jsonl 的条目 id）
--------------------------------------------------------------
    缩量回调 / 洗盘        skill-rules-revision：技巧一的缩量条件方向相反（t −4.36）
    RSI 超卖 / 超跌反弹     reversal-flow-robustness：跌得最多的 20% 之后不反弹
    均线多头排列            skill-rules-revision：趋势向上在短周期偏负（t −1.85）
    主力资金流入            capital-flow：净流入最强的之后 10 日反而多数跑输
    业绩大增 / 超预期       earnings-drift：业绩意外在披露窗口内就被消化，之后没有漂移
    增持                    insider-buying：股东 / 高管增持之后不跑赢
    高股息                  dividend-yield：高股息组不显著跑赢（t 1.46）
    突破                    pattern-breakout：突破确认没有优势
"""

from __future__ import annotations

import re
from typing import Any, List, Pattern, Tuple

_RULES: Tuple[Tuple[Pattern[str], str], ...] = (
    (re.compile(r"缩量(?:回调|回踩|洗盘|整理|企稳|调整)|洗盘"), "缩量回踩的缩量条件历史上方向相反"),
    (re.compile(r"超卖|超跌|反弹(?:需求|动能|要求|空间)"), "跌得多的股票之后不反弹"),
    (re.compile(r"多头排列|均线多头|多头均线"), "均线多头在 10 日上没有优势"),
    (re.compile(r"(?:主力|北向|大单)?资金[^，。；]{0,6}(?:净流入|流入|抢筹)"), "主力净流入之后 10 日反而多数跑输"),
    (re.compile(r"(?:净利润|归母|业绩|营收|利润)[^，。；]{0,10}(?:大增|高增|爆发|超预期|翻倍|大幅增长|高速增长)|超预期"),
     "已公布的业绩在公告窗口内就被消化，之后没有漂移"),
    (re.compile(r"增持"), "股东 / 高管增持之后不跑赢"),
    (re.compile(r"高股息|股息率"), "高股息组不显著跑赢"),
    (re.compile(r"突破"), "突破确认之后没有优势"),
)
_TAG_OPEN, _TAG_CLOSE = "〔历史检验：", "〕"
_MAX_NOTES = 2


def tested_notes(text: Any) -> List[str]:
    if not isinstance(text, str) or not text.strip():
        return []
    notes: List[str] = []
    for pattern, note in _RULES:
        if pattern.search(text) and note not in notes:
            notes.append(note)
    return notes[:_MAX_NOTES]


def annotate(text: Any) -> Any:
    """命中规则就在末尾加〔历史检验：…〕；已标注过、或不命中，原样返回。"""
    if not isinstance(text, str) or _TAG_OPEN in text:
        return text
    notes = tested_notes(text)
    if not notes:
        return text
    return f"{text.rstrip()} {_TAG_OPEN}{'；'.join(notes)}{_TAG_CLOSE}"


def annotate_tested_signals(result: Any, language: str = "zh") -> List[str]:
    """就地标注 dashboard 里的看多理由，返回改动过的字段名。只做中文（规则是中文关键词）。"""
    if language not in (None, "", "zh"):
        return []
    dashboard = getattr(result, "dashboard", None)
    if not isinstance(dashboard, dict):
        return []
    changed: List[str] = []

    attribution = dashboard.get("signal_attribution")
    if isinstance(attribution, dict):
        old = attribution.get("strongest_bullish_signal")
        new = annotate(old)
        if new != old:
            attribution["strongest_bullish_signal"] = new
            changed.append("signal_attribution.strongest_bullish_signal")

    intel = dashboard.get("intelligence")
    catalysts = intel.get("positive_catalysts") if isinstance(intel, dict) else None
    if isinstance(catalysts, list):
        new_list = [annotate(item) for item in catalysts]
        if new_list != catalysts:
            intel["positive_catalysts"] = new_list
            changed.append("intelligence.positive_catalysts")

    battle = dashboard.get("battle_plan")
    checklist = battle.get("action_checklist") if isinstance(battle, dict) else None
    if isinstance(checklist, list):
        # 只标模型自己打 ✅ 的【模型】项：⚠️/❌ 已经是在提示风险，【程序】项本身就是检验口径
        new_list = [
            annotate(item) if isinstance(item, str) and "【模型】" in item and item.lstrip().startswith("✅") else item
            for item in checklist
        ]
        if new_list != checklist:
            battle["action_checklist"] = new_list
            changed.append("battle_plan.action_checklist")
    return changed
