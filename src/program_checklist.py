# -*- coding: utf-8 -*-
"""检查清单里的【程序】项：用已检验过的信号由程序判定，插在模型填的【模型】项前面。

为什么
------
检查清单原本 6 项全由模型打勾，没有逐项标准；其中有的事程序已按检验口径算好（过热风险旗标），
模型却还要自己再判断一遍，同样的数据两次运行可能打出不同的勾。另有一项「仓位与止损计划明确」
是模型检查自己写的计划，几乎总是 ✅，已从提示词里删掉。

【程序】项只用已检验过的信号，**只提示、不调分**（不改评分与买卖结论）：
    高位过热       overheat_flags A（乖离过大）或 B（放量急拉）——Macd-Qlib-Analyzer turning_point_confirm
    高位高换手     overheat_flags C——large_cap_turnover_threshold
    近期高管减持   fundamental_context.holder_changes.management_recent_selling——insider_selling_veto
触发标 ⚠️（检验里是弱参考，不是否决条件，所以不用 ❌）；未触发 ✅；数据不足或取数失败 ❓。
简版报告只列 ⚠️/❌ 项，程序项排在最前，检验过的风险会优先显示。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

PROGRAM_TAG = {"zh": "【程序】", "en": "[Rule]", "ko": "[규칙]"}

_TEXT = {
    "zh": {
        "overheat": "高位过热（乖离过大/放量急拉）",
        "turnover": "高位高换手",
        "insider": "近期高管减持",
        "hit": "触发",
        "clear": "未触发",
        "unknown": "数据不足",
        "insider_none": "近期无",
        "insider_failed": "未取到数据",
        "more": "等 {n} 笔",
    },
    "en": {
        "overheat": "Overheated at highs (stretched / volume spike)",
        "turnover": "High turnover at highs",
        "insider": "Recent executive selling",
        "hit": "triggered",
        "clear": "not triggered",
        "unknown": "insufficient data",
        "insider_none": "none recently",
        "insider_failed": "data unavailable",
        "more": "and {n} more",
    },
    "ko": {
        "overheat": "고점 과열(이격 과대/거래량 급증)",
        "turnover": "고점 고회전",
        "insider": "최근 임원 매도",
        "hit": "발생",
        "clear": "해당 없음",
        "unknown": "데이터 부족",
        "insider_none": "최근 없음",
        "insider_failed": "데이터 없음",
        "more": "외 {n}건",
    },
}


def _lang(language: Optional[str]) -> str:
    return language if language in _TEXT else "zh"


def _item(mark: str, tag: str, label: str, detail: str, language: str) -> str:
    sep = "：" if language == "zh" else ": "
    return f"{mark} {tag}{label}{sep}{detail}"


def build_program_checklist(
    overheat_flags: Optional[Dict[str, Any]],
    fundamental_context: Optional[Dict[str, Any]],
    language: str = "zh",
) -> List[str]:
    lang = _lang(language)
    t, tag = _TEXT[lang], PROGRAM_TAG[lang]
    flags = overheat_flags if isinstance(overheat_flags, dict) else {}
    items: List[str] = []

    a, b, c = flags.get("flag_a"), flags.get("flag_b"), flags.get("flag_c")
    if a or b:
        hit = "/".join(name for name, on in (("A", a), ("B", b)) if on)
        items.append(_item("⚠️", tag, t["overheat"], f"{t['hit']}（{hit}）" if lang == "zh" else f"{t['hit']} ({hit})", lang))
    elif a is False and b is False:
        items.append(_item("✅", tag, t["overheat"], t["clear"], lang))
    else:
        items.append(_item("❓", tag, t["overheat"], t["unknown"], lang))

    if c is True:
        items.append(_item("⚠️", tag, t["turnover"], t["hit"], lang))
    elif c is False:
        items.append(_item("✅", tag, t["turnover"], t["clear"], lang))
    else:
        items.append(_item("❓", tag, t["turnover"], t["unknown"], lang))

    block = (fundamental_context or {}).get("holder_changes") if isinstance(fundamental_context, dict) else None
    data = block.get("data") if isinstance(block, dict) and block.get("status") == "ok" else None
    if isinstance(data, dict):
        recent = data.get("management_recent_selling") or []
        if recent:
            first = recent[0]
            detail = f"{first.get('date')} {first.get('who') or ''} {first.get('change') or ''}".strip()
            if len(recent) > 1:
                detail += " " + t["more"].format(n=len(recent))
            items.append(_item("⚠️", tag, t["insider"], detail, lang))
        else:
            items.append(_item("✅", tag, t["insider"], t["insider_none"], lang))
    else:
        items.append(_item("❓", tag, t["insider"], t["insider_failed"], lang))
    return items


def apply_program_checklist(
    result: Any,
    *,
    overheat_flags: Optional[Dict[str, Any]],
    fundamental_context: Optional[Dict[str, Any]],
    language: str = "zh",
) -> List[str]:
    """把【程序】项插到清单最前面；模型自己写的同名【程序】项先去掉（模型改不到这几项）。"""
    dashboard = getattr(result, "dashboard", None)
    if not isinstance(dashboard, dict):
        return []
    battle = dashboard.get("battle_plan")
    if not isinstance(battle, dict):
        battle = {}
        dashboard["battle_plan"] = battle
    existing = battle.get("action_checklist")
    model_items = [str(x) for x in existing] if isinstance(existing, list) else []
    tags = tuple(PROGRAM_TAG.values())
    model_items = [x for x in model_items if not any(tag in x for tag in tags)]
    program_items = build_program_checklist(overheat_flags, fundamental_context, language)
    battle["action_checklist"] = program_items + model_items
    return program_items
