# -*- coding: utf-8 -*-
"""分层分析：Lite 全量初筛 → 选出该动的票 → 高阶模型深度复核。

背景：STOCK_LIST 已经是持仓股列表，所以「该加仓 / 该减仓」两侧都有实际
行动价值，不需要再和持仓求交集。

流程：
1. Stage 1 用低成本模型对全部持仓跑一遍，拿到 action + sentiment_score。
2. Stage 2 按 action 分桶挑候选，数量随当天信号浮动，不硬凑固定条数。
3. Stage 3 用高阶模型只对候选做 FULL 复核。
4. 调用方把三段内容合并成一封邮件（沿用 Issue #190 的 merge 路径）。

模型切换依赖一个事实：analyzer 每次调用都现读 ``config.litellm_model``
（src/analyzer.py 的 models_to_try），因此临时改配置即可换档，无需给
analyze() 增加 model 参数。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional, Sequence

from src.enums import ReportType
from src.schemas.decision_action import display_action_fields_for_result

logger = logging.getLogger(__name__)

# 明确表达「该加仓」与「该减仓」的 action。alert（风险预警）归入减仓侧：
# 持仓股触发预警时同样需要人工复核。
ADD_ACTIONS = ("buy", "add")
CUT_ACTIONS = ("reduce", "sell", "alert")


@dataclass
class TieredCandidate:
    """一只进入深度复核的候选股，同时保留初筛与复核两侧结果。"""

    code: str
    name: str
    side: str  # "add" | "cut"
    lite_action: Optional[str]
    lite_score: int
    deep_result: Optional[Any] = None

    @property
    def deep_score(self) -> Optional[int]:
        if self.deep_result is None:
            return None
        return getattr(self.deep_result, "sentiment_score", None)

    @property
    def deep_action(self) -> Optional[str]:
        if self.deep_result is None:
            return None
        return display_action_fields_for_result(self.deep_result)["action"]

    @property
    def score_delta(self) -> Optional[int]:
        deep = self.deep_score
        if deep is None:
            return None
        return deep - self.lite_score

    @property
    def action_changed(self) -> bool:
        """复核后 action 是否与初筛不一致——分歧本身就是重点信号。"""
        deep = self.deep_action
        return deep is not None and deep != self.lite_action


@dataclass
class TieredAnalysisOutcome:
    lite_results: List[Any] = field(default_factory=list)
    candidates: List[TieredCandidate] = field(default_factory=list)

    @property
    def add_candidates(self) -> List[TieredCandidate]:
        return [c for c in self.candidates if c.side == "add"]

    @property
    def cut_candidates(self) -> List[TieredCandidate]:
        return [c for c in self.candidates if c.side == "cut"]

    @property
    def deep_results(self) -> List[Any]:
        return [c.deep_result for c in self.candidates if c.deep_result is not None]


@contextmanager
def model_scope(config: Any, model: str) -> Iterator[None]:
    """临时切换主模型；空值表示沿用当前配置。

    只改 litellm_model，保留 litellm_fallback_models：换档失败时仍走原有
    降级链，不至于整轮分析失败。
    """
    if not model:
        yield
        return

    original = getattr(config, "litellm_model", "")
    config.litellm_model = model
    logger.info("[tiered] 模型切换: %s -> %s", original or "(default)", model)
    try:
        yield
    finally:
        config.litellm_model = original


@contextmanager
def report_type_scope(config: Any, report_type: ReportType) -> Iterator[None]:
    """临时切换报告类型（pipeline.run 从 config.report_type 读取）。"""
    original = getattr(config, "report_type", "simple")
    config.report_type = report_type.value
    try:
        yield
    finally:
        config.report_type = original


def _action_of(result: Any) -> Optional[str]:
    """取展示口径的 action，与 Web/邮件一致（含 score/action 冲突对齐）。"""
    return display_action_fields_for_result(result)["action"]


def select_candidates(
    lite_results: Sequence[Any],
    *,
    top_n: int = 3,
    include_cut: bool = True,
) -> List[TieredCandidate]:
    """按 action 分桶挑出该深挖的票。

    刻意不用 sentiment_score 的头尾：最低分未必是「最该减仓」的，
    真正该减仓的票常落在中间分段（转差但未崩）。
    """
    usable = [r for r in lite_results if getattr(r, "success", False)]

    add_pool = [r for r in usable if _action_of(r) in ADD_ACTIONS]
    add_pool.sort(key=lambda r: getattr(r, "sentiment_score", 0), reverse=True)

    cut_pool: List[Any] = []
    if include_cut:
        cut_pool = [r for r in usable if _action_of(r) in CUT_ACTIONS]
        # 分数越低越紧急
        cut_pool.sort(key=lambda r: getattr(r, "sentiment_score", 0))

    candidates: List[TieredCandidate] = []
    seen: set[str] = set()
    for side, pool in (("add", add_pool), ("cut", cut_pool)):
        for result in pool[:top_n]:
            code = getattr(result, "code", "")
            if not code or code in seen:
                continue
            seen.add(code)
            candidates.append(
                TieredCandidate(
                    code=code,
                    name=getattr(result, "name", "") or code,
                    side=side,
                    lite_action=_action_of(result),
                    lite_score=getattr(result, "sentiment_score", 0),
                )
            )

    logger.info(
        "[tiered] 初筛 %d 只可用，候选 %d 只（加仓 %d / 减仓 %d）",
        len(usable),
        len(candidates),
        sum(1 for c in candidates if c.side == "add"),
        sum(1 for c in candidates if c.side == "cut"),
    )
    return candidates


def run_tiered_analysis(
    config: Any,
    pipeline: Any,
    stock_codes: Optional[List[str]] = None,
    *,
    current_time: Optional[Any] = None,
) -> TieredAnalysisOutcome:
    """跑完 Stage 1-3，返回初筛结果与带复核结论的候选。

    两个 stage 都用 send_notification=False：推送由调用方在合并邮件时统一
    完成，避免初筛和复核各发一封。
    """
    tier1_model = getattr(config, "tier1_model", "") or ""
    tier2_model = getattr(config, "tier2_model", "") or ""
    top_n = int(getattr(config, "tier2_top_n", 3) or 3)
    include_cut = bool(getattr(config, "tier2_include_cut", True))

    # ---- Stage 1: 全量初筛 ----
    with model_scope(config, tier1_model), report_type_scope(config, ReportType.BRIEF):
        lite_results = pipeline.run(
            stock_codes=stock_codes,
            send_notification=False,
            merge_notification=True,  # 复用「只存不推」语义
            current_time=current_time,
        )

    outcome = TieredAnalysisOutcome(lite_results=list(lite_results or []))

    # ---- Stage 2: 选股 ----
    candidates = select_candidates(
        outcome.lite_results, top_n=top_n, include_cut=include_cut
    )
    outcome.candidates = candidates
    if not candidates:
        logger.info("[tiered] 今日无明确加仓/减仓信号，跳过深度复核")
        return outcome

    # ---- Stage 3: 深度复核 ----
    # 注意：第二遍行情拉取会命中 has_today_data 断点续传而跳过，
    # 但 analyze_stock 无条件执行，所以复核确实会重新调用 LLM。
    with model_scope(config, tier2_model), report_type_scope(config, ReportType.FULL):
        deep_results = pipeline.run(
            stock_codes=[c.code for c in candidates],
            send_notification=False,
            merge_notification=True,
            current_time=current_time,
        )

    by_code = {
        getattr(r, "code", ""): r
        for r in (deep_results or [])
        if getattr(r, "success", False)
    }
    for candidate in candidates:
        candidate.deep_result = by_code.get(candidate.code)
        if candidate.deep_result is None:
            logger.warning("[tiered] %s 深度复核未返回结果，邮件将回落到初筛结论", candidate.code)

    return outcome
