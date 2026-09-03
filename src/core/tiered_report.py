# -*- coding: utf-8 -*-
"""分层分析的邮件正文渲染。

深挖段落刻意突出「初筛 → 复核」的分歧：Lite 说加仓、高阶模型复核后
说持有的票，是当天最值得人工过一眼的一行。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.core.tiered_analysis import TieredAnalysisOutcome, TieredCandidate
from src.schemas.decision_action import localize_action_label
from src.utils.sniper_points import extract_sniper_points

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


def _dashboard_of(deep: Any) -> Dict[str, Any]:
    dashboard = getattr(deep, "dashboard", None)
    return dashboard if isinstance(dashboard, dict) else {}


def _fmt_price(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"¥{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def _current_price_of(deep: Any, dashboard: Dict[str, Any]) -> Optional[float]:
    price_position = dashboard.get("data_perspective", {}).get("price_position", {})
    price = price_position.get("current_price")
    if price is None:
        snapshot = getattr(deep, "market_snapshot", None)
        if isinstance(snapshot, dict):
            price = snapshot.get("close")
    try:
        return float(price) if price is not None else None
    except (TypeError, ValueError):
        return None


def _execution_plan_lines(deep: Any, dashboard: Dict[str, Any]) -> List[str]:
    """🎯 执行计划：买点 / 止损 / 目标位 / 仓位。"""
    sniper = extract_sniper_points(deep)
    position = dashboard.get("battle_plan", {}).get("position_strategy", {})
    suggested_position = (position.get("suggested_position") or "").strip()

    rows = [
        ("理想买点", _fmt_price(sniper.get("ideal_buy"))),
        ("次优买点", _fmt_price(sniper.get("secondary_buy"))),
        ("止损", _fmt_price(sniper.get("stop_loss"))),
        ("第一目标", _fmt_price(sniper.get("take_profit"))),
        ("建议仓位", suggested_position or "—"),
    ]
    rows = [(label, value) for label, value in rows if value != "—"]
    if not rows:
        return []

    lines = ["**🎯 执行计划**", ""]
    lines.extend(f"- {label}：{value}" for label, value in rows)
    return lines


def _key_levels_lines(dashboard: Dict[str, Any], current_price: Optional[float]) -> List[str]:
    """📊 关键位置：MA20 / 支撑 / 压力 / 距支撑 / 距压力。"""
    price_position = dashboard.get("data_perspective", {}).get("price_position", {})
    ma20 = price_position.get("ma20")
    support = price_position.get("support_level")
    resistance = price_position.get("resistance_level")

    volume = dashboard.get("data_perspective", {}).get("volume_analysis", {})
    volume_ratio = volume.get("volume_ratio")

    rows: List[str] = []
    if ma20 is not None:
        rows.append(f"- MA20：{_fmt_price(ma20)}")
    if support is not None:
        rows.append(f"- 支撑：{_fmt_price(support)}")
    if resistance is not None:
        rows.append(f"- 压力：{_fmt_price(resistance)}")

    if current_price is not None and support is not None:
        try:
            distance = (current_price - float(support)) / float(support) * 100
            rows.append(f"- 距支撑：{distance:+.1f}%")
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if current_price is not None and resistance is not None:
        try:
            distance = (float(resistance) - current_price) / current_price * 100
            rows.append(f"- 距压力：{distance:+.1f}%")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if volume_ratio is not None:
        rows.append(f"- 量比：{volume_ratio}")

    if not rows:
        return []
    return ["**📊 关键位置**", ""] + rows


def _why_wait_lines(deep: Any, dashboard: Dict[str, Any]) -> List[str]:
    """🟡 为什么现在不追：核心结论一句话 + 风险提示。"""
    one_sentence = (dashboard.get("core_conclusion", {}).get("one_sentence") or "").strip()
    risk = (getattr(deep, "risk_warning", "") or "").strip()

    rows = [text for text in (one_sentence, risk) if text]
    if not rows:
        return []
    lines = ["**🟡 为什么现在不追**", ""]
    lines.extend(f"- {text}" for text in rows)
    return lines


def _upgrade_conditions_lines(dashboard: Dict[str, Any]) -> List[str]:
    """✅ 转为 BUY 的条件：来自 phase_decision.watch_conditions。"""
    conditions = dashboard.get("phase_decision", {}).get("watch_conditions")
    if not isinstance(conditions, list):
        return []
    conditions = [str(c).strip() for c in conditions if str(c).strip()]
    if not conditions:
        return []
    lines = ["**✅ 转为 BUY 的条件**", ""]
    lines.extend(f"{i}. {text}" for i, text in enumerate(conditions, start=1))
    return lines


def _next_check_lines(dashboard: Dict[str, Any]) -> List[str]:
    """📅 下一观察点：action_window + next_check_time。"""
    phase = dashboard.get("phase_decision", {})
    action_window = (phase.get("action_window") or "").strip()
    next_check = (phase.get("next_check_time") or "").strip()

    text = " / ".join(t for t in (action_window, next_check) if t)
    if not text:
        return []
    return ["**📅 下一观察点**", "", f"- {text}"]


def render_candidate_block(
    candidate: TieredCandidate,
    *,
    language: str = "zh",
) -> str:
    """单只候选的复核区块：执行计划优先，佐证文本放在最后。"""
    lines: List[str] = []
    deep = candidate.deep_result

    headline = f"### 🔬 {candidate.name} {candidate.code}"
    if candidate.action_changed:
        # 分歧优先级最高，直接标在标题上
        headline += " ⚠️ Tier1/Tier2 有分歧"
    lines.append(headline)

    lite_label = _label(candidate.lite_action, language)
    deep_label = _label(candidate.deep_action, language)
    lines.append(f"结论：{lite_label} → {deep_label}")

    if deep is None:
        lines.append("")
        lines.append("深度复核未返回结果，请以初筛结论为准。")
        return "\n".join(lines)

    dashboard = _dashboard_of(deep)
    current_price = _current_price_of(deep, dashboard)
    if current_price is not None:
        lines.append(f"现价：{_fmt_price(current_price)}")

    lines.append("")
    lines.append("| 阶段 | 结论 | 评分 |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| 初筛 | {lite_label} | {candidate.lite_score} |")
    lines.append(
        f"| 深度复核 | {deep_label} | "
        f"{candidate.deep_score if candidate.deep_score is not None else '—'}"
        f"（{_delta_marker(candidate)}） |"
    )

    blocks = [
        _execution_plan_lines(deep, dashboard),
        _key_levels_lines(dashboard, current_price),
        _why_wait_lines(deep, dashboard),
        _upgrade_conditions_lines(dashboard),
        _next_check_lines(dashboard),
    ]
    for block in blocks:
        if block:
            lines.append("")
            lines.extend(block)

    footnotes = []
    for title, value in (
        ("结论", getattr(deep, "analysis_summary", "")),
        ("理由", getattr(deep, "buy_reason", "")),
        ("核心看点", getattr(deep, "key_points", "")),
        ("风险", getattr(deep, "risk_warning", "")),
    ):
        text = (value or "").strip()
        if text:
            footnotes.append(f"- **{title}**：{text}")

    if footnotes:
        lines.append("")
        lines.extend(footnotes)

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


def render_watchlist_section(
    outcome: TieredAnalysisOutcome,
    *,
    language: str = "zh",
) -> str:
    """观察名单：LLM 看多但被资金面护栏拦下的票。"""
    if not outcome.watchlist:
        return ""

    lines = [
        "## 👀 观察名单（看多但买点未到）",
        "",
        "LLM 判断偏多，但资金面/位置护栏未确认，按严进原则暂不给出加仓信号。",
        "",
        "| 股票 | 原始分 | 调整后 | 护栏原因 |",
        "| --- | --- | --- | --- |",
    ]
    for e in outcome.watchlist:
        reason = e.guardrail_reason or "—"
        lines.append(
            f"| {e.name}({e.code}) | **{e.raw_score}** | {e.adjusted_score} | {reason} |"
        )
    return "\n".join(lines)


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

    # 观察名单排在深挖之后、全量初筛之前：它比初筛表重要，但不构成当日行动。
    watchlist_section = render_watchlist_section(outcome, language=language)
    if watchlist_section:
        parts.append(watchlist_section)

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
