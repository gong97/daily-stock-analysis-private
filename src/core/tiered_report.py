# -*- coding: utf-8 -*-
"""分层分析的邮件正文渲染。

深挖段落刻意突出「初筛 → 复核」的分歧：Lite 说加仓、高阶模型复核后
说持有的票，是当天最值得人工过一眼的一行。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.core.tiered_analysis import ADD_ACTIONS, CUT_ACTIONS, TieredAnalysisOutcome, TieredCandidate
from src.schemas.decision_action import display_action_fields_for_result, localize_action_label
from src.utils.sniper_points import extract_sniper_points

_SIDE_TITLES = {
    "add": "📈 加仓候选（深度复核）",
    "cut": "📉 减仓 / 预警候选（深度复核）",
}

# 紧迫度映射：time_sensitivity 是 LLM 自由字符串，不是强约束的 Literal，
# 所以用「包含匹配」而不是精确相等——顺序很重要，更紧急的判断要排在前面
# （比如某个值同时含"立即"和"观察"字样时，先命中紧急档）。
_URGENCY_MARKERS = (
    ("立即", "🔴"),
    ("今日", "🔴"),
    ("本周", "🟡"),
    ("不急", "🟢"),
)

_SUMMARY_ACTION_LABELS = {"add": "ADD", "cut": "CUT", "hold": "HOLD"}


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


def _summary_action_bucket(result: Any) -> str:
    """把展示口径的 action 归成 add/cut/hold 三档，用于总览表排序和显示。"""
    action = display_action_fields_for_result(result)["action"]
    if action in ADD_ACTIONS:
        return "add"
    if action in CUT_ACTIONS:
        return "cut"
    return "hold"


def _summary_urgency_marker(dashboard: Dict[str, Any]) -> str:
    """🔴🟡🟢：time_sensitivity 是自由字符串，用包含匹配 + 兜底。"""
    time_sensitivity = str(dashboard.get("core_conclusion", {}).get("time_sensitivity") or "")
    for marker, emoji in _URGENCY_MARKERS:
        if marker in time_sensitivity:
            return emoji
    return "—"


def _summary_key_price(bucket: str, deep: Any, dashboard: Dict[str, Any]) -> str:
    """总览表的「关键价格」列：按动作分流，不同动作看不同的价位。"""
    sniper = extract_sniper_points(deep)
    price_position = dashboard.get("data_perspective", {}).get("price_position", {})

    if bucket == "add":
        ideal = sniper.get("ideal_buy")
        secondary = sniper.get("secondary_buy")
        if ideal is not None and secondary is not None:
            lo, hi = sorted((ideal, secondary))
            return f"{_fmt_price(lo)} ~ {_fmt_price(hi)}"
        single = ideal if ideal is not None else secondary
        if single is not None:
            return _fmt_price(single)
        return "—"

    if bucket == "cut":
        stop_loss = sniper.get("stop_loss")
        if stop_loss is not None:
            return f"< {_fmt_price(stop_loss)} 止损"
        return "—"

    # hold：优先给出「突破再买」的压力位，没有就给止损位兜底
    resistance = price_position.get("resistance_level")
    if resistance is not None:
        return f"> {_fmt_price(resistance)} 再买"
    stop_loss = sniper.get("stop_loss")
    if stop_loss is not None:
        return f"< {_fmt_price(stop_loss)} 止损"
    return "—"


def _market_light_header(market_light: Optional[Dict[str, Any]]) -> str:
    """市场状态表头：用确定性计算的 MarketLightSnapshot，不涉及 LLM。"""
    if not isinstance(market_light, dict):
        return ""
    if market_light.get("data_quality") == "unavailable":
        return ""

    temperature_label = str(market_light.get("temperature_label") or "").strip()
    label = str(market_light.get("label") or "").strip()
    score = market_light.get("score")
    guidance = str(market_light.get("guidance") or "").strip()

    status_text = " · ".join(t for t in (temperature_label, label) if t)
    if not status_text and score is None and not guidance:
        return ""

    parts = []
    if status_text:
        parts.append(f"市场状态：{status_text}")
    if score is not None:
        parts.append(f"市场评分：{score}/100")
    if guidance:
        parts.append(guidance)
    return " | ".join(parts)


def render_decision_summary(
    outcome: TieredAnalysisOutcome,
    *,
    market_light: Optional[Dict[str, Any]] = None,
    language: str = "zh",
) -> str:
    """今日持仓决策总览：市场状态表头 + 全部持仓的动作/关键价格/紧迫度一览表。

    排在邮件最开头——这是当天唯一需要通读的表，下面的深度复核卡片和全量
    初筛表是这张表里每一行的依据，不是必须逐条看完的内容。
    """
    results = [r for r in outcome.lite_results if getattr(r, "success", False)]
    if not results:
        return ""

    rows = []
    for result in results:
        bucket = _summary_action_bucket(result)
        dashboard = _dashboard_of(result)
        score = getattr(result, "sentiment_score", 0) or 0
        rows.append(
            {
                "name": getattr(result, "name", ""),
                "code": getattr(result, "code", ""),
                "bucket": bucket,
                "score": score,
                "key_price": _summary_key_price(bucket, result, dashboard),
                "urgency": _summary_urgency_marker(dashboard),
            }
        )

    # 要动的排前面：CUT → ADD → HOLD；组内按评分降序。
    bucket_order = {"cut": 0, "add": 1, "hold": 2}
    rows.sort(key=lambda r: (bucket_order[r["bucket"]], -r["score"]))

    lines: List[str] = ["# 📋 今日持仓决策", ""]

    header = _market_light_header(market_light)
    if header:
        lines.append(header)
        lines.append("")

    lines.append("| 股票 | 动作 | 关键价格 | 紧迫度 |")
    lines.append("| --- | --- | --- | --- |")
    for row in rows:
        action_label = _SUMMARY_ACTION_LABELS[row["bucket"]]
        lines.append(
            f"| {row['name']} {row['code']} | {action_label} | "
            f"{row['key_price']} | {row['urgency']} |"
        )

    return "\n".join(lines)


def _major_change_reason(deep: Any, dashboard: Dict[str, Any]) -> str:
    """今天的结论原文——不是「为什么变了」的归因，系统里没有这个计算。
    用今天 LLM 真实给出的判断依据，不编造差异解释。"""
    one_sentence = (dashboard.get("core_conclusion", {}).get("one_sentence") or "").strip()
    if one_sentence:
        return one_sentence
    return (getattr(deep, "buy_reason", "") or "").strip()


def _major_change_price_diff(label: str, before: Any, after: Any) -> Optional[str]:
    """止损/目标价从 before 变到 after 才输出一行；两边都拿不到数值就跳过。"""
    try:
        before_f = float(before) if before is not None else None
    except (TypeError, ValueError):
        before_f = None
    try:
        after_f = float(after) if after is not None else None
    except (TypeError, ValueError):
        after_f = None

    if before_f is None and after_f is None:
        return None
    if before_f is not None and after_f is not None and abs(before_f - after_f) < 0.005:
        return None
    return f"- {label}：{_fmt_price(before_f)} → {_fmt_price(after_f)}"


def _major_change_arrow(before_bucket: str, after_bucket: str) -> str:
    """ADD 方向算变好（↑），CUT 方向算变差（↓），同档位之间给个中性箭头。"""
    rank = {"cut": 0, "hold": 1, "add": 2}
    if rank[after_bucket] > rank[before_bucket]:
        return "↑"
    if rank[after_bucket] < rank[before_bucket]:
        return "↓"
    return ""


def render_major_changes(
    outcome: TieredAnalysisOutcome,
    *,
    previous: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
    language: str = "zh",
) -> str:
    """今日重大变化：逐只对比「昨天同档位的结论」和「今天的结论」。

    只有动作档位变化，或止损/目标价变化，才算「重大变化」进入这张列表；
    仅评分波动、动作和价位都没变的，一律归进末尾的「其余 N 只无重大变化」，
    避免持仓多时被大量噪音行淹没真正的信号。

    previous 为空（首次运行、当天没有可比基线）时整段不出现，不抛异常。
    """
    if not previous:
        return ""

    results = [r for r in outcome.lite_results if getattr(r, "success", False)]
    if not results:
        return ""

    changed_blocks: List[str] = []
    unchanged_count = 0

    for result in results:
        code = getattr(result, "code", "")
        baseline = previous.get(code)
        if not baseline:
            continue

        dashboard = _dashboard_of(result)
        after_bucket = _summary_action_bucket(result)
        before_action = baseline.get("action")
        before_bucket = "hold"
        if before_action in ADD_ACTIONS:
            before_bucket = "add"
        elif before_action in CUT_ACTIONS:
            before_bucket = "cut"

        sniper = extract_sniper_points(result)
        take_profit_line = _major_change_price_diff(
            "目标", baseline.get("take_profit"), sniper.get("take_profit")
        )
        stop_loss_line = _major_change_price_diff(
            "止损", baseline.get("stop_loss"), sniper.get("stop_loss")
        )
        action_changed = before_bucket != after_bucket

        if not action_changed and take_profit_line is None and stop_loss_line is None:
            unchanged_count += 1
            continue

        before_label = _SUMMARY_ACTION_LABELS.get(before_bucket, before_bucket.upper())
        after_label = _SUMMARY_ACTION_LABELS.get(after_bucket, after_bucket.upper())
        arrow = _major_change_arrow(before_bucket, after_bucket)
        headline = f"**{getattr(result, 'name', '')} {code}** {before_label} → {after_label}"
        if arrow:
            headline += f" {arrow}"

        block_lines = [headline]
        reason = _major_change_reason(result, dashboard)
        if reason:
            block_lines.append(f"- 原因：{reason}")
        if take_profit_line:
            block_lines.append(take_profit_line)
        if stop_loss_line:
            block_lines.append(stop_loss_line)

        changed_blocks.append("\n".join(block_lines))

    if not changed_blocks:
        return ""

    lines: List[str] = ["## 📌 今日重大变化", ""]
    lines.append("\n\n".join(changed_blocks))
    if unchanged_count > 0:
        lines.append("")
        lines.append(f"_其余 {unchanged_count} 只无重大变化。_")

    return "\n".join(lines)


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
    market_light: Optional[Dict[str, Any]] = None,
    previous_decisions: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
    lite_report_type: Any = None,
    language: str = "zh",
    tier1_model: str = "",
    tier2_model: str = "",
) -> str:
    """拼出完整邮件正文：决策总览 → 重大变化 → 大盘 → 深度复核 → 全量初筛。

    决策总览排在最前：当天要不要动、动哪几只，3 秒内看完，不必逐段往下翻。
    重大变化紧随其后：相比上一次同档位的结论，哪些票的判断/点位变了。
    深挖段落排在初筛表之前：需要行动的少数几只应当先被看到。
    """
    parts: List[str] = []

    decision_summary = render_decision_summary(
        outcome, market_light=market_light, language=language
    )
    if decision_summary:
        parts.append(decision_summary)

    major_changes = render_major_changes(
        outcome, previous=previous_decisions, language=language
    )
    if major_changes:
        parts.append(major_changes)

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
