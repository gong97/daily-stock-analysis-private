# -*- coding: utf-8 -*-
"""分层分析的邮件正文渲染。

深挖段落刻意突出「初筛 → 复核」的分歧：Lite 说加仓、高阶模型复核后
说持有的票，是当天最值得人工过一眼的一行。
"""

from __future__ import annotations

from typing import Any, List, Optional

from src.core.tiered_analysis import TieredAnalysisOutcome, TieredCandidate
from src.schemas.decision_action import localize_action_label

_SIDE_TITLES = {
    "add": "📈 加仓候选（深度复核）",
    "cut": "📉 减仓 / 预警候选（深度复核）",
}


def _label(action: Optional[str], language: str) -> str:
    if not action:
        return "—"
    return localize_action_label(action, language) or action


def _delta_marker(candidate: TieredCandidate) -> str:
    """把评分变化渲染成一眼可读的标记。"""
    delta = candidate.score_delta
    if delta is None:
        return "复核未返回"
    if delta > 0:
        return f"↑ +{delta}"
    if delta < 0:
        return f"↓ {delta}"
    return "→ 持平"


def render_candidate_block(
    candidate: TieredCandidate,
    *,
    language: str = "zh",
) -> str:
    """单只候选的复核区块。"""
    lines: List[str] = []
    deep = candidate.deep_result

    headline = f"### {candidate.name}({candidate.code})"
    if candidate.action_changed:
        # 分歧优先级最高，直接标在标题上
        headline += " ⚠️ 结论有分歧"
    lines.append(headline)

    lite_label = _label(candidate.lite_action, language)
    deep_label = _label(candidate.deep_action, language)
    lines.append(
        f"- **初筛** {lite_label} / {candidate.lite_score} 分"
        f" → **复核** {deep_label} / "
        f"{candidate.deep_score if candidate.deep_score is not None else '—'} 分"
        f"（{_delta_marker(candidate)}）"
    )

    if deep is None:
        lines.append("- 深度复核未返回结果，请以初筛结论为准。")
        return "\n".join(lines)

    for title, value in (
        ("结论", getattr(deep, "analysis_summary", "")),
        ("理由", getattr(deep, "buy_reason", "")),
        ("核心看点", getattr(deep, "key_points", "")),
        ("风险", getattr(deep, "risk_warning", "")),
    ):
        text = (value or "").strip()
        if text:
            lines.append(f"- **{title}**：{text}")

    return "\n".join(lines)


def render_deep_section(
    outcome: TieredAnalysisOutcome,
    *,
    language: str = "zh",
) -> str:
    """加仓 / 减仓两组深挖内容。"""
    blocks: List[str] = []
    for side in ("add", "cut"):
        group = [c for c in outcome.candidates if c.side == side]
        if not group:
            continue
        blocks.append(f"## {_SIDE_TITLES[side]}")
        blocks.extend(render_candidate_block(c, language=language) for c in group)

    if not blocks:
        return "今日持仓无明确加仓 / 减仓信号，未触发深度复核。"
    return "\n\n".join(blocks)


def render_tiered_email(
    outcome: TieredAnalysisOutcome,
    *,
    notifier: Any,
    market_report: str = "",
    lite_report_type: Any = None,
    language: str = "zh",
    tier1_model: str = "",
    tier2_model: str = "",
) -> str:
    """拼出完整邮件正文：大盘 → 深度复核 → 全量初筛。

    深挖段落放在初筛表之前：需要行动的少数几只应当先被看到。
    """
    parts: List[str] = []

    if market_report:
        parts.append(f"# 📈 大盘复盘\n\n{market_report}")

    deep_section = render_deep_section(outcome, language=language)
    header = "# 🔬 深度复核"
    if tier2_model:
        header += f"（{tier2_model}）"
    parts.append(f"{header}\n\n{deep_section}")

    if outcome.lite_results:
        lite_body = notifier.generate_aggregate_report(
            outcome.lite_results,
            lite_report_type,
        )
        lite_header = f"# 🗂 持仓全量初筛（{len(outcome.lite_results)} 只）"
        if tier1_model:
            lite_header += f"（{tier1_model}）"
        parts.append(f"{lite_header}\n\n{lite_body}")

    return "\n\n---\n\n".join(parts)
