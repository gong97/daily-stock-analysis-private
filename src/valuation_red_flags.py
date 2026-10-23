# -*- coding: utf-8 -*-
"""估值报告的质量红旗：运行时读 Macd-Qlib-Analyzer 的 reports/red_flags.json，接进持仓日报。

为什么
------
2026-10-15 检查日报：五粮液的检查清单写着「✅ 无重大利空」，而 Macd-Qlib-Analyzer 的估值报告
（Claude 每天 3 只、约 91 天轮一次）把 2025 年会计更正争议、经营现金流与收入背离列为重大红旗。
日报的新闻只覆盖近几天，几个月前的事看不到；估值报告正好补上这一块。

红旗不是检验过的信号（与 src/program_checklist.py 的【程序】项不同），所以：
- 不改评分、不改买卖结论；
- 写进提示词作为事实，让模型在「无重大利空」这类判断里不再忽略；
- 报告里单列一段，注明来源报告日期。

分类（2026-10-23 起）：以前只要有红旗就把模型的「✅ 无重大利空」改成 ⚠️，50 只有红旗的股票里 49 只被改，
多数只是「商誉占比接近阈值」「少数股东权益高」这类财务提示。现在 red_flags.json 给每条红旗带 flag_kinds
（Macd-Qlib build_red_flags.py 分类，说明文字在 kind_notes，这里不写研究结论）：
- major（立案、处罚、会计更正、冻结等）：仍把「✅ 无重大利空」改成 ⚠️；
- tested / other（财务质量提示）：不改模型的勾，清单末尾另加一行「⚠️ 【估值报告】财务提示」，警示仍在。
没有 flag_kinds 的旧文件全部按 major 处理，与以前相同。美股仓库也带 flag_kinds（只有 major / other，
A 股那次检验没在美股复现过）。

数据来源与隐私
--------------
本仓库是公开的，Macd-Qlib-Analyzer 是私有的。红旗文件不提交到本仓库，运行时用只读 token
（VALUATION_RED_FLAGS_TOKEN，fine-grained PAT，只给 Macd-Qlib-Analyzer 的 Contents: Read）
通过 GitHub API 读取。没有 token 时整段跳过，日报照常生成。本地调试可用 VALUATION_RED_FLAGS_FILE
指向一份本地 json。

美股（2026-10-17 起）：美股代码读 US-stock-macd-analyzer（私有）的 reports/red_flags.json（该仓库
build_red_flags.py 生成，Claude 每个工作日估值 1 只），按 ticker 查（BRK.B 与报告里的 BRK-B 视为同一只）。
同一个 token，需要在 fine-grained PAT 的仓库权限里加上 US-stock-macd-analyzer；没加时美股这段跳过。
本地调试用 US_VALUATION_RED_FLAGS_FILE。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from src.market_evidence import evidence_market

logger = logging.getLogger(__name__)

ENV_TOKEN = "VALUATION_RED_FLAGS_TOKEN"
ENV_REPO = "VALUATION_RED_FLAGS_REPO"
ENV_FILE = "VALUATION_RED_FLAGS_FILE"
DEFAULT_REPO = "gong97/Macd-Qlib-Analyzer"
US_ENV_REPO = "US_VALUATION_RED_FLAGS_REPO"
US_ENV_FILE = "US_VALUATION_RED_FLAGS_FILE"
US_DEFAULT_REPO = "gong97/US-stock-macd-analyzer"
# 报告来源，提示词与报告里注明
SOURCE_LABEL = {"cn": "Macd-Qlib 估值报告", "us": "美股估值报告"}
REMOTE_PATH = "reports/red_flags.json"
TIMEOUT_SEC = 15
MAX_AGE_DAYS = 200  # 约两轮重估周期；更旧的报告不再引用

_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}  # 市场 -> {键: 条目}
_notes: Dict[str, Dict[str, str]] = {}  # 市场 -> kind_notes
REPORT_TAG = "【估值报告】"
KIND_ORDER = ("major", "tested", "other")
_NO_RISK_CHECK = re.compile(r"无(?:重大|实质性|明显)?(?:突发)?(?:重大)?(?:恶性)?利空")


def _fetch_remote(token: str, repo: str) -> Dict[str, Any]:
    import requests

    resp = requests.get(
        f"https://api.github.com/repos/{repo}/contents/{REMOTE_PATH}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=TIMEOUT_SEC,
    )
    resp.raise_for_status()
    return resp.json()


def load_red_flags(market: str = "cn") -> Dict[str, Any]:
    """{键: 条目}（A 股键是 6 位代码，美股是 ticker）；取不到返回 {}。每个市场整次运行只取一次。"""
    with _lock:
        if market in _cache:
            return _cache[market]
        env_file, env_repo, default_repo = (
            (US_ENV_FILE, US_ENV_REPO, US_DEFAULT_REPO) if market == "us" else (ENV_FILE, ENV_REPO, DEFAULT_REPO)
        )
        stocks: Dict[str, Any] = {}
        try:
            local = os.getenv(env_file, "").strip()
            token = os.getenv(ENV_TOKEN, "").strip()
            if local:
                with open(local, encoding="utf-8") as fh:
                    payload = json.load(fh)
            elif token:
                payload = _fetch_remote(token, os.getenv(env_repo, "").strip() or default_repo)
            else:
                logger.info("[valuation_red_flags] 未配置 %s，跳过估值报告红旗", ENV_TOKEN)
                payload = {}
            notes = payload.get("kind_notes") if isinstance(payload, dict) else None
            _notes[market] = {str(k): str(v) for k, v in notes.items()} if isinstance(notes, dict) else {}
            raw = payload.get("stocks") if isinstance(payload, dict) else None
            if isinstance(raw, dict):
                for k, v in raw.items():
                    key = _key(k, market)
                    if key and isinstance(v, dict):
                        stocks[key] = v
            if stocks:
                logger.info("[valuation_red_flags] 已载入 %d 只的估值报告红旗（%s）", len(stocks), market)
        except Exception as exc:  # 只是补充信息，取不到不影响日报（美股常见原因：token 没加美股仓库权限）
            logger.warning("[valuation_red_flags] 读取失败（%s），跳过：%s", market, exc)
            stocks = {}
        _cache[market] = stocks
        return stocks


def reset_cache() -> None:
    with _lock:
        _cache.clear()
        _notes.clear()


def _digits(code: Any) -> str:
    m = re.search(r"\d{6}", str(code or ""))
    return m.group(0) if m else ""


def _key(code: Any, market: str) -> str:
    if market == "us":
        return str(code or "").strip().upper().replace(".", "-")
    return _digits(code)


def _age_days(day: str, today: Optional[date] = None) -> Optional[int]:
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return ((today or date.today()) - d).days


def red_flags_for(code: Any, today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """该股最新估值报告的红旗；没有、太旧或两类条目都为空时返回 None。"""
    market = evidence_market(code)
    if market not in ("cn", "us"):
        return None
    key = _key(code, market)
    if not key:
        return None
    entry = load_red_flags(market).get(key)
    if not entry:
        return None
    age = _age_days(entry.get("date", ""), today)
    if age is not None and age > MAX_AGE_DAYS:
        return None
    raw_flags = [str(x) for x in entry.get("flags") or []]
    raw_kinds = entry.get("flag_kinds")
    if not (isinstance(raw_kinds, list) and len(raw_kinds) == len(raw_flags)):
        raw_kinds = ["major"] * len(raw_flags)  # 没分类的旧文件：与以前一样，每条都当重大
    pairs = [(f, k if k in KIND_ORDER else "other") for f, k in zip(raw_flags, raw_kinds) if f.strip()]
    pairs.sort(key=lambda p: KIND_ORDER.index(p[1]))  # 报告栏最多列 4 条，重大的排前面
    flags = [f for f, _ in pairs]
    risks = [str(x) for x in entry.get("company_risks") or [] if str(x).strip()]
    if not flags and not risks:
        return None
    # 报告原文里置信度常写成「中低** —— 核心原因：…」，只留开头的等级词
    confidence = re.split(r"[*—（(，,：:\s]", str(entry.get("confidence") or "").strip(), maxsplit=1)[0]
    return {"date": entry.get("date", ""), "confidence": confidence,
            "flags": flags, "flag_kinds": [k for _, k in pairs], "kind_notes": dict(_notes.get(market, {})),
            "company_risks": risks, "age_days": age, "source": SOURCE_LABEL[market]}


def flag_kinds(entry: Dict[str, Any]) -> List[str]:
    """与 entry['flags'] 一一对应的类别；没带 flag_kinds 的条目（直接构造的、旧文件）全部当 major。"""
    flags = entry.get("flags") or []
    kinds = entry.get("flag_kinds")
    if isinstance(kinds, list) and len(kinds) == len(flags):
        return [k if k in KIND_ORDER else "other" for k in kinds]
    return ["major"] * len(flags)


def render_red_flags_prompt(entry: Optional[Dict[str, Any]]) -> str:
    """提示词段落：作为事实提供，要求在风险排查里考虑。"""
    if not entry:
        return ""
    source = entry.get("source") or SOURCE_LABEL["cn"]
    if source == SOURCE_LABEL["cn"]:
        source = "Macd-Qlib-Analyzer 估值报告"  # A 股提示词原文不变
    lines = [f"\n### 估值报告质量红旗（{source}，{entry['date']}）"]
    kinds = flag_kinds(entry)
    for item, kind in zip(entry["flags"], kinds):
        lines.append(f"- {'红旗' if kind == 'major' else '财务提示'}：{item}")
    for item in entry["company_risks"]:
        lines.append(f"- 公司层面风险：{item}")
    lines.append(
        "\n> 以上来自较早的深度估值报告，不一定出现在近几日新闻里。「红旗」是治理 / 合规类重大事项，判断「有无"
        "重大利空」与给分时要考虑，不要因为近期新闻没提到就写「无重大利空」。「财务提示」单独不构成重大利空，"
        "也不据此调分，可以在分析里提及。**不要把它们写进 risk_alerts**：报告会在「估值报告红旗 / "
        "公司层面风险」一栏单独列出；risk_alerts 只写分析窗口内的新事件（新闻、公告、减持、处罚等），"
        "不得给这些旧条目标上近期日期。"
    )
    note = (entry.get("kind_notes") or {}).get("tested")
    if note and "tested" in kinds:
        lines.append(f"> 财务质量类提示（现金流 / 存货 / 应收）：{note}。")
    return "\n".join(lines) + "\n"


# 风险警报与估值报告条目连续相同多少个字算「抄过来的」。2026-10-20 的日报里 48 条风险警报有 34 条与估值报告
# 重合（71%），其中 12 条被标成近几日日期、没注明来源；抄写的条目与原文的最长连续相同片段都在 12 字以上。
COPY_MIN_CHARS = 12


def _longest_common(a: str, b: str) -> int:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b)).size


def drop_copied_alerts(alerts: List[Any], entry: Dict[str, Any]) -> List[Any]:
    """去掉从估值报告抄进风险警报的条目（报告另有一栏显示它们）。非字符串条目原样保留。"""
    sources = [str(x) for x in (entry.get("flags") or []) + (entry.get("company_risks") or []) if str(x).strip()]
    if not sources:
        return list(alerts)
    return [a for a in alerts
            if not (isinstance(a, str) and any(_longest_common(a, s) >= COPY_MIN_CHARS for s in sources))]


def apply_red_flags(result: Any, entry: Optional[Dict[str, Any]]) -> List[str]:
    """把红旗挂到 dashboard 供报告渲染。有重大红旗时，模型打 ✅ 的「无重大利空」项改为 ⚠️ 并注明；
    只有财务提示时不改模型的勾，清单末尾加一行「⚠️ 【估值报告】财务提示」。"""
    dashboard = getattr(result, "dashboard", None)
    if not entry or not isinstance(dashboard, dict):
        return []
    dashboard["valuation_red_flags"] = entry
    changed = ["valuation_red_flags"]
    # 风险警报只留分析窗口内的新事件：抄自估值报告的条目在「估值报告红旗 / 公司层面风险」一栏已有
    intel = dashboard.get("intelligence")
    alerts = intel.get("risk_alerts") if isinstance(intel, dict) else None
    if isinstance(alerts, list):
        kept = drop_copied_alerts(alerts, entry)
        if len(kept) != len(alerts):
            intel["risk_alerts"] = kept
            changed.append("intelligence.risk_alerts")
    if not entry["flags"]:
        return changed  # 只有一般性的公司层面风险时不去改模型的勾
    battle = dashboard.get("battle_plan")
    checklist = battle.get("action_checklist") if isinstance(battle, dict) else None
    if not isinstance(checklist, list):
        return changed
    kinds = flag_kinds(entry)
    n_major = kinds.count("major")
    # 模型自己写的【估值报告】项先去掉（这一项由程序写）
    new_list = [x for x in checklist if not (isinstance(x, str) and REPORT_TAG in x)]
    if n_major:
        note = f"〔估值报告（{entry['date']}）列有 {n_major} 条重大红旗，见「估值报告红旗」〕"
        new_list = [item.replace("✅", "⚠️", 1).rstrip() + " " + note
                    if (isinstance(item, str) and "【模型】" in item and item.lstrip().startswith("✅")
                        and _NO_RISK_CHECK.search(item) and note not in item) else item
                    for item in new_list]
    else:
        new_list.append(f"⚠️ {REPORT_TAG}财务提示 {len(kinds)} 条（估值报告 {entry['date']}，非重大利空，"
                        f"见「估值报告红旗」）")
    if new_list != checklist:
        battle["action_checklist"] = new_list
        changed.append("battle_plan.action_checklist")
    return changed
