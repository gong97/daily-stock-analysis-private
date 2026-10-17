# -*- coding: utf-8 -*-
"""按市场切换提示词里的「历史检验」结论：A 股的检验结果不能直接套到美股上。

为什么
------
提示词、交易策略里写的「下列理由没有优势」「近 5 日涨幅居前不买」「高位过热」等都来自
Macd-Qlib-Analyzer 的 A 股检验。2026-10 把同一批检验在 US-stock-macd-analyzer 上复现过，
有几条在美股不成立，甚至方向相反（该仓库 research_ledger.jsonl）：

    短期反转 / 近 5 日涨幅居前    A 股成立（t 约 9）；标普 500 不成立（reversal-us-sp500）
    高位过热 A/B                  A 股成立；美股不成立（overheat-us-a / overheat-us-b）
    内部人                        A 股高管减持有效、增持无效；美股反过来：公开市场买入之后跑赢
                                  （insider-buying-us-*），高管卖出无效（insider-selling-us-mgmt）
    财报超预期后漂移              A 股没有；美股按登记规则判无效，但诊断里很可能存在，前瞻复核中（pead-us-*）
    估值便宜组合                  A 股估值因子成立；美股估值组合不过（value-portfolio-us）
两边一致：形态 / 突破无方向（pattern-detectors-us）、缩量回踩与均线多头等技术加分项无优势
（six-skill-bonus-us）、MACD 买入触发无优势（macd-buy-trigger-us）、支撑 / 压力不比随机价位准（sr-levels-us）。
美股额外成立的风险信号：高位放量异动（skill4-danger-us，见 src/us_surge_flag.py）。

做法：提示词原文保留 A 股版本（上游合并时改动最小），美股代码在组装好的提示词上逐句替换。
每一句原文都必须在替换时找到——tests/test_market_evidence.py 检查原文没有漂移。
港股、日股等没有做过检验，保持原样。
"""

from __future__ import annotations

from typing import Optional, Tuple

from src.market_context import detect_market

# (A 股原文片段, 美股替换文本)。片段取自 src/analyzer.py、src/agent/executor.py 与 strategies/*.yaml。
US_REPLACEMENTS: Tuple[Tuple[str, str], ...] = (
    (
        "技术形态（均线多头排列、缩量回调、超卖反弹、放量突破）在 A 股历史检验中没有优势",
        "技术形态（均线多头排列、缩量回调、放量突破、MACD 金叉 / 背离、图表形态）在美股历史检验中没有优势",
    ),
    (
        "主力资金流向只作描述、不作买卖依据：历史检验（2020~2026，300 只随机样本）中主力净流入最强的股票之后"
        " 10 个交易日反而多数跑输（多是刚涨过的），扣除前期涨跌后没有信息。",
        "资金流向在美股没有检验过，只作描述、不作买卖依据。",
    ),
    (
        "支撑位 / 压力位在 A 股历史检验中不比随机价位准（八种算法，Macd-Qlib-Analyzer sr_levels_reaction）",
        "支撑位 / 压力位在美股历史检验中同样不比随机价位准（八种算法，US-stock-macd-analyzer sr-levels-us）",
    ),
    (
        "下列理由在 A 股历史检验（Macd-Qlib-Analyzer，随机样本 + 全市场）中没有优势或方向相反，不要把它们当作看多或"
        "加仓的依据（可以作为事实描述）：缩量回调 / 洗盘、RSI 超卖或超跌后的反弹需求、均线多头排列、主力资金净流入、"
        "已公布的业绩大增或超预期、股东 / 高管增持、高股息、放量突破。程序会在报告里给这些理由加上检验标注。",
        "下列理由在美股历史检验（US-stock-macd-analyzer，标普 500 与随机样本）中没有优势，不要把它们当作看多或"
        "加仓的依据（可以作为事实描述）：缩量回调 / 洗盘、均线多头排列、MACD 金叉或底背离、图表形态与放量突破、"
        "仅凭估值便宜（美股估值最便宜一组不跑赢）。与 A 股不同：美股高管 / 大股东在公开市场买入之后历史上跑赢"
        "（弱参考，可作为看多理由之一）；财报超预期之后的漂移尚未确认，不能单独作为买入依据。"
        "程序会在报告里给无效理由加上检验标注。",
    ),
    ("不追涨：近 5 日涨幅居全市场前 10% 时不给买入（程序会改判为观望）。", "不追涨："),
    ("不追涨：近 5 日涨幅居全市场前 10% 时不给买入（程序会改判为观望）；", "不追涨："),
    (
        "历史检验（Macd-Qlib-Analyzer，2011~2026）中刚涨过、放量突破的股票之后 10 个交易日多数跑输，形态突破确认也没有优势。",
        "美股历史检验中形态突破确认没有优势，放量急涨到近 60 日高位附近的股票之后跑输。",
    ),
    (
        "高位过热、高位高换手、近期高管减持由程序按检验口径判定后插在清单最前面（标【程序】），不要自己写这几项",
        "高位放量异动、近期内部人买入由程序按美股检验口径判定后插在清单最前面（标【程序】），不要自己写这两项",
    ),
    (
        "（A 股历史检验中突破确认没有优势、刚涨过的股票之后多数跑输）",
        "（美股历史检验中形态突破确认没有优势）",
    ),
    (
        "A 股历史检验中短期涨幅居前、放量急拉的股票之后多数跑输；已公布的业绩意外在公告窗口内就被消化。",
        "美股历史检验中放量急涨到近 60 日高位附近的股票之后跑输；财报超预期之后是否继续漂移尚未确认。",
    ),
)


def evidence_market(stock_code: Optional[str]) -> Optional[str]:
    """'cn'（A 股检验适用）、'us'（美股检验适用），其他市场没有检验，返回 None。"""
    market = detect_market(stock_code)
    if market in ("cn", "us"):
        return market
    return None


def is_us(stock_code: Optional[str]) -> bool:
    return evidence_market(stock_code) == "us"


def localize_prompt(prompt: str, stock_code: Optional[str]) -> str:
    """美股代码：把提示词里 A 股的检验结论换成美股的；其他市场原样返回。"""
    if not prompt or not is_us(stock_code):
        return prompt
    for cn_text, us_text in US_REPLACEMENTS:
        prompt = prompt.replace(cn_text, us_text)
    return prompt
