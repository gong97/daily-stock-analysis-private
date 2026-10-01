# -*- coding: utf-8 -*-
"""Prompt rendering for the shared market-structure context."""

from __future__ import annotations

from typing import Any, List

from src.report_language import normalize_report_language
from src.schemas.market_structure import MARKET_STRUCTURE_SCHEMA_VERSION


def format_market_structure_prompt_section(
    context: Any,
    report_language: str = "zh",
) -> str:
    """Render a compact, low-sensitive market structure section for LLM prompts."""
    if not isinstance(context, dict):
        return ""
    if context.get("schema_version") != MARKET_STRUCTURE_SCHEMA_VERSION:
        return ""
    if context.get("status") == "not_supported":
        return ""

    market_theme = context.get("market_theme_context")
    stock_position = context.get("stock_market_position")
    if not isinstance(market_theme, dict) or not isinstance(stock_position, dict):
        return ""

    language = normalize_report_language(report_language)
    active_themes = _item_names(market_theme.get("active_themes"), limit=5)
    leading_concepts = _item_names(market_theme.get("leading_concepts"), limit=5)
    leading_industries = _item_names(market_theme.get("leading_industries"), limit=5)
    primary_theme = stock_position.get("primary_theme")
    primary_name = (
        str(primary_theme.get("name")).strip()
        if isinstance(primary_theme, dict) and primary_theme.get("name")
        else ""
    )
    # 当天没有任何市场层面的题材证据（CI 上东财板块/概念接口不可达时就是这样），这一段只剩
    # 「位置 unknown/edge + 缺失清单」，模型会照抄成报告里的「成分股排行数据缺失」。整段不写。
    if not (active_themes or leading_concepts or leading_industries):
        return ""
    # 缺失证据清单和 *_partial 这类数据状态标签是内部诊断，不交给模型（数据本身照旧进信号记录）；
    # 「没有证据不要断言龙头」的约束保留。
    risk_tags = [
        tag for tag in (
            str(item.get("code") or item.get("message") or "").strip()
            for item in stock_position.get("risk_tags") or []
            if isinstance(item, dict)
        )
        if tag and "partial" not in tag
    ]

    if language == "en":
        lines = _format_en(context, stock_position, active_themes, leading_concepts, leading_industries, primary_name, risk_tags)
        return "\n".join(lines) + "\n"
    if language == "ko":
        lines = _format_ko(context, stock_position, active_themes, leading_concepts, leading_industries, primary_name, risk_tags)
        return "\n".join(lines) + "\n"

    lines = _format_zh(context, stock_position, active_themes, leading_concepts, leading_industries, primary_name, risk_tags)
    return "\n".join(lines) + "\n"


def _format_en(
    context: Any,
    stock_position: dict[str, Any],
    active_themes: List[str],
    leading_concepts: List[str],
    leading_industries: List[str],
    primary_name: str,
    risk_tags: List[str],
) -> List[str]:
    lines = [
        "\n## Market Structure Context",
        f"- Status: {context.get('status', 'unknown')}",
    ]
    if active_themes:
        lines.append(f"- Active themes: {', '.join(active_themes)}")
    if leading_concepts:
        lines.append(f"- Leading concepts: {', '.join(leading_concepts)}")
    if leading_industries:
        lines.append(f"- Leading industries: {', '.join(leading_industries)}")
    if primary_name:
        lines.append(f"- Stock primary theme: {primary_name}")
    lines.append(f"- Theme phase: {stock_position.get('theme_phase', 'unknown')}")
    lines.append(f"- Stock role: {stock_position.get('stock_role', 'unknown')}")
    if risk_tags:
        lines.append(f"- Risk tags: {', '.join(risk_tags)}")
    lines.append("- Guardrail: do not claim leader-stock status without constituent or leader evidence.")
    return lines


def _format_zh(
    context: Any,
    stock_position: dict[str, Any],
    active_themes: List[str],
    leading_concepts: List[str],
    leading_industries: List[str],
    primary_name: str,
    risk_tags: List[str],
) -> List[str]:
    lines = [
        "\n## 市场结构上下文",
        f"- 状态：{context.get('status', 'unknown')}",
    ]
    if active_themes:
        lines.append(f"- 活跃题材：{'，'.join(active_themes)}")
    if leading_concepts:
        lines.append(f"- 领涨概念：{'，'.join(leading_concepts)}")
    if leading_industries:
        lines.append(f"- 领涨行业：{'，'.join(leading_industries)}")
    if primary_name:
        lines.append(f"- 个股主关联题材：{primary_name}")
    lines.append(f"- 题材阶段：{stock_position.get('theme_phase', 'unknown')}")
    lines.append(f"- 个股位置：{stock_position.get('stock_role', 'unknown')}")
    if risk_tags:
        lines.append(f"- 风险标签：{'，'.join(risk_tags)}")
    lines.append("- 约束：没有成分股或 leader_stocks 证据时，不要断言个股是题材龙头。")
    return lines


def _format_ko(
    context: Any,
    stock_position: dict[str, Any],
    active_themes: List[str],
    leading_concepts: List[str],
    leading_industries: List[str],
    primary_name: str,
    risk_tags: List[str],
) -> List[str]:
    lines = [
        "\n## 시장 구조 컨텍스트",
        f"- 상태: {context.get('status', '알 수 없음')}",
    ]
    if active_themes:
        lines.append(f"- 활성 테마: {', '.join(active_themes)}")
    if leading_concepts:
        lines.append(f"- 선도 테마: {', '.join(leading_concepts)}")
    if leading_industries:
        lines.append(f"- 선도 산업: {', '.join(leading_industries)}")
    if primary_name:
        lines.append(f"- 개별 종목 주력 테마: {primary_name}")
    lines.append(f"- 테마 단계: {stock_position.get('theme_phase', '알 수 없음')}")
    lines.append(f"- 종목 위치: {stock_position.get('stock_role', '알 수 없음')}")
    if risk_tags:
        lines.append(f"- 리스크 태그: {', '.join(risk_tags)}")
    lines.append("- 제약 규칙: 구성종목이나 leader_stocks 근거가 없다면 종목을 테마 선도주로 단정하지 마십시오.")
    return lines


def _item_names(value: Any, *, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    names: List[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        change_pct = item.get("change_pct")
        if isinstance(change_pct, (int, float)):
            names.append(f"{name}({change_pct:+.2f}%)")
        else:
            names.append(name)
        if len(names) >= limit:
            break
    return names
