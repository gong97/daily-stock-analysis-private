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
- 报告里单列一段，注明来源报告日期；模型仍打「✅ 无重大利空」时改成 ⚠️ 并注明。

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
    flags = [str(x) for x in entry.get("flags") or [] if str(x).strip()]
    risks = [str(x) for x in entry.get("company_risks") or [] if str(x).strip()]
    if not flags and not risks:
        return None
    # 报告原文里置信度常写成「中低** —— 核心原因：…」，只留开头的等级词
    confidence = re.split(r"[*—（(，,：:\s]", str(entry.get("confidence") or "").strip(), maxsplit=1)[0]
    return {"date": entry.get("date", ""), "confidence": confidence,
            "flags": flags, "company_risks": risks, "age_days": age, "source": SOURCE_LABEL[market]}


def render_red_flags_prompt(entry: Optional[Dict[str, Any]]) -> str:
    """提示词段落：作为事实提供，要求在风险排查里考虑。"""
    if not entry:
        return ""
    source = entry.get("source") or SOURCE_LABEL["cn"]
    if source == SOURCE_LABEL["cn"]:
        source = "Macd-Qlib-Analyzer 估值报告"  # A 股提示词原文不变
    lines = [f"\n### 估值报告质量红旗（{source}，{entry['date']}）"]
    for item in entry["flags"]:
        lines.append(f"- 红旗：{item}")
    for item in entry["company_risks"]:
        lines.append(f"- 公司层面风险：{item}")
    lines.append(
        "\n> 以上来自较早的深度估值报告，不一定出现在近几日新闻里。判断「有无重大利空」与写风险警报时必须考虑，"
        "不要因为近期新闻没提到就写「无重大利空」。"
    )
    return "\n".join(lines) + "\n"


def apply_red_flags(result: Any, entry: Optional[Dict[str, Any]]) -> List[str]:
    """把红旗挂到 dashboard 供报告渲染；模型打 ✅ 的「无重大利空」项改为 ⚠️ 并注明。"""
    dashboard = getattr(result, "dashboard", None)
    if not entry or not isinstance(dashboard, dict):
        return []
    dashboard["valuation_red_flags"] = entry
    changed = ["valuation_red_flags"]
    if not entry["flags"]:
        return changed  # 只有一般性的公司层面风险时不去改模型的勾
    battle = dashboard.get("battle_plan")
    checklist = battle.get("action_checklist") if isinstance(battle, dict) else None
    if isinstance(checklist, list):
        note = f"〔估值报告（{entry['date']}）列有 {len(entry['flags'])} 条质量红旗，见「估值报告红旗」〕"
        new_list = []
        for item in checklist:
            if (isinstance(item, str) and "【模型】" in item and item.lstrip().startswith("✅")
                    and _NO_RISK_CHECK.search(item) and note not in item):
                item = item.replace("✅", "⚠️", 1).rstrip() + " " + note
            new_list.append(item)
        if new_list != checklist:
            battle["action_checklist"] = new_list
            changed.append("battle_plan.action_checklist")
    return changed
